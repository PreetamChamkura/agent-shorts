"""Stage 5 — approve/reject.

Reads: work/{video_id}/out_{n}.mp4, work/{video_id}/segments.json
Writes: work/{video_id}/review.json

A local HTTP server (stdlib only, no new dependency) listing every rendered
clip for one video with its metadata, a short editorial checklist, and
approve/reject. The checklist is never a blocking gate — decisions save
independently of whether the checklist is filled in — it exists so rejection
patterns can inform future prompt revisions (CLAUDE.md).

Everything except the server plumbing is pure and unit-testable: page
rendering takes plain data in and returns an HTML string, and review.json
read/modify/write is a plain function. The server class is a thin adapter
over those.
"""

from __future__ import annotations

import html
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .paths import work_dir

log = logging.getLogger("clipper.review")

# (key, label) — shown as a Yes/No prompt per clip. Not blocking; unanswered
# stays null in review.json.
CHECKLIST_ITEMS = [
    ("vo_added_context", "Did the VO add real context, rather than re-describe the clip?"),
    (
        "matches_channel_style",
        "Does the editing look like this channel's own style, not a generic template?",
    ),
    ("stands_alone", "Does the clip stand alone without the source video?"),
]
CHECKLIST_KEYS = {key for key, _ in CHECKLIST_ITEMS}

_MEDIA_NAME_RE = re.compile(r"^(out_\d+\.mp4|vo_\d+\.wav)$")


def _load_review(video_id: str) -> dict:
    path = work_dir(video_id) / "review.json"
    if not path.exists():
        return {"video_id": video_id, "reviews": []}
    return json.loads(path.read_text())


def _segments_with_render(video_id: str) -> list[dict]:
    """Segments from segments.json that actually have a rendered out_{n}.mp4."""
    wd = work_dir(video_id)
    seg_path = wd / "segments.json"
    if not seg_path.exists():
        return []
    segments = json.loads(seg_path.read_text()).get("segments", [])
    return sorted(
        (s for s in segments if (wd / f"out_{s['n']}.mp4").exists()),
        key=lambda s: s["n"],
    )


