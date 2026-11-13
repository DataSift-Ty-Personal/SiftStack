"""Files dropped in Slack: walkthrough photos, a boundary as KML, KMZ, GeoJSON or CSV, a PDF.

Slack delivers a file as metadata on the message event; the bytes sit behind the bot token on
files.slack.com and need the files:read scope. Intake has three seconds to answer Slack, so it only
RECORDS what arrived; the worker SAVES the files into the working employee's own workspace when its
turn starts, because the employee's Read tool may not leave that workspace.

  * Files on a thread are kept under kv `files:<channel>:<thread>`, whoever they were meant for, so
    a teammate the work is handed to, a reply after an ask-back and a follow-up all see them.
  * Files a person posts in the channel without addressing anyone are HELD for that person: the
    next ask they make in the same channel within PENDING_MIN takes them (people post the photos
    first and say what they want second).
  * A photo is saved as Slack's own 1024px preview when Slack made one: about a tenth of the
    original's size, plenty to judge a kitchen or a roof, and it turns an iPhone HEIC into a JPEG.
  * A KMZ (Google Earth) is unzipped to its KML, so a boundary can be read as text.

Nothing here raises into intake or the worker. A file that cannot be saved is listed with the reason.
"""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

from . import config, slack_app, store

log = logging.getLogger(__name__)

PENDING_MIN = 30
INBOX = "inbox"
LAST_KEY = "slack:files:last"            # the last save attempt, for /health: "ok ..." or "blocked ..."
_LOCK = threading.Lock()
THUMBS = ("thumb_1024", "thumb_960", "thumb_800", "thumb_720")
VIEWABLE = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXT_TYPES = {"kml", "geojson", "json", "csv", "txt", "text", "gpx", "xml", "md", "markdown", "tsv"}
PHOTO_EXT = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"}
SCOPE_HELP = ("the Slack app is missing the files:read permission (add it under OAuth & Permissions in "
              "the app's settings, then click Reinstall)")
SCOPE_NOTICE = ("I can see %d file%s here but can't open %s yet: the Slack app is missing the files:read "
                "permission. Add it under OAuth & Permissions in the app's settings, click Reinstall, then "
                "post the files again.")


