"""Twitch Helix API — clip discovery for stage 2b (see CLAUDE.md).

Not a pipeline stage itself: this fetches a *list* of candidate clips (id,
url, title, view_count, created_at) for a human to sanity-check before
anything is spent on ingest/transcribe/select. Downloading a chosen clip
reuses the existing, source-agnostic clipper.ingest.ingest() unchanged —
a Twitch clip URL is just another yt-dlp-supported URL, and TwitchClipsIE
reports no live_status at all (a clip is a fixed-duration excerpt, finished
the instant it's created, regardless of whether the source channel happens
to be live right now).

Auth: client-credentials grant (app access token) against Twitch's OAuth2
token endpoint. This is a read-only public-data lookup — no user login, no
scopes; the app never acts on anyone's behalf. Requires TWITCH_CLIENT_ID and
TWITCH_CLIENT_SECRET in .env (see .env.example for where to register them).

Token caching: app access tokens are long-lived (observed expires_in on a
real token: ~5.67M seconds, ~65 days) but not infinite, so the token is
cached to disk (work/_twitch_cache/token.json) and refreshed proactively —
before its recorded expiry, with a safety margin — rather than waiting for a
401 mid-batch.

Rate limits: Helix allows 800 points/minute per client id for an app token,
~1 point per request for /helix/clips (100 clips/page via pagination). A
batch pull of even a few hundred clips is a handful of requests — nowhere
near the limit — but a 429 is still handled by sleeping until the
Ratelimit-Reset header says the bucket refills, then retrying once.
"""

from __future__ import annotations

import json
import logging
import os
import time

import requests

from .paths import TWITCH_CACHE_DIR

log = logging.getLogger("clipper.twitch")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.twitch.tv/helix"
TOKEN_CACHE_PATH = TWITCH_CACHE_DIR / "token.json"

# Refresh if less than this much of the token's life remains, so a run
# started near expiry doesn't fail partway through a batch pull.
REFRESH_MARGIN_SECONDS = 3600.0

CLIPS_PAGE_SIZE = 100  # Helix's max `first` for /helix/clips
TIMEOUT = 30.0


class TwitchAuthError(RuntimeError):
    """Missing credentials or a token request Twitch rejected."""


def is_twitch_clip_url(url: str) -> bool:
    """True if ``url`` is a Twitch *clip* (not a VOD or channel) — used by
    the `clip` CLI command to route to select_clip() instead of select().

    Delegates to yt-dlp's own TwitchClipsIE regex rather than a hand-rolled
    pattern, so this can't drift from what actually downloads as a clip.
    """
    from yt_dlp.extractor.twitch import TwitchClipsIE

    return TwitchClipsIE.suitable(url)


def _env_credentials(client_id: str | None, client_secret: str | None) -> tuple[str, str]:
    client_id = client_id or os.environ.get("TWITCH_CLIENT_ID")
    client_secret = client_secret or os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise TwitchAuthError(
            "twitch: TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set (see .env.example). "
            "Register an app at dev.twitch.tv/console/apps."
        )
    return client_id, client_secret


def _load_cached_token() -> dict | None:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cached_token(access_token: str, expires_at: float) -> None:
    TWITCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"access_token": access_token, "expires_at": expires_at})
    )


def token_needs_refresh(cached: dict | None, *, now: float | None = None) -> bool:
    """True if ``cached`` is missing, malformed, or within the refresh margin
    of its recorded expiry. Pure — takes `now` explicitly so it's testable
    without mocking the clock."""
    if not cached or "access_token" not in cached or "expires_at" not in cached:
        return True
    now = time.time() if now is None else now
    return now >= float(cached["expires_at"]) - REFRESH_MARGIN_SECONDS


