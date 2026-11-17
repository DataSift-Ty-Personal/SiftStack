"""Files dropped in Slack: walkthrough photos, a walkthrough video, a boundary as KML, KMZ, GeoJSON or
CSV, a PDF, and a Google Drive or Dropbox share link pasted in the message.

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
  * A VIDEO (mp4, mov, m4v) is recorded but never saved into the workspace: an employee cannot watch
    one, and on every turn a phone video would be read again for nothing. The walkthrough_video
    workflow downloads it (save_video) into a work folder outside the volume, because one phone video
    can be bigger than the brain's whole volume and the database lives there.
  * A SHARE LINK (a Google Drive or Dropbox file) is recorded the same way, since phone video often
    passes what Slack takes comfortably. It is fetched with no Slack token: the bot token belongs to
    files.slack.com and must never reach Google or Dropbox.
  * A Slack video over AGENTS_MAX_VIDEO_MB with no share link on the thread is said once, from
    intake, with what to do instead, so the employee is not left to guess why it cannot be read.

Nothing here raises into intake or the worker. A file that cannot be saved is listed with the reason.
"""
from __future__ import annotations

import errno
import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests

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
VIDEO_EXT = {"mp4", "mov", "m4v"}
VIDEO_MIME = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-m4v": ".m4v"}
HOSTS = {"drive": "Google Drive", "dropbox": "Dropbox"}
FRAMES_DIR = "video-frames"
MB = 1024 * 1024
UA = "Mozilla/5.0 (compatible; siftstack-agents)"
SCOPE_HELP = ("the Slack app is missing the files:read permission (add it under OAuth & Permissions in "
              "the app's settings, then click Reinstall)")
SCOPE_NOTICE = ("I can see %d file%s here but can't open %s yet: the Slack app is missing the files:read "
                "permission. Add it under OAuth & Permissions in the app's settings, click Reinstall, then "
                "post the files again.")
BIG_NOTICE = ("%s is %s, more than the %d MB I can take from Slack. Put it in Google Drive or Dropbox, share it so "
              "anyone with the link can view, and paste the link here.")
BIG_WHY = ("it is %s, more than the %d MB a video posted in Slack can be; the thread was already told to share it as "
           "a Google Drive or Dropbox link, so do not repeat that unless asked")
FOLDER_WHY = ("it is a shared folder, and only a shared file can be opened: paste the link of each video in it, "
              "shared so anyone with the link can view")
NOT_SHARED = ("the link opens a sign-in or preview page instead of the video: set its sharing to anyone with the "
              "link can view, then paste it again")

# Stand-ins the selftest sets. None = the real one.
WEB = requests     # share links: a plain client that never carries the Slack token
DISK = None        # (path) -> free bytes on that disk


def _mb_setting(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def video_mb() -> float:
    """The largest video taken from Slack. Past it the walker shares a link instead."""
    return _mb_setting("AGENTS_MAX_VIDEO_MB", 500.0)


def link_mb() -> float:
    """The largest video taken from a share link (phone video passes what Slack takes comfortably)."""
    return _mb_setting("AGENTS_MAX_LINK_VIDEO_MB", 2000.0)


def disk_floor_mb() -> float:
    """Free space a download or a kept copy may never take a disk below."""
    return _mb_setting("AGENTS_VIDEO_DISK_FLOOR_MB", 300.0)


def size_text(n: int) -> str:
    n = int(n or 0)
    return ("%.1f GB" % (n / 1024.0 / MB)) if n >= 1024 * MB else ("%d MB" % max(1, round(n / MB)))


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
    if mime in VIDEO_MIME or ft in VIDEO_EXT or ext in VIDEO_EXT:
        return "video"
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
            "duration_ms": int(f.get("duration_ms") or 0),
            "url": url, "thumb": thumb, "needs_info": f.get("file_access") == "check_file_info" or not url,
            "user": user, "ts": ts}