def kind_of(f: dict) -> str:
    mime, ft = (f.get("mimetype") or "").lower(), (f.get("filetype") or "").lower()
    name = (f.get("name") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if mime.startswith("image/") or (not mime and ext in PHOTO_EXT):
        return "photo"
    if mime == "application/pdf" or ft == "pdf":
        return "pdf"
    if ft == "kmz" or name.endswith(".kmz"):
        return "kmz"
    if ft in TEXT_TYPES or mime.startswith("text/") or ext in TEXT_TYPES:
        return "text"
    return "other"


def brief(f: dict, user: str = "", ts: str = "") -> dict:
    """The part of Slack's file object the worker needs later. Slack may send only a stub
    (file_access: check_file_info); the worker then asks files.info for the rest."""
    url = f.get("url_private_download") or f.get("url_private") or ""
    thumb = next((f[k] for k in THUMBS if f.get(k)), "")
    return {"id": str(f.get("id") or ""), "name": str(f.get("name") or f.get("title") or f.get("id") or "file"),
            "kind": kind_of(f), "mimetype": f.get("mimetype") or "", "size": int(f.get("size") or 0),
            "url": url, "thumb": thumb, "needs_info": f.get("file_access") == "check_file_info" or not url,
            "user": user, "ts": ts}


def from_event(event: dict) -> list[dict]:
    user, ts = event.get("user") or "", event.get("ts") or ""
    return [brief(f, user, ts) for f in (event.get("files") or []) if isinstance(f, dict) and f.get("id")]


def _thread_key(channel: str, thread_ts: str) -> str:
    return "files:%s:%s" % (channel, thread_ts)


def _load(key: str) -> list[dict]:
    try:
        v = json.loads(store.kv_get(key) or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def add(channel: str, thread_ts: str, files: list[dict]) -> int:
    """Record files on a thread; returns how many were new. At most MAX_FILES per thread."""
    if not (channel and thread_ts and files):
        return 0
    key = _thread_key(channel, thread_ts)
    with _LOCK:
        have = _load(key)
        ids = {f.get("id") for f in have}
        new = [f for f in files if f.get("id") and f["id"] not in ids]
        room = max(0, config.MAX_FILES - len(have))
        kept = new[:room]
        if len(new) > room:
            store.kv_set(key + ":over", str(int(store.kv_get(key + ":over") or 0) + len(new) - room))
        if kept:
            store.kv_set(key, json.dumps(have + kept))
        return len(kept)


def on_thread(channel: str, thread_ts: str) -> list[dict]:
    return _load(_thread_key(channel, thread_ts)) if channel and thread_ts else []


def over(channel: str, thread_ts: str) -> int:
    """Files posted on the thread past MAX_FILES, which were not kept."""
    try:
        return int(store.kv_get(_thread_key(channel, thread_ts) + ":over") or 0)
    except ValueError:
        return 0


def _pending_key(channel: str, user: str) -> str:
    return "files:pending:%s:%s" % (channel, user)


def _fresh(files: list[dict]) -> list[dict]:
    cut = time.time() - PENDING_MIN * 60
    return [f for f in files if float(f.get("held_at") or 0) >= cut]


def hold(channel: str, user: str, files: list[dict]) -> int:
    """Files posted without an ask: kept for this person's next ask in this channel."""
    if not (channel and user and files):
        return 0
    key = _pending_key(channel, user)
    with _LOCK:
        held = _fresh(_load(key))
        ids = {f.get("id") for f in held}
        now = time.time()
        held += [dict(f, held_at=now) for f in files if f.get("id") not in ids]
        store.kv_set(key, json.dumps(held[-config.MAX_FILES:]))
        return len(held)


def claim(channel: str, user: str) -> list[dict]:
    """The files this person held in this channel in the last PENDING_MIN minutes, cleared once taken."""
    if not (channel and user):
        return []
    key = _pending_key(channel, user)
    with _LOCK:
        held = _fresh(_load(key))
        if held or store.kv_get(key):
            store.kv_set(key, "[]")
    return [{k: v for k, v in f.items() if k != "held_at"} for f in held]


def unreadable_notice(channel: str, thread_ts: str, n: int) -> bool:
    """When the app lacks files:read, say so once in the thread, from intake, before any turn runs:
    an employee told only that the files failed would guess at why."""
    if not (n and config.SLACK_BOT_TOKEN) or slack_app.can_read_files():
        return False
    if not store.seen_once("files:noscope:%s:%s" % (channel, thread_ts)):
        return False
    slack_app.post(channel, SCOPE_NOTICE % (n, "" if n == 1 else "s", "it" if n == 1 else "them"),
                   thread_ts=thread_ts)
    return True


# --------------------------------------------------------------------------------- the worker side

def _safe(text: str, keep: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.")
    return (s or "file")[:keep]


def _prune(inbox: Path) -> None:
    """Saved files are kept FILES_KEEP_DAYS, then the thread's folder goes."""
    if not inbox.is_dir():
        return
    cut = time.time() - config.FILES_KEEP_DAYS * 86400
    for d in inbox.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cut:
                shutil.rmtree(d)
        except OSError as exc:
            log.warning("could not prune %s: %s", d, exc)


def _source(f: dict) -> tuple[str, str]:
    """(url, file extension) to save. A photo is saved as Slack's preview when there is one."""
    if f["kind"] == "photo":
        if f.get("thumb"):
            ext = ".png" if f["thumb"].lower().split("?")[0].endswith(".png") else ".jpg"
            return f["thumb"], ext
        if (f.get("mimetype") or "").lower() not in VIEWABLE:
            return "", ""
    suffix = Path(f.get("name") or "").suffix.lower()
    return f.get("url") or "", suffix if suffix and len(suffix) <= 8 else ""


def _download(url: str, dest: Path, limit_mb: float) -> str:
    """Save one Slack file; "" on success, else why not."""
    try:
        r = slack_app.fetch(url)
    except Exception as exc:  # noqa: BLE001  (requests, timeouts, anything)
        return "Slack could not be reached (%s)" % type(exc).__name__
    if getattr(r, "status_code", 200) != 200:
        return "Slack answered %s" % getattr(r, "status_code", "?")
    ctype = str((getattr(r, "headers", None) or {}).get("content-type") or "").lower()
    if "text/html" in ctype:
        slack_app.forget_scopes()
        return SCOPE_HELP
    data = getattr(r, "content", b"") or b""
    if len(data) > limit_mb * 1024 * 1024:
        return "larger than %d MB" % int(limit_mb)
    if not data:
        return "Slack sent an empty file"
    dest.write_bytes(data)
    return ""


def _kml_from_kmz(path: Path) -> tuple[Path, str]:
    """A KMZ is a zip with the KML inside; the KML is what can be read."""
    try:
        with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".kml")), "")
            if not name:
                return path, "the KMZ holds no KML"
            out = path.with_suffix(".kml")
            out.write_bytes(z.read(name))
        path.unlink(missing_ok=True)
        return out, ""
    except (zipfile.BadZipFile, OSError) as exc:
        return path, "the KMZ could not be opened (%s)" % type(exc).__name__