def get_app_access_token(
    *, client_id: str | None = None, client_secret: str | None = None, force: bool = False
) -> str:
    """Return a valid app access token, refreshing before expiry as needed.

    Cached to disk (shared across every call in this process or a later one) —
    never re-requested just because a new CLI invocation started.
    """
    cid, secret = _env_credentials(client_id, client_secret)

    cached = None if force else _load_cached_token()
    if not token_needs_refresh(cached):
        return cached["access_token"]

    resp = requests.post(
        TOKEN_URL,
        data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
        timeout=TIMEOUT,
    )
    if resp.status_code == 401 or resp.status_code == 403:
        raise TwitchAuthError(
            f"twitch: token request rejected ({resp.status_code}): {resp.text[:200]} — "
            "check TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET in .env"
        )
    resp.raise_for_status()
    data = resp.json()
    access_token = data["access_token"]
    expires_at = time.time() + float(data["expires_in"])
    _save_cached_token(access_token, expires_at)
    log.info("twitch: new app access token, expires in %.1f days", data["expires_in"] / 86400)
    return access_token


def _auth_headers(token: str, client_id: str) -> dict:
    return {"Client-Id": client_id, "Authorization": f"Bearer {token}"}


def _request(
    method: str, url: str, *, headers: dict, params: dict | None = None
) -> requests.Response:
    """One Helix call with a single 429 retry, honoring Ratelimit-Reset."""
    resp = requests.request(method, url, headers=headers, params=params, timeout=TIMEOUT)
    if resp.status_code == 429:
        reset_at = float(resp.headers.get("Ratelimit-Reset", time.time() + 5))
        wait = max(0.0, reset_at - time.time()) + 0.5
        log.warning("twitch: rate limited, waiting %.1fs for the bucket to refill", wait)
        time.sleep(wait)
        resp = requests.request(method, url, headers=headers, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def get_broadcaster_id(login: str, *, token: str, client_id: str) -> str:
    """Resolve a channel login (e.g. "shroud") to its numeric broadcaster id.

    Raises ValueError if the login doesn't exist — a typo'd channel name
    should fail loudly here, not surface as an empty clips list later.
    """
    resp = _request(
        "GET", f"{API_BASE}/users", headers=_auth_headers(token, client_id), params={"login": login}
    )
    users = resp.json().get("data", [])
    if not users:
        raise ValueError(f"twitch: no channel found for login {login!r}")
    return users[0]["id"]


def _normalize_clip(raw: dict) -> dict:
    return {
        "id": raw["id"],
        "url": raw.get("url"),
        "embed_url": raw.get("embed_url"),
        "broadcaster_name": raw.get("broadcaster_name"),
        "creator_name": raw.get("creator_name"),
        "title": raw.get("title", ""),
        "view_count": int(raw.get("view_count") or 0),
        "created_at": raw.get("created_at"),
        "duration": float(raw.get("duration") or 0.0),
        "game_id": raw.get("game_id"),
        "language": raw.get("language"),
        "thumbnail_url": raw.get("thumbnail_url"),
    }


def fetch_clips(
    broadcaster_id: str,
    *,
    token: str,
    client_id: str,
    limit: int = 20,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> list[dict]:
    """Up to ``limit`` clips for ``broadcaster_id``, sorted by view count.

    ``started_at``/``ended_at`` are RFC3339 timestamps (Helix's own format,
    e.g. "2024-01-01T00:00:00Z"), both optional. Paginates in pages of
    CLIPS_PAGE_SIZE until ``limit`` is reached or Helix runs out of pages.

    Helix returns clips ordered by view count already when filtering by
    broadcaster_id, but that ordering is re-asserted here explicitly rather
    than trusted implicitly — "sortable by view count" is a hard requirement,
    not an assumption about API behavior that could change.
    """
    params = {"broadcaster_id": broadcaster_id, "first": min(CLIPS_PAGE_SIZE, max(limit, 1))}
    if started_at:
        params["started_at"] = started_at
    if ended_at:
        params["ended_at"] = ended_at

    headers = _auth_headers(token, client_id)
    collected: list[dict] = []
    cursor: str | None = None

    while len(collected) < limit:
        page_params = dict(params)
        if cursor:
            page_params["after"] = cursor
        resp = _request("GET", f"{API_BASE}/clips", headers=headers, params=page_params)
        body = resp.json()
        page = body.get("data", [])
        collected.extend(_normalize_clip(c) for c in page)
        cursor = (body.get("pagination") or {}).get("cursor")
        if not cursor or not page:
            break

    collected.sort(key=lambda c: c["view_count"], reverse=True)
    return collected[:limit]