# ------------------------------------------------------------------------- share links in the text

# Slack writes a link as <url> or <url|label> and escapes & as &amp; inside it. A bare link only
# reaches here from a tool input or a test.
_SLACK_URL = re.compile(r"<(https?://[^>|\s]+)(?:\|[^>]*)?>")
_BARE_URL = re.compile(r"https?://[^\s<>|]+")
_DRIVE_FILE = re.compile(r"^https?://(?:drive|docs)\.google\.com/(?:a/[^/\s]+/)?"
                         r"(?:file/d/([A-Za-z0-9_-]{10,})|(?:open|uc)\?(?:[^#\s]*&)?id=([A-Za-z0-9_-]{10,}))")
_DRIVE_DIRECT = re.compile(r"^https?://drive\.usercontent\.google\.com/(?:download|uc)\?(?:[^#\s]*&)?id=([A-Za-z0-9_-]{10,})")
_DRIVE_FOLDER = re.compile(r"^https?://drive\.google\.com/(?:a/[^/\s]+/)?drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,})")
_DROPBOX = re.compile(r"^https?://(?:www\.)?dropbox\.com/(s|scl/fi|sh|scl/fo)/([^?#\s]+)")
_DROPBOX_DIRECT = re.compile(r"^https?://dl\.dropboxusercontent\.com/[^\s]+")
_TRAIL = ".,;:!?)]}'\""


def _link(url: str, host: str, key: str, name: str, fetch: str, user: str, ts: str, folder: bool = False) -> dict:
    return {"id": "link-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], "name": name, "kind": "link",
            "host": host, "url": url, "fetch": fetch, "folder": folder, "mimetype": "", "size": 0, "duration_ms": 0,
            "thumb": "", "needs_info": False, "user": user, "ts": ts}


def _dl1(url: str) -> str:
    """A Dropbox share link made into its download: dl=1, every other part kept (a newer link carries
    its access key, rlkey, in the query, and without it the file does not open)."""
    s = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if k not in ("dl", "raw")]
    q.append(("dl", "1"))
    return urlunsplit((s.scheme, s.netloc, s.path, urlencode(q), ""))