def fetch_for_turn(task: dict, ws: Path) -> list[dict]:
    """Save the thread's files into the employee's workspace; one row per file:
    {"name", "kind", "path" (relative to the workspace, "" when not saved), "why"}."""
    channel, thread_ts = task.get("slack_channel") or "", task.get("slack_thread") or ""
    files = on_thread(channel, thread_ts)
    if not files:
        return []
    inbox = Path(ws) / INBOX
    _prune(inbox)
    dest_dir = inbox / _safe("%s-%s" % (channel, thread_ts), 80)
    rows, blocked = [], not slack_app.can_read_files()
    for i, f in enumerate(files, 1):
        row = {"name": f.get("name") or "file", "kind": f.get("kind") or "other", "path": "", "why": ""}
        rows.append(row)
        if blocked:
            row["why"] = SCOPE_HELP
            continue
        if f.get("needs_info"):
            # Slack sent only a stub: the full object (type, links, preview) comes from files.info.
            full = slack_app.file_info(f.get("id") or "")
            if full:
                f = brief(full, f.get("user", ""), f.get("ts", ""))
                row.update(name=f["name"], kind=f["kind"])
        if row["kind"] == "other":
            row["why"] = "not a photo, PDF or text file"
            continue
        url, ext = _source(f)
        if not url:
            row["why"] = "Slack made no viewable copy of this photo"
            continue
        stem = _safe(Path(row["name"]).stem, 50)
        target = dest_dir / ("%02d-%s%s" % (i, stem, ext))
        done = [p for p in (target, target.with_suffix(".kml")) if p.is_file() and p.stat().st_size > 0]
        if done:
            row["path"] = done[0].relative_to(ws).as_posix()
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        why = _download(url, target, config.MAX_FILE_MB)
        if not why and row["kind"] == "kmz":
            target, why = _kml_from_kmz(target)
        if why:
            row["why"] = why
            target.unlink(missing_ok=True)
            if why == SCOPE_HELP:
                blocked = True
            continue
        row["path"] = target.relative_to(ws).as_posix()
    saved = sum(1 for r in rows if r["path"])
    if saved or blocked:
        store.kv_set(LAST_KEY, "%s %s, %d of %d saved" % ("ok" if not blocked else "blocked",
                                                         store.now(), saved, len(rows)))
    if dest_dir.is_dir():
        dest_dir.touch()           # a thread still being worked on is not pruned
    return rows


def prompt_lines(rows: list[dict], dropped: int = 0) -> list[str]:
    """What the employee is told about the files on its thread."""
    if not rows:
        return []
    saved = [r for r in rows if r["path"]]
    lines = []
    if saved:
        photos = sum(1 for r in saved if r["kind"] == "photo")
        lines += ["", "Files on this thread, saved in your workspace. Open them with Read, several in one step:"]
        lines += ["- %s (%s)" % (r["path"], r["kind"] if r["kind"] != "kmz" else "boundary KML") for r in saved]
        if photos:
            lines.append("Look at every photo before you answer; say which photo each finding came from.")
    failed = [r for r in rows if not r["path"]]
    if failed:
        lines += ["", "Files you could not open (tell the operator why, in one line):"]
        lines += ["- %s: %s" % (r["name"], r["why"]) for r in failed[:10]]
        if len(failed) > 10:
            lines.append("- and %d more" % (len(failed) - 10))
    if dropped:
        lines.append("%d more files were posted than the %d kept for one thread." % (dropped, config.MAX_FILES))
    return lines