def save_review(video_id: str, n: int, decision: str, checklist: dict, notes: str) -> dict:
    """Merge one clip's review into review.json and write it. Returns the full data.

    ``checklist`` may be a partial or empty dict — unanswered items stay null.
    Unknown keys are dropped rather than silently accepted.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(
            f"save_review: decision must be 'approved' or 'rejected', got {decision!r}"
        )

    clean_checklist = {key: checklist.get(key) for key in CHECKLIST_KEYS}

    data = _load_review(video_id)
    reviews = data.setdefault("reviews", [])
    entry = next((r for r in reviews if r.get("n") == n), None)
    if entry is None:
        entry = {"n": n}
        reviews.append(entry)
    entry["decision"] = decision
    entry["checklist"] = clean_checklist
    entry["notes"] = notes

    path = work_dir(video_id) / "review.json"
    path.write_text(json.dumps(data, indent=2))
    log.info("review: saved n=%d decision=%s", n, decision)
    return data


def _reviews_by_n(video_id: str) -> dict[int, dict]:
    return {r["n"]: r for r in _load_review(video_id).get("reviews", [])}


def _checklist_field_html(n: int, key: str, label: str, value: bool | None) -> str:
    yes = " checked" if value is True else ""
    no = " checked" if value is False else ""
    return (
        '<div class="check-item">'
        f"<span>{html.escape(label)}</span>"
        f'<label><input type="radio" name="{key}" value="yes" data-n="{n}"{yes}> Yes</label>'
        f'<label><input type="radio" name="{key}" value="no" data-n="{n}"{no}> No</label>'
        "</div>"
    )


def _clip_card_html(seg: dict, review: dict | None) -> str:
    n = seg["n"]
    review = review or {}
    checklist = review.get("checklist") or {}
    decision = review.get("decision")
    notes = html.escape(review.get("notes") or "")
    vo_script = html.escape(seg.get("vo_script") or "(no VO)")
    checklist_html = "\n".join(
        _checklist_field_html(n, key, label, checklist.get(key)) for key, label in CHECKLIST_ITEMS
    )
    status = f'<span class="status status-{decision}">{decision}</span>' if decision else ""

    return f"""
    <section class="clip" id="clip-{n}" data-n="{n}">
      <h2>Clip {n} — {html.escape(seg.get("caption_title", ""))} {status}</h2>
      <video controls preload="metadata" src="/media/out_{n}.mp4"></video>
      <dl class="meta">
        <dt>Tone</dt><dd>{html.escape(seg.get("tone", ""))}</dd>
        <dt>Hook</dt><dd>{html.escape(seg.get("hook", ""))}</dd>
        <dt>Loopable</dt><dd>{"yes" if seg.get("loopable") else "no"}</dd>
        <dt>VO script</dt><dd>{vo_script}</dd>
      </dl>
      <div class="checklist">{checklist_html}</div>
      <textarea class="notes" placeholder="Notes (optional)">{notes}</textarea>
      <div class="actions">
        <button class="approve" onclick="save({n}, 'approved')">Approve</button>
        <button class="reject" onclick="save({n}, 'rejected')">Reject</button>
        <span class="saved" id="saved-{n}"></span>
      </div>
    </section>
    """


def render_review_page(video_id: str, segments: list[dict], reviews_by_n: dict[int, dict]) -> str:
    """Pure: build the full review page for ``video_id``. No I/O."""
    if not segments:
        body = "<p>No rendered clips found for this video yet.</p>"
    else:
        body = "\n".join(_clip_card_html(seg, reviews_by_n.get(seg["n"])) for seg in segments)

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Review — {html.escape(video_id)}</title>
<style>
  html {{ background: #f5f5f5; }}
  body {{ font-family: -apple-system, sans-serif; max-width: 640px; margin: 2rem auto;
    color: #1a1a1a; background: #ffffff; padding: 1.5rem; border-radius: 8px; }}
  .clip {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin-bottom: 2rem; }}
  video {{ width: 100%; max-height: 70vh; background: #000; border-radius: 4px; }}
  .meta {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 1rem;
    margin: 0.75rem 0; }}
  .meta dt {{ font-weight: 600; color: #555; }}
  .meta dd {{ margin: 0; }}
  .checklist {{ display: flex; flex-direction: column; gap: 0.4rem; margin: 0.75rem 0; }}
  .check-item {{ display: flex; justify-content: space-between; align-items: center; gap: 1rem; }}
  .check-item span {{ flex: 1; }}
  .notes {{ width: 100%; box-sizing: border-box; min-height: 3rem; margin-bottom: 0.75rem; }}
  .actions {{ display: flex; align-items: center; gap: 0.75rem; }}
  button {{ padding: 0.5rem 1.25rem; border-radius: 6px; border: none; cursor: pointer;
    font-weight: 600; }}
  .approve {{ background: #1a7f37; color: white; }}
  .reject {{ background: #cf222e; color: white; }}
  .status {{ font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 4px;
    text-transform: uppercase; }}
  .status-approved {{ background: #d3f2dd; color: #1a7f37; }}
  .status-rejected {{ background: #fbdada; color: #cf222e; }}
  .saved {{ color: #1a7f37; font-size: 0.9rem; }}
</style>
</head>
<body>
<h1>Review: {html.escape(video_id)}</h1>
<p>Not a blocking gate — the checklist is just prompts.
Approve/Reject saves regardless of whether it's filled in.</p>
{body}
<script>
function save(n, decision) {{
  const card = document.getElementById('clip-' + n);
  const checklist = {{}};
  card.querySelectorAll('input[type=radio]:checked').forEach(el => {{
    checklist[el.name] = el.value === 'yes';
  }});
  const notes = card.querySelector('.notes').value;
  fetch('/save', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{n, decision, checklist, notes}}),
  }})
    .then(r => {{ if (!r.ok) throw new Error('save failed'); return r.json(); }})
    .then(() => {{
      document.getElementById('saved-' + n).textContent = 'Saved (' + decision + ')';
    }})
    .catch(() => {{
      document.getElementById('saved-' + n).textContent = 'Save failed — see server log';
    }});
}}
</script>
</body>
</html>"""


class _ReviewHandler(BaseHTTPRequestHandler):
    video_id: str = ""  # set per-server via functools.partial in review()

    def log_message(self, fmt: str, *args) -> None:  # quiet by default; use log.info explicitly
        log.debug(fmt, *args)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "":
            segments = _segments_with_render(self.video_id)
            page = render_review_page(self.video_id, segments, _reviews_by_n(self.video_id))
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/media/"):
            name = self.path[len("/media/") :]
            if not _MEDIA_NAME_RE.match(name):
                self.send_error(404, "not found")
                return
            path = work_dir(self.video_id) / name
            if not path.exists():
                self.send_error(404, "not found")
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4" if name.endswith(".mp4") else "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404, "not found")

    def do_POST(self) -> None:
        if self.path != "/save":
            self.send_error(404, "not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
            n = int(payload["n"])
            decision = str(payload["decision"])
            checklist = payload.get("checklist") or {}
            notes = str(payload.get("notes") or "")
            save_review(self.video_id, n, decision, checklist, notes)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            log.warning("review: bad /save request: %s", exc)
            self.send_error(400, "bad request")
            return

        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def review(video_id: str, *, port: int = 8765, serve_forever: bool = True) -> ThreadingHTTPServer:
    """Start the local review server for ``video_id`` and return it.

    With ``serve_forever`` (the default), blocks running the server until
    interrupted (Ctrl+C). Pass False to get the server back unstarted, e.g.
    for tests that drive one request then shut it down themselves.
    """
    handler = type("_BoundReviewHandler", (_ReviewHandler,), {"video_id": video_id})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    if serve_forever:
        log.info("review: serving http://127.0.0.1:%d/ for %s — Ctrl+C to stop", port, video_id)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
    return httpd