def link_of(url: str, user: str = "", ts: str = "") -> dict | None:
    """The record for one Google Drive or Dropbox link, or None for any other link."""
    u = (url or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").rstrip(_TRAIL)
    m = _DRIVE_FILE.match(u) or _DRIVE_DIRECT.match(u)
    if m:
        fid = next(g for g in m.groups() if g)
        return _link(u, "drive", "drive:" + fid, "Google Drive link",
                     "https://drive.usercontent.google.com/download?id=%s&export=download&confirm=t" % fid, user, ts)
    m = _DRIVE_FOLDER.match(u)
    if m:
        return _link(u, "drive", "drive-folder:" + m.group(1), "Google Drive folder", "", user, ts, folder=True)
    m = _DROPBOX.match(u)
    if m:
        path = urlsplit(u).path
        if m.group(1) in ("sh", "scl/fo"):
            return _link(u, "dropbox", "dropbox-folder:" + path, "Dropbox folder", "", user, ts, folder=True)
        name = unquote(path.rstrip("/").rsplit("/", 1)[-1]) or "Dropbox link"
        return _link(u, "dropbox", "dropbox:" + path, name, _dl1(u), user, ts)
    if _DROPBOX_DIRECT.match(u):
        name = unquote(urlsplit(u).path.rstrip("/").rsplit("/", 1)[-1]) or "Dropbox link"
        return _link(u, "dropbox", "dropbox:" + urlsplit(u).path, name, u, user, ts)
    return None


def links_from_text(text: str, user: str = "", ts: str = "") -> list[dict]:
    """Every Google Drive or Dropbox share link in a message, once each, in the order written."""
    text = text or ""
    urls = [m.group(1) for m in _SLACK_URL.finditer(text)] + _BARE_URL.findall(_SLACK_URL.sub(" ", text))
    out, seen = [], set()
    for u in urls:
        rec = link_of(u, user, ts)
        if rec and rec["id"] not in seen:
            seen.add(rec["id"])
            out.append(rec)
    return out


def from_event(event: dict) -> list[dict]:
    user, ts = event.get("user") or "", event.get("ts") or ""
    files = [brief(f, user, ts) for f in (event.get("files") or []) if isinstance(f, dict) and f.get("id")]
    return files + links_from_text(event.get("text") or "", user, ts)


def _thread_key(channel: str, thread_ts: str) -> str:
    return "files:%s:%s" % (channel, thread_ts)


def _load(key: str) -> list[dict]:
    try:
        v = json.loads(store.kv_get(key) or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def add(channel: str, thread_ts: str, files: list[dict]) -> int:
    """Record files on a thread; returns how many were new. At most MAX_FILES per thread, share links
    included: a link is one more thing to open on the thread."""
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


def videos_on_thread(channel: str, thread_ts: str) -> list[dict]:
    """The videos and share links on a thread, in the order posted. A stub Slack sent without a name
    is included, since it may be a video; save_video completes it and says when it is not."""
    return [f for f in on_thread(channel, thread_ts)
            if f.get("kind") in ("video", "link") or (f.get("kind") == "other" and f.get("needs_info"))]


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


def _too_big(f: dict) -> bool:
    return f.get("kind") == "video" and int(f.get("size") or 0) > video_mb() * MB


too_big = _too_big


def _shared_link_on(files: list[dict]) -> bool:
    return any(f.get("kind") == "link" and not f.get("folder") for f in files)


def _big_video_notice(channel: str, thread_ts: str) -> bool:
    """A Slack video too large to take, with no share link on the thread yet: said once per video,
    plainly, with what to do. With a link already there, the link is what gets read, so nothing is said."""
    if not (config.SLACK_BOT_TOKEN and channel and thread_ts):
        return False
    files = on_thread(channel, thread_ts)
    if _shared_link_on(files):
        return False
    said = False
    for f in files:
        if _too_big(f) and store.seen_once("files:bigvideo:%s:%s:%s" % (channel, thread_ts, f.get("id"))):
            slack_app.post(channel, BIG_NOTICE % (f.get("name") or "That video", size_text(f.get("size")), int(video_mb())),
                           thread_ts=thread_ts)
            said = True
    return said


def unreadable_notice(channel: str, thread_ts: str, n: int) -> bool:
    """When the app lacks files:read, say so once in the thread, from intake, before any turn runs:
    an employee told only that the files failed would guess at why. A share link needs no Slack
    permission, so a thread holding only links is never told about files:read. A video too large for
    Slack is said here too, once."""
    said = _big_video_notice(channel, thread_ts)
    if not (n and config.SLACK_BOT_TOKEN):
        return said
    hosted = sum(1 for f in on_thread(channel, thread_ts) if f.get("kind") != "link")
    if not hosted or slack_app.can_read_files():
        return said
    if not store.seen_once("files:noscope:%s:%s" % (channel, thread_ts)):
        return said
    n = min(n, hosted)
    text = SCOPE_NOTICE % (n, "" if n == 1 else "s", "it" if n == 1 else "them")
    app = store.kv_get("slack:app_id")
    if app:
        text += " Straight to the page: https://api.slack.com/apps/%s/oauth" % app
    slack_app.post(channel, text, thread_ts=thread_ts)
    return True


# ------------------------------------------------------------------- a video that could not be read

def _fail_key(fid: str) -> str:
    return "files:videofail:%s" % fid


def note_failure(fid: str, why: str) -> None:
    """The workflow could not fetch a video: kept so a later turn lists it with the reason the thread
    was already given, instead of the employee saying it again or trying a fetch it cannot make."""
    if fid:
        store.kv_set(_fail_key(fid), (why or "")[:300])


def clear_failure(fid: str) -> None:
    if fid and store.kv_get(_fail_key(fid)):
        store.kv_set(_fail_key(fid), "")


def failure(fid: str) -> str:
    return store.kv_get(_fail_key(fid)) if fid else ""


# --------------------------------------------------------------------------------- the worker side

def _safe(text: str, keep: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.")
    return (s or "file")[:keep]


safe = _safe


def thread_dir(channel: str, thread_ts: str) -> str:
    """The folder under the workspace inbox that holds one thread's files."""
    return _safe("%s-%s" % (channel, thread_ts), 80)


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


def _video_row(row: dict, f: dict, files: list[dict]) -> None:
    """A video or a share link, listed for the employee and never saved into the workspace."""
    row.update(video=True, source=HOSTS.get(f.get("host") or "", "Slack"), link=f.get("kind") == "link",
               minutes=round(int(f.get("duration_ms") or 0) / 60000.0, 1))
    if f.get("kind") == "link" and f.get("folder"):
        row["why"] = FOLDER_WHY
        return
    if _too_big(f):
        if _shared_link_on(files):
            row["note"] = "too large to take from Slack; the share link on this thread is read instead"
        else:
            row.update(why=BIG_WHY % (size_text(f.get("size")), int(video_mb())), told=True)
        return
    failed = failure(f.get("id") or "")
    if failed:
        row.update(why="it could not be fetched: %s (the thread was already told)" % failed, told=True)


def _frames_key(channel: str, thread_ts: str) -> str:
    return "files:frames:%s:%s" % (channel, thread_ts)


def frames_on_thread(channel: str, thread_ts: str) -> list[dict]:
    return _load(_frames_key(channel, thread_ts)) if channel and thread_ts else []


def add_frames(channel: str, thread_ts: str, ws: Path, frames: list[dict], read: bool, limit: int = 60) -> list[dict]:
    """Frames pulled from a walkthrough video, put into the employee's workspace for this thread so they
    can be opened like photos: [{"src": Path, "name": "v1-0m42s-kitchen.jpg", "label": "video 1 at 0:42,
    kitchen"}]. `read` says whether walkthrough_video already read them; a box that does not pay for
    reading video leaves them for the employee. A new read replaces the earlier set."""
    if not (channel and thread_ts and frames):
        return []
    ws = Path(ws)
    dest = ws / INBOX / thread_dir(channel, thread_ts) / FRAMES_DIR
    if dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for f in frames[:limit]:
        src = Path(f["src"])
        if not src.is_file():
            continue
        target = dest / _safe(f.get("name") or src.name, 60)
        try:
            shutil.copyfile(src, target)
        except OSError as exc:
            log.warning("frame not copied: %s", exc)
            continue
        rows.append({"name": target.name, "path": target.relative_to(ws).as_posix(),
                     "label": str(f.get("label") or ""), "read": bool(read)})
    store.kv_set(_frames_key(channel, thread_ts), json.dumps(rows))
    dest.parent.touch()
    return rows


def fetch_for_turn(task: dict, ws: Path) -> list[dict]:
    """Save the thread's files into the employee's workspace; one row per file:
    {"name", "kind", "path" (relative to the workspace, "" when not saved), "why"}. A video or a share
    link carries "video": True and no path (walkthrough_video reads it); frames already saved for the
    thread come last, with kind "frame"."""
    channel, thread_ts = task.get("slack_channel") or "", task.get("slack_thread") or ""
    files = on_thread(channel, thread_ts)
    frames = frames_on_thread(channel, thread_ts)
    if not files and not frames:
        return []
    inbox = Path(ws) / INBOX
    _prune(inbox)
    dest_dir = inbox / thread_dir(channel, thread_ts)
    hosted = [f for f in files if f.get("kind") != "link"]      # a share link needs no Slack permission
    rows, blocked = [], (not slack_app.can_read_files()) if hosted else False
    for i, f in enumerate(files, 1):
        row = {"name": f.get("name") or "file", "kind": f.get("kind") or "other", "path": "", "why": ""}
        rows.append(row)
        if f.get("kind") == "link":
            _video_row(row, f, files)
            continue
        if blocked:
            row["why"] = SCOPE_HELP
            continue
        if f.get("needs_info"):
            # Slack sent only a stub: the full object (type, links, preview) comes from files.info.
            full = slack_app.file_info(f.get("id") or "")
            if full:
                f = brief(full, f.get("user", ""), f.get("ts", ""))
                row.update(name=f["name"], kind=f["kind"])
        if row["kind"] == "video":
            _video_row(row, f, files)
            continue
        if row["kind"] == "other":
            row["why"] = "not a photo, PDF, text file or mp4, mov or m4v video"
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
    for fr in frames:
        p = Path(ws) / str(fr.get("path") or "")
        try:
            p.resolve().relative_to(Path(ws).resolve())
        except (ValueError, OSError):
            continue
        if p.is_file():
            rows.append({"name": fr.get("name") or p.name, "kind": "frame", "path": fr["path"], "why": "",
                         "label": fr.get("label") or "", "read": bool(fr.get("read"))})
    saved = sum(1 for r in rows if r["path"] and r["kind"] != "frame")
    if saved or blocked:
        store.kv_set(LAST_KEY, "%s %s, %d of %d saved" % ("ok" if not blocked else "blocked",
                                                         store.now(), saved, len(hosted)))
    if dest_dir.is_dir():
        dest_dir.touch()           # a thread still being worked on is not pruned
    return rows


def prompt_lines(rows: list[dict], dropped: int = 0) -> list[str]:
    """What the employee is told about the files on its thread."""
    if not rows:
        return []
    saved = [r for r in rows if r["path"] and r["kind"] != "frame"]
    frames = [r for r in rows if r["path"] and r["kind"] == "frame"]
    clips = [r for r in rows if r.get("video") and not r.get("link") and not r["why"]]
    links = [r for r in rows if r.get("video") and r.get("link") and not r["why"]]
    told = [r for r in rows if r.get("told")]
    failed = [r for r in rows if not r["path"] and r["why"] and not r.get("told")]
    lines = []
    if saved:
        photos = sum(1 for r in saved if r["kind"] == "photo")
        lines += ["", "Files on this thread, saved in your workspace. Open them with Read, several in one step:"]
        lines += ["- %s (%s)" % (r["path"], r["kind"] if r["kind"] != "kmz" else "boundary KML") for r in saved]
        if photos:
            lines.append("Look at every photo before you answer; say which photo each finding came from.")
    if clips:
        lines += ["", "Walkthrough videos on this thread. Do not open them with Read. To read them, run "
                  "walkthrough_video for the house: it transcribes the narration, reads frames room by room, keeps "
                  "the video for buyers and builds the one repair number with underwrite:"]
        lines += ["- %s (%s%s)%s" % (r["name"], r.get("source") or "Slack",
                                     ", %g min" % r["minutes"] if r.get("minutes") else "",
                                     ": " + r["note"] if r.get("note") else "") for r in clips]
    if links:
        lines += ["", "Shared links on this thread. A walkthrough video shared this way is read by walkthrough_video "
                  "(the same as a video posted here); a link to anything else cannot be opened yet:"]
        lines += ["- %s (%s)" % (r["name"], r.get("source") or "shared link") for r in links]
    if frames:
        if any(not r.get("read") for r in frames):
            lines += ["", "Frames pulled from the walkthrough video, not read yet. Open them with Read like photos, "
                      "write the walk's photo_notes from them (one per room, naming the frame), then run underwrite "
                      "with that walk:"]
        else:
            lines += ["", "Frames from the walkthrough video, already read by walkthrough_video. Open one only "
                      "when you are asked about that room:"]
        # Listed on every later turn on the thread, so the list is kept short; the rest sit beside these.
        lines += ["- %s%s" % (r["path"], " (%s)" % r["label"] if r.get("label") else "") for r in frames[:24]]
        if len(frames) > 24:
            lines.append("- and %d more frames in the same folder" % (len(frames) - 24))
    if failed:
        lines += ["", "Files you could not open (tell the operator why, in one line):"]
        lines += ["- %s: %s" % (r["name"], r["why"]) for r in failed[:10]]
        if len(failed) > 10:
            lines.append("- and %d more" % (len(failed) - 10))
    if told:
        lines += ["", "Videos the thread was already told about (do not say it again unless asked):"]
        lines += ["- %s: %s" % (r["name"], r["why"]) for r in told[:10]]
    if dropped:
        lines.append("%d more files were posted than the %d kept for one thread." % (dropped, config.MAX_FILES))
    return lines


# ------------------------------------------------------------------------ downloading a video

_CD = re.compile(r"filename\*\s*=\s*(?:UTF-8'')?\"?([^\";]+)\"?|filename\s*=\s*\"([^\"]+)\"|filename\s*=\s*([^;]+)", re.I)


class _Stop(Exception):
    """A download stopped on purpose (too big, out of room), with the reason in plain words."""


def _free(path: Path) -> int:
    if DISK is not None:
        return int(DISK(path))
    try:
        return int(shutil.disk_usage(str(path)).free)
    except OSError:
        return 1 << 62


free_bytes = _free


def _ext(ctype: str, name: str) -> str:
    base = (ctype or "").split(";")[0].strip().lower()
    if base in VIDEO_MIME:
        return VIDEO_MIME[base]
    suffix = Path(name or "").suffix.lower()
    return suffix if suffix.lstrip(".") in VIDEO_EXT else ".mp4"


def _cd_name(value: str) -> str:
    m = _CD.search(value or "")
    if not m:
        return ""
    return unquote(next(g for g in m.groups() if g)).strip().strip('"')


def _too_large(n: int, limit_mb: float, slack: bool) -> str:
    if slack:
        return ("it is %s, more than the %d MB a video posted in Slack can be: put it in Google Drive or Dropbox, "
                "share it so anyone with the link can view, and paste the link" % (size_text(n), int(limit_mb)))
    return "it is %s, more than the %d MB the box takes from a link" % (size_text(n), int(limit_mb))


def _room_left(free: int) -> str:
    return ("the box has %s free on that disk, and a download may not take it below %d MB" %
            (size_text(max(free, 0)), int(disk_floor_mb())))


def _stream(get, url: str, headers: dict, dest_dir: Path, limit_mb: float, *, slack: bool, name: str = "") -> tuple[str, Path | None, str]:
    """(why, path, the file name the server gave). Streamed to video.part in chunks, never held in
    memory (the brain has 2 GB and a phone video can be most of that), and renamed only when whole,
    so a broken download is never mistaken for a video later."""
    limit, floor = int(limit_mb * MB), int(disk_floor_mb() * MB)
    try:
        r = get(url, headers=headers, timeout=(20, 120), stream=True)
    except Exception as exc:  # noqa: BLE001  (requests, timeouts, anything)
        return ("Slack could not be reached (%s)" if slack else "the link could not be reached (%s)") % type(exc).__name__, None, ""
    part = Path(dest_dir) / "video.part"
    try:
        status = int(getattr(r, "status_code", 200) or 200)
        hdrs = {str(k).lower(): str(v) for k, v in dict(getattr(r, "headers", None) or {}).items()}
        ctype = hdrs.get("content-type", "").lower()
        if "text/html" in ctype or (not slack and status in (401, 403)):
            if slack:
                slack_app.forget_scopes()
                return SCOPE_HELP, None, ""
            return NOT_SHARED, None, ""
        if status == 404 and not slack:
            return "the link answered 404: the file may have been moved or deleted", None, ""
        if status != 200:
            return ("Slack answered %s" if slack else "the link answered %s") % status, None, ""
        length = int(float(hdrs.get("content-length") or 0))
        if length > limit:
            return _too_large(length, limit_mb, slack), None, ""
        free = _free(dest_dir)
        if length and free - length < floor:
            return _room_left(free), None, ""
        written, checked = 0, 0
        with open(part, "wb") as out:
            for chunk in r.iter_content(chunk_size=MB):
                if not chunk:
                    continue
                written += len(chunk)
                if written > limit:
                    raise _Stop(_too_large(written, limit_mb, slack))
                if written - checked >= 64 * MB:
                    checked = written
                    free = _free(dest_dir)
                    if free < floor:
                        raise _Stop(_room_left(free))
                out.write(chunk)
        if not written:
            return "it came back empty", None, ""
        given = _cd_name(hdrs.get("content-disposition", ""))
        path = Path(dest_dir) / ("video" + _ext(ctype, given or name))
        os.replace(part, path)
        return "", path, given
    except _Stop as stop:
        return str(stop), None, ""
    except Exception as exc:  # noqa: BLE001  (a broken connection mid-download, a full disk)
        if getattr(exc, "errno", None) == errno.ENOSPC:
            return "the box ran out of room saving it", None, ""
        return "the download broke off (%s)" % type(exc).__name__, None, ""
    finally:
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass
        part.unlink(missing_ok=True)


def save_video(f: dict, dest_dir: Path) -> dict:
    """Download one video, a Slack file or a share link, into dest_dir (a work folder outside the
    volume). {"path": Path or None, "why": "", "name": the file's name, "not_video": bool}. A video
    already downloaded into dest_dir (an earlier ask on the same video) is reused, not fetched again."""
    out = {"path": None, "why": "", "name": str(f.get("name") or "video"), "not_video": False}
    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        out["why"] = "the work folder could not be made (%s)" % type(exc).__name__
        return out
    named = dest_dir / "name.txt"
    have = sorted(p for p in dest_dir.glob("video.*") if p.suffix != ".part" and p.is_file() and p.stat().st_size > 0)
    if have:
        out["path"] = have[0]
        if named.is_file():
            out["name"] = named.read_text(encoding="utf-8").strip() or out["name"]
        return out
    if f.get("kind") == "link":
        if f.get("folder"):
            out["why"] = FOLDER_WHY
            return out
        if not f.get("fetch"):
            out["why"] = "that link cannot be downloaded"
            return out
        why, path, given = _stream(WEB.get, f["fetch"], {"User-Agent": UA}, dest_dir, link_mb(), slack=False)
    else:
        if f.get("needs_info") or not f.get("url"):
            full = slack_app.file_info(f.get("id") or "")
            if full:
                f = brief(full, f.get("user", ""), f.get("ts", ""))
                out["name"] = f["name"]
        if f.get("kind") != "video":
            out.update(why="it is not an mp4, mov or m4v video", not_video=True)
            return out
        if _too_big(f):
            out["why"] = _too_large(int(f.get("size") or 0), video_mb(), True)
            return out
        if not (f.get("url") and config.SLACK_BOT_TOKEN):
            out["why"] = "Slack gave no way to download it"
            return out
        if not slack_app.can_read_files():
            out["why"] = SCOPE_HELP
            return out
        # The module's own transport, streamed: slack_app.fetch reads a whole file into memory, which
        # is right for a photo preview and wrong for a phone video.
        why, path, given = _stream(slack_app._http.get, f["url"], {"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN},
                                   dest_dir, video_mb(), slack=True, name=out["name"])
    if why:
        out["why"] = why
        return out
    if given:
        out["name"] = given
    try:
        named.write_text(out["name"], encoding="utf-8")
    except OSError:
        pass
    out["path"] = path
    return out
