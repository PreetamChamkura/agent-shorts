"""Twitch Helix API — clip discovery for stage 2b (see CLAUDE.md).

Not a pipeline stage itself: this fetches a *list* of candidate clips (id,
url, title, view_count, created_at) for a human to sanity-check before
anything is spent on ingest/transcribe/select. Downloading a chosen clip
reuses the existing, source-agnostic clipper.ingest.ingest() unchanged —
a Twitch clip URL is just another yt-dlp-supported URL, and TwitchClipsIE
reports no live_status at all (a clip is a fixed-duration excerpt, finished
the instant it's created, regardless of whether the source channel happens
to be live right now).

Two discovery modes, sharing everything downstream: `fetch_clips()` for a
specific known channel, and `fetch_trending_clips()` for channels not known
in advance — clips returned by either flow through the identical
ingest/transcribe/visual-gate/select_clip/render pipeline unchanged. Each
clip is tagged clip["discovery"] = {"mode": "channel"|"trending", ...} so
which mode found it stays visible wherever clip metadata already travels.

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


def _normalize_stream(raw: dict) -> dict:
    return {
        "broadcaster_id": raw["user_id"],
        "broadcaster_login": raw.get("user_login"),
        "broadcaster_name": raw.get("user_name"),
        "viewer_count": int(raw.get("viewer_count") or 0),
        "game_id": raw.get("game_id"),
        "game_name": raw.get("game_name"),
        "title": raw.get("title", ""),
        "language": raw.get("language"),
    }


def get_top_live_streams(*, token: str, client_id: str, count: int = 20) -> list[dict]:
    """Up to ``count`` currently-live streams, no filter — Helix's own
    default order for an unfiltered /helix/streams call.

    Confirmed with a real call (no filter applied) rather than assumed:
    results already come back sorted by viewer_count descending. Re-sorted
    here anyway for the same reason fetch_clips() re-asserts its own sort —
    a hard requirement shouldn't rest on an implicit, undocumented ordering
    that could change. Paginates in pages of CLIPS_PAGE_SIZE if ``count``
    exceeds Helix's 100-per-page max.
    """
    headers = _auth_headers(token, client_id)
    collected: list[dict] = []
    cursor: str | None = None

    while len(collected) < count:
        params = {"first": min(CLIPS_PAGE_SIZE, max(count, 1))}
        if cursor:
            params["after"] = cursor
        resp = _request("GET", f"{API_BASE}/streams", headers=headers, params=params)
        body = resp.json()
        page = body.get("data", [])
        collected.extend(_normalize_stream(s) for s in page)
        cursor = (body.get("pagination") or {}).get("cursor")
        if not cursor or not page:
            break

    collected.sort(key=lambda s: s["viewer_count"], reverse=True)
    return collected[:count]


def fetch_trending_clips(
    *,
    token: str,
    client_id: str,
    limit: int = 20,
    top_channels: int = 15,
    per_channel_limit: int = 5,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> list[dict]:
    """Up to ``limit`` clips discovered from currently-trending channels,
    with no broadcaster given up front — the trending counterpart to
    fetch_clips().

    Helix's /helix/clips has no global/unfiltered mode: it requires
    broadcaster_id, game_id, or id (confirmed against the API reference,
    not assumed). There is no server-side "trending clips" query at all.
    So this goes through /helix/streams first (which DOES support a
    zero-filter call, confirmed for real: an unfiltered request came back
    already sorted by viewer_count, 384824 > 199639 > 62450 > ...) to find
    the top currently-live channels, then calls the existing fetch_clips()
    per channel — reusing it unchanged rather than duplicating its
    pagination/sort/normalization logic.

    Each returned clip carries clip["discovery"] = {"mode": "trending",
    "channel": <broadcaster_login>, "viewer_count_at_discovery": <int>} —
    the same field the `fetch-clips` CLI command stamps with mode="channel"
    for the broadcaster-specific path, so which discovery mode found a clip
    is visible wherever clip metadata already travels (the printed list,
    the --out manifest, and the visual-gate-annotated dicts) without a new
    mechanism.

    A live channel with zero public clips (or a clip fetch that errors) is
    skipped, not fatal — trending discovery inherently means some
    candidates won't pan out. Queries at most ``top_channels`` channels
    regardless of whether ``limit`` is reached; returns fewer than
    ``limit`` rather than open-endedly querying more channels.
    """
    streams = get_top_live_streams(token=token, client_id=client_id, count=top_channels)
    log.info("twitch: fetch_trending_clips: %d top live channel(s) to query", len(streams))

    collected: list[dict] = []
    for stream in streams:
        try:
            clips = fetch_clips(
                stream["broadcaster_id"],
                token=token,
                client_id=client_id,
                limit=per_channel_limit,
                started_at=started_at,
                ended_at=ended_at,
            )
        except requests.RequestException as exc:
            log.warning(
                "twitch: fetch_trending_clips: clip fetch failed for %s (%s) — skipping",
                stream["broadcaster_login"],
                exc,
            )
            continue

        for clip in clips:
            clip["discovery"] = {
                "mode": "trending",
                "channel": stream["broadcaster_login"],
                "viewer_count_at_discovery": stream["viewer_count"],
            }
        collected.extend(clips)
        log.info(
            "twitch: fetch_trending_clips: %s (%d viewers) -> %d clip(s)",
            stream["broadcaster_login"],
            stream["viewer_count"],
            len(clips),
        )

    collected.sort(key=lambda c: c["view_count"], reverse=True)
    result = collected[:limit]
    log.info(
        "twitch: fetch_trending_clips: %d channel(s) queried -> %d clip(s) found, %d kept",
        len(streams),
        len(collected),
        len(result),
    )
    return result
