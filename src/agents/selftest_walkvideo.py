"""Selftest: walkthrough videos. attachments.py takes a video or a share link from Slack and fetches it
safely; the walkthrough_video workflow reads it into one repair number and keeps it for buyers.

Zero network: Slack, the share-link hosts, ffmpeg, the transcriber and the frame reader are fakes, and
the real transports are shut while the section runs, so a call that slipped past a fake fails loudly.
A "video" here is a few bytes that say how long it is and which invented narration it carries. Every
street, name and number is invented.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
import zipfile
from pathlib import Path

from . import config, store

CH = "CVIDEO"
ADDR = "77 Nowhere Test Ln, Knoxville, TN 37900"
NOTES = "3 bed 1 bath 1,200 sqft built 1948"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
MB = 1024 * 1024
DROPBOX_2 = "https://www.dropbox.com/scl/fi/zz2test/walk-part2.mov?rlkey=k2test&amp;dl=0"

# The narration each invented video carries, in seconds from its start.
SCRIPTS = {
    "walk1": {"rooms": [(5, "Front exterior"), (40, "Kitchen"), (130, "Hall bath"), (220, "Roof")],
              "said": [(5, "Okay, we are out front."), (42, "This is the kitchen, original cabinets."),
                       (95, "Seller said he would take 65k for it."), (132, "Soft floor right by the toilet."),
                       (222, "Up on the roof now.")],
              "findings": [(42, "Kitchen", "the cabinets are original and the counters are burned"),
                           (132, "Hall bath", "soft floor by the toilet"),
                           (222, "Roof", "the roof is about 20 years old"),
                           (226, "Roof", "a roofer quoted $8,500 to replace it")]},
    # The walker talks to the end of the clip, which is what lets a clock that runs long be seen and scaled.
    "walk2": {"rooms": [(4, "Laundry"), (60, "Back yard")],
              "said": [(4, "Laundry room."), (62, "Back yard, the fence is down."), (118, "That is the end of the walk.")],
              "findings": [(62, "Back yard", "the fence is down along the back")]},
    "long": {"rooms": [(i * 300 + 10, "Bedroom %d" % (i + 1)) for i in range(9)],
             "said": [(i * 300 + 12, "Bedroom number %d." % (i + 1)) for i in range(9)], "findings": []},
    "quiet": {"rooms": [], "said": [], "findings": []},
}
SEEN = {"Front exterior": {"room": "Front exterior", "condition": "worn", "shows": "Brick ranch with the gutters pulling away",
                           "findings": ["a sign in the yard reads $65,000"], "usable": True},
        "Kitchen": {"room": "Kitchen", "condition": "dated", "shows": "Original oak cabinets and laminate counters",
                    "findings": ["burn marks on the counter"], "usable": True},
        "Hall bath": {"room": "Hall bath", "condition": "damaged", "shows": "Vinyl floor lifting beside the toilet",
                      "findings": ["soft subfloor"], "usable": True},
        "Roof": {"room": "Roof", "condition": "unclear", "shows": "Only sky", "findings": [], "usable": False},
        "Laundry": "garbage",
        "Back yard": {"room": "Back yard", "condition": "worn", "shows": "Wood fence lying flat along the back",
                      "findings": [], "usable": True}}


def _video(seconds: float, script: str, audio: bool = True, has_video: bool = True) -> bytes:
    head = ("FAKEVIDEO|%.1f|%d|%s|%s|" % (seconds, 1 if audio else 0, script, "video" if has_video else "novideo")).encode()
    return head + b"0" * 256


def _info(path) -> list[bytes]:
    return Path(path).read_bytes().split(b"|")


class _Resp:
    def __init__(self, body=b"", status=200, ctype="video/quicktime", headers=None, data=None, length=True):
        self.status_code, self._body, self._d = status, body, data or {}
        self.headers = {"content-type": ctype, **({"content-length": str(len(body))} if length and body else {}),
                        **(headers or {})}

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), max(1, chunk_size)):
            yield self._body[i:i + chunk_size]

    def json(self):
        return self._d

    def close(self):
        return None


class _Net:
    """Slack's Web API and file URLs, and the share-link hosts, all invented. Every request is kept,
    headers and all, so a test can say which host saw which token."""

    def __init__(self):
        self.posts, self.gets, self.files, self.info, self.scopes = [], [], {}, {}, "chat:write,files:read"

    def post(self, url, json=None, timeout=None, headers=None, **kw):
        if url.endswith("chat.postMessage"):
            self.posts.append(dict(json or {}))
        elif url.endswith("auth.test"):
            return _Resp(ctype="application/json", headers={"x-oauth-scopes": self.scopes}, data={"ok": True})
        return _Resp(ctype="application/json", data={"ok": True, "ts": "1801000000.%06d" % len(self.posts)})

    def get(self, url, params=None, timeout=None, headers=None, stream=False, **kw):
        self.gets.append({"url": url, "headers": dict(headers or {}), "stream": stream})
        if url.endswith("files.info"):
            f = self.info.get((params or {}).get("file", ""))
            return _Resp(ctype="application/json", data={"ok": bool(f), "file": f or {}})
        if url in self.files:
            return self.files[url]
        return _Resp(b"not here", status=404, ctype="text/plain")


def _vfile(fid, name, size=3 * MB, mime="video/quicktime", **extra) -> dict:
    base = "https://files.example.test/%s" % fid
    f = {"id": fid, "name": name, "mimetype": mime, "filetype": name.rsplit(".", 1)[-1].lower(), "size": size,
         "url_private": base + "/orig", "url_private_download": base + "/download"}
    f.update(extra)
    return f


class _NoRecords:
    """DataSift for underwrite: the records search finds nothing, so the house is one not in DataSift yet."""

    def call(self, path, method="GET", body=None, headers=None):
        if path == "/api/internal/property/" and method == "POST":
            return {"count": 0, "results": []}
        raise AssertionError("unexpected DataSift call: %s %s" % (method, path))

    def record(self, u):
        return {}

    def custom_fields(self, u):
        return {}


@contextlib.contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_PHONE = re.compile(r"(?<![\d$])(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")


def _clean(text: str) -> bool:
    return chr(0x2014) not in text and chr(0x2013) not in text and not _PHONE.search(text)


def run(check, tmp) -> None:
    import urllib.request
    import requests
    from . import attachments, slack_app
    from .workflows import quick_screen as qs
    from .workflows import walkthrough_video as wv

    net = _Net()

    def shut(*a, **k):
        raise AssertionError("the walkthrough video selftest tried to reach the network")

    keep = {"db": config.DB_PATH, "ws": config.WORKSPACES_DIR, "token": config.SLACK_BOT_TOKEN, "mode": config.MODE,
            "cap": config.DAILY_CAP_USD, "settle": config.FILE_SETTLE_S, "max": config.MAX_FILES,
            "http": slack_app._http, "web": attachments.WEB, "disk": attachments.DISK,
            "urlopen": urllib.request.urlopen, "rget": requests.get, "rpost": requests.post, "crm": qs.CRM,
            "wv": (wv.PROBE, wv.AUDIO, wv.FRAME, wv.TRANSCRIBE, wv.VISION, wv.SAVE)}
    config.DB_PATH = Path(tmp) / "walkvideo.db"
    store.init()
    config.WORKSPACES_DIR, config.SLACK_BOT_TOKEN, config.FILE_SETTLE_S = Path(tmp) / "ws_walkvideo", "xoxb-test", 40.0
    config.DAILY_CAP_USD, config.MODE = 1000.0, "dry"
    slack_app._http, attachments.WEB, attachments.DISK = net, net, None
    urllib.request.urlopen, requests.get, requests.post = shut, shut, shut
    slack_app.forget_scopes()
    qs.CRM = _NoRecords()
    try:
        with _env(AGENTS_VIDEO_WORK_DIR=str(Path(tmp) / "video_work"), AGENTS_MAX_VIDEO_MB=None,
                  AGENTS_MAX_LINK_VIDEO_MB=None, AGENTS_VIDEO_DISK_FLOOR_MB=None, AGENTS_VIDEO_DRY_PAID=None,
                  AGENTS_VIDEO_MAX_MIN=None, AGENTS_VIDEO_MAX_FRAMES=None, AGENTS_VIDEO_RUN_CAP_USD=None,
                  AGENTS_VIDEO_DAILY_USD=None, AGENTS_VIDEO_ASK_MIN=None, AGENTS_VIDEO_ASK_USD=None,
                  AGENTS_VIDEO_KEEP_MB=None, AGENTS_VIDEO_KEEP_TOTAL_MB=None, AGENTS_VIDEO_CHUNK_MIN=None,
                  AGENTS_VIDEO_FRAME_EVERY_S=None, AGENTS_VIDEO_VISION_WORKERS="2", AGENTS_DEFAULT_REGION=None):
            _intake(check, net, attachments, slack_app)
            _downloads(check, tmp, net, attachments, slack_app)
            _workflow(check, tmp, net, attachments, qs, wv)
            _hygiene(check)
    finally:
        config.DB_PATH, config.WORKSPACES_DIR, config.SLACK_BOT_TOKEN = keep["db"], keep["ws"], keep["token"]
        config.MODE, config.DAILY_CAP_USD, config.FILE_SETTLE_S, config.MAX_FILES = keep["mode"], keep["cap"], keep["settle"], keep["max"]
        slack_app._http, attachments.WEB, attachments.DISK = keep["http"], keep["web"], keep["disk"]
        urllib.request.urlopen, requests.get, requests.post = keep["urlopen"], keep["rget"], keep["rpost"]
        qs.CRM = keep["crm"]
        wv.PROBE, wv.AUDIO, wv.FRAME, wv.TRANSCRIBE, wv.VISION, wv.SAVE = keep["wv"]
        slack_app.forget_scopes()


# ------------------------------------------------------------------------------- intake

def _intake(check, net, attachments, slack_app) -> None:
    from . import intake, team
    print("  videos and share links at intake")
    roster = team.load()

    def say(text, ts, *, thread="", files=None, subtype=""):
        ev = {"type": "message", "channel": CH, "ts": ts, "text": text, "user": "UBOSS"}
        if thread:
            ev["thread_ts"] = thread
        if files:
            ev["files"] = files
        if subtype:
            ev["subtype"] = subtype
        return intake.handle_body({"event": ev, "event_id": "Ev" + ts.replace(".", "")}, False, lambda: roster)

    kind = attachments.kind_of
    check(kind({"name": "walk.MOV", "mimetype": "video/quicktime"}) == "video" and kind({"name": "a.mp4"}) == "video"
          and kind({"name": "b.m4v", "mimetype": "video/x-m4v"}) == "video",
          "an mp4, mov or m4v posted in Slack is taken as a video")
    check(kind({"name": "c.webm", "mimetype": "video/webm"}) == "other" and kind({"name": "d.jpg"}) == "photo",
          "a video in another format is not taken as a walkthrough video, and a photo is still a photo")

    text = ("walk is here <https://drive.google.com/file/d/1TestFileIdAbc123/view?usp=sharing> and <%s|part 2> plus "
            "<https://docs.google.com/document/d/1TestDocIdAbc123/edit> <https://www.example.com/listing> and again "
            "<https://drive.google.com/file/d/1TestFileIdAbc123/view?usp=drive_link>" % DROPBOX_2)
    got = attachments.from_event({"user": "UBOSS", "ts": "1.1", "text": text, "files": [_vfile("V1", "walk1.mov")]})
    links = [g for g in got if g["kind"] == "link"]
    check([g["kind"] for g in got] == ["video", "link", "link"],
          "a message's video and its Google Drive and Dropbox links are recorded; a Google Doc or another site is not")
    drive = next(g for g in links if g["host"] == "drive")
    box = next(g for g in links if g["host"] == "dropbox")
    check(drive["fetch"] == "https://drive.usercontent.google.com/download?id=1TestFileIdAbc123&export=download&confirm=t",
          "a Google Drive file link is fetched from its direct download, past the large-file warning page")
    check("dl=1" in box["fetch"] and "rlkey=k2test" in box["fetch"] and "&amp;" not in box["fetch"] and box["name"] == "walk-part2.mov",
          "a Dropbox link keeps its access key, has the escaped ampersand undone, and is fetched with dl=1")
    check(len([g for g in links if g["host"] == "drive"]) == 1, "the same Drive file linked twice in one message is one record")
    folders = attachments.links_from_text("<https://drive.google.com/drive/folders/1TestFolderIdXy9> "
                                          "<https://www.dropbox.com/scl/fo/abc/defg?rlkey=r1&amp;dl=0>")
    check(len(folders) == 2 and all(f["folder"] and not f["fetch"] for f in folders),
          "a shared folder is recorded as a folder with nothing to fetch")

    r = say("Roma, rehab from this walkthrough", "1801.1", files=[_vfile("V1", "walk1.mov")], subtype="file_share")
    tid = r.get("queued") or 0
    task = store.task(tid) or {}
    check(r.get("agent") == "sales" and task.get("not_before", "") > store.now()
          and [f["kind"] for f in attachments.on_thread(CH, "1801.1")] == ["video"],
          "a video addressed to an employee is recorded on the thread and its turn waits for more parts")
    r2 = say("<%s>" % DROPBOX_2, "1801.2", thread="1801.1")
    check(r2.get("joined") == tid and [f["kind"] for f in attachments.on_thread(CH, "1801.1")] == ["video", "link"],
          "a share link posted on the same thread joins the waiting turn as another part of the walk")

    n = len(net.posts)
    say("Roma, here is the walk", "1802.1", files=[_vfile("V9", "huge.mov", size=900 * MB)], subtype="file_share")
    said = [p for p in net.posts[n:]]
    check(len(said) == 1 and "huge.mov" in said[0]["text"] and "Google Drive or Dropbox" in said[0]["text"]
          and said[0].get("thread_ts") == "1802.1",
          "a video too large to take from Slack is said in the thread, plainly, with what to do instead")
    say("", "1802.2", thread="1802.1", files=[_vfile("V10", "porch.jpg", mime="image/jpeg")], subtype="file_share")
    check(len(net.posts) == n + 1, "the too-large video is said once, not again on the next message")
    check(all(chr(0x2014) not in p.get("text", "") and chr(0x2013) not in p.get("text", "") for p in said),
          "what intake says about a video carries no long dash")
    n = len(net.posts)
    say("Roma, walk video <https://drive.google.com/file/d/1BigFileLinkId99/view>", "1803.1",
        files=[_vfile("V11", "big2.mov", size=900 * MB)], subtype="file_share")
    check(len(net.posts) == n, "a too-large video posted with a share link is not called a problem: the link is read instead")

    net.scopes = "chat:write"
    slack_app.forget_scopes()
    n = len(net.posts)
    say("Roma, the walk <https://drive.google.com/file/d/1OnlyLinkThread77/view>", "1804.1")
    check(not any("files:read" in p.get("text", "") for p in net.posts[n:]),
          "a thread holding only a share link is never told the app lacks files:read: a link needs no Slack permission")
    net.scopes = "chat:write,files:read"
    slack_app.forget_scopes()

    keep = config.MAX_FILES
    config.MAX_FILES = 2
    attachments.add(CH, "1805.1", attachments.links_from_text(
        "<https://drive.google.com/file/d/1LinkNumberOne111/view> <https://drive.google.com/file/d/1LinkNumberTwo222/view> "
        "<https://drive.google.com/file/d/1LinkNumberThree3/view>"))
    check(len(attachments.on_thread(CH, "1805.1")) == 2 and attachments.over(CH, "1805.1") == 1,
          "share links count toward the per-thread file limit like any file")
    config.MAX_FILES = keep

    # ------------------------------------------------------- the turn: listed, never saved into the workspace
    ws = Path(config.WORKSPACES_DIR) / "sales"
    ws.mkdir(parents=True, exist_ok=True)
    n = len(net.gets)
    rows = attachments.fetch_for_turn(store.task(tid), ws)
    check(len(rows) == 2 and all(r.get("video") and not r["path"] and not r["why"] for r in rows),
          "a video and a share link are listed for the employee and never saved into the workspace")
    check(not [g for g in net.gets[n:] if "/V1/" in g["url"] or "dropbox" in g["url"]],
          "starting a turn downloads no video and opens no share link")
    check(all({"name", "kind", "path", "why"} <= set(r) for r in rows) and all(r["kind"] in ("video", "link") for r in rows),
          "the rows keep the shape other workflows read (name, kind, path, why), so a scan reader skips a video")
    prompt = "\n".join(attachments.prompt_lines(rows))
    check("Do not open them with Read" in prompt and "walkthrough_video" in prompt and "walk1.mov" in prompt
          and "Shared links on this thread" in prompt and "walk-part2.mov" in prompt,
          "the employee is told the video is read by walkthrough_video, not opened with Read")
    big = attachments.fetch_for_turn({"slack_channel": CH, "slack_thread": "1802.1"}, ws)
    prompt = "\n".join(attachments.prompt_lines(big))
    huge = next(r for r in big if r["name"] == "huge.mov")
    check(huge.get("told") and "already told" in huge["why"] and "already told about" in prompt
          and "huge.mov" not in prompt.split("Videos the thread was already told about")[0],
          "a video the thread was already told about is listed apart, so the employee does not say it again")
    other = attachments.fetch_for_turn({"slack_channel": CH, "slack_thread": "1803.1"}, ws)
    big2 = next(r for r in other if r["name"] == "big2.mov")
    check(not big2["why"] and "share link on this thread is read instead" in big2.get("note", ""),
          "a too-large video with a share link beside it is listed as read from the link instead")
    attachments.add(CH, "1806.1", attachments.links_from_text("<https://drive.google.com/drive/folders/1TestFolderIdXy9>"))
    fold = attachments.fetch_for_turn({"slack_channel": CH, "slack_thread": "1806.1"}, ws)
    prompt = "\n".join(attachments.prompt_lines(fold))
    check(fold and "shared folder" in fold[0]["why"] and "Files you could not open" in prompt,
          "a shared folder is listed as a file that cannot be opened, with why, for the employee to say once")
    store.finish(tid, "done", {"text": "ok"})


# ------------------------------------------------------------------------------- downloads

def _downloads(check, tmp, net, attachments, slack_app) -> None:
    print("  downloading a video safely")
    v1 = attachments.brief(_vfile("V1", "walk1.mov"))
    body = _video(300, "walk1")
    net.files["https://files.example.test/V1/download"] = _Resp(body, ctype="video/quicktime")
    got = attachments.save_video(v1, Path(tmp) / "sv1")
    req = [g for g in net.gets if g["url"].endswith("/V1/download")]
    check(got["path"] and Path(got["path"]).read_bytes() == body and Path(got["path"]).suffix == ".mov",
          "a video posted in Slack is downloaded whole into the work folder")
    check(req and req[-1]["stream"] and req[-1]["headers"].get("Authorization") == "Bearer xoxb-test",
          "a Slack video is streamed with the bot token, never read whole into memory")
    n = len(net.gets)
    again = attachments.save_video(v1, Path(tmp) / "sv1")
    check(again["path"] == got["path"] and len(net.gets) == n, "a video already downloaded for an earlier ask is reused, not fetched again")

    box = attachments.links_from_text("<%s>" % DROPBOX_2)[0]
    net.files[box["fetch"]] = _Resp(_video(120, "walk2"), ctype="video/mp4",
                                    headers={"content-disposition": 'attachment; filename="walk-part2.mov"'})
    got = attachments.save_video(box, Path(tmp) / "sv2")
    req = [g for g in net.gets if g["url"] == box["fetch"]]
    check(got["path"] and req and "Authorization" not in req[-1]["headers"],
          "a share link is fetched with no Slack token: the bot token never reaches Google or Dropbox")
    check(got["name"] == "walk-part2.mov" and (Path(tmp) / "sv2" / "name.txt").is_file(),
          "the file name the host gives is kept for the index")

    drive = attachments.links_from_text("<https://drive.google.com/file/d/1PrivateFileIdQq7/view>")[0]
    net.files[drive["fetch"]] = _Resp(b"<html>Sign in</html>", ctype="text/html; charset=utf-8")
    got = attachments.save_video(drive, Path(tmp) / "sv3")
    check(not got["path"] and "anyone with the link" in got["why"] and not list((Path(tmp) / "sv3").glob("video*")),
          "a link that opens a sign-in page is refused with how to share it, and nothing half-saved is kept")
    n = len(net.gets)
    folder = attachments.links_from_text("<https://drive.google.com/drive/folders/1TestFolderIdXy9>")[0]
    got = attachments.save_video(folder, Path(tmp) / "sv4")
    check(not got["path"] and "folder" in got["why"] and len(net.gets) == n,
          "a shared folder is refused without a request")
    big = attachments.brief(_vfile("V9", "huge.mov", size=900 * MB))
    got = attachments.save_video(big, Path(tmp) / "sv5")
    check(not got["path"] and "Google Drive or Dropbox" in got["why"] and len(net.gets) == n,
          "an oversized Slack video without a link is refused with a reason, before any download")
    with _env(AGENTS_MAX_LINK_VIDEO_MB="0.0001"):
        stream = attachments.links_from_text("<https://drive.google.com/file/d/1StreamTooBig55/view>")[0]
        net.files[stream["fetch"]] = _Resp(b"x" * 400, ctype="video/mp4", length=False)
        got = attachments.save_video(stream, Path(tmp) / "sv6")
        check(not got["path"] and "more than" in got["why"] and not list((Path(tmp) / "sv6").glob("video*")),
              "a link that turns out larger than its cap is stopped mid-download and the partial file is removed")
    attachments.DISK = lambda p: 50 * MB
    roomy = attachments.links_from_text("<https://drive.google.com/file/d/1DiskFloorIdRr4/view>")[0]
    net.files[roomy["fetch"]] = _Resp(_video(60, "quiet"), ctype="video/mp4")
    got = attachments.save_video(roomy, Path(tmp) / "sv7")
    check(not got["path"] and "free" in got["why"], "a download that would take the disk below its floor is refused")
    attachments.DISK = None
    net.files["https://files.example.test/V30/download"] = _Resp(b"<html>sign in</html>", ctype="text/html")
    got = attachments.save_video(attachments.brief(_vfile("V30", "deck.mov")), Path(tmp) / "sv8")
    check(not got["path"] and "files:read" in got["why"], "a Slack sign-in page in place of the video is caught as a missing permission")
    net.info["V31"] = _vfile("V31", "stub.mov")
    net.files["https://files.example.test/V31/download"] = _Resp(_video(30, "quiet"), ctype="video/quicktime")
    got = attachments.save_video({"id": "V31", "name": "V31", "kind": "other", "needs_info": True, "url": ""}, Path(tmp) / "sv9")
    check(got["path"] and got["name"] == "stub.mov", "a video Slack sent only as a stub is completed through files.info and saved")
    net.info["V32"] = _vfile("V32", "plan.pdf", mime="application/pdf")
    got = attachments.save_video({"id": "V32", "name": "V32", "kind": "other", "needs_info": True, "url": ""}, Path(tmp) / "sv10")
    check(got["not_video"] and not got["path"], "a stub that turns out not to be a video is set aside quietly")


# ------------------------------------------------------------------------------- the workflow

def _workflow(check, tmp, net, attachments, qs, wv) -> None:
    from .workflows import underwrite as uw
    print("  the walkthrough video workflow")
    calls = {"t": 0, "v": 0, "f": 0, "a": []}
    fail = {"t": False}
    drift = {}

    def probe(path):
        bits = _info(path)
        if bits[0] != b"FAKEVIDEO":
            raise wv.VideoError("ffmpeg could not read it")
        return {"seconds": float(bits[1]), "audio": bits[2] == b"1", "video": bits[4] == b"video"}

    def audio(path, start, length):
        calls["a"].append((start, length))
        return ("MP3|%s|%.1f|%.1f" % (_info(path)[3].decode(), start, length)).encode()

    def frame(path, at, out):
        calls["f"] += 1
        if at < float(_info(path)[1]):
            out.write_bytes(JPEG)

    def transcribe(mp3, prompt):
        calls["t"] += 1
        if fail["t"]:
            raise RuntimeError("the audio model is down")
        _, key, start, length = mp3.decode().split("|")
        start, length, sc, k = float(start), float(length), SCRIPTS[key], drift.get(key, 1.0)

        def stamp(t):
            s = int(round((t - start) * k))
            return "[%d:%02d]" % (s // 60, s % 60)

        inside = [x for x in sc["said"] if start <= x[0] < start + length]
        lines = ["## TRANSCRIPT"] + ["%s %s" % (stamp(t), x) for t, x in inside]
        lines += ["**ROOMS**"] + ["%s %s" % (stamp(t), r) for t, r in sc["rooms"] if start <= t < start + length]
        found = ["%s %s | %s" % (stamp(t), r, x) for t, r, x in sc["findings"] if start <= t < start + length]
        lines += ["## FINDINGS"] + (found or ["none"])
        return "\n".join(lines), {"cost": 0.0015}

    def vision(jpeg, prompt):
        calls["v"] += 1
        m = re.search(r"they were in: (.+?)\. Around", prompt)
        ans = SEEN.get(m.group(1) if m else "", {"room": "", "condition": "unclear", "shows": "A plain wall",
                                                 "findings": [], "usable": True})
        if ans == "garbage":
            return "I cannot tell from this frame.", 900, 40
        return json.dumps(ans), 1200, 120

    wv.PROBE, wv.AUDIO, wv.FRAME, wv.TRANSCRIBE, wv.VISION, wv.SAVE = probe, audio, frame, transcribe, vision, None

    def task(inputs, thread, details=""):
        tid, _ = store.enqueue("sales", "walkthrough_video", "", subject="", force=True,
                               payload={"inputs": inputs, "details": details}, source="agent:sales",
                               slack_channel=CH, slack_thread=thread)
        return store.task(tid)

    def post_video(thread, fid, name, seconds, script, **kw):
        body = _video(seconds, script, **{k: v for k, v in kw.items() if k in ("audio", "has_video")})
        net.files["https://files.example.test/%s/download" % fid] = _Resp(body, ctype="video/quicktime")
        attachments.add(CH, thread, [attachments.brief(_vfile(fid, name, size=len(body)))])

    def snap():
        return dict(t=calls["t"], v=calls["v"], f=calls["f"], g=len(net.gets))

    # ------------------------------------------------------------------ refusals before anything is fetched
    r = wv.run(task({"address": ADDR, "notes": NOTES}, "1901.1"))
    check(not r["ok"] and "mp4, mov or m4v" in r["why"] and "Google Drive or Dropbox" in r["why"],
          "with no video on the thread the run says what to post, in plain words")
    post_video("1902.1", "W1", "walk1.mov", 240, "walk1")
    before = snap()
    r = wv.run(task({"address": "99 Nosuch Test Rd", "notes": ""}, "1902.1"))
    check(not r["ok"] and "square footage" in r["why"] and snap() == before,
          "a house that cannot be priced is refused before the video is downloaded or anything is paid for")
    r = wv.run(task({"address": ADDR, "notes": NOTES, "video": "nothere.mov"}, "1902.1"))
    check(not r["ok"] and "no video named nothere.mov" in r["why"], "a video named in the ask that is not on the thread is refused")

    # ------------------------------------------------------------------ dry: the free steps, nothing paid
    config.MODE = "dry"
    before = snap()
    t = task({"address": ADDR, "notes": NOTES}, "1902.1")
    r = wv.run(t)
    check(r["ok"] and r["mode"] == "dry" and calls["t"] == before["t"] and calls["v"] == before["v"],
          "a dry box does the free steps and transcribes nothing and reads no frame")
    check(r["spent_usd"] == 0 and store.spent(task_id=t["id"]) == 0, "a dry run spends nothing")
    check(r["estimate"]["usd"] > 0 and r["estimate"]["frames"] > 0, "a dry run says what reading the video would cost")
    files = Path(r["run_dir"]) / "files"
    kept = [f for f in r["files"] if f.endswith(".mov")]
    check(kept and (files / kept[0]).is_file(), "a small video posted in Slack is kept with the run for buyers")
    frames = attachments.frames_on_thread(CH, "1902.1")
    ws = Path(config.WORKSPACES_DIR) / "sales"
    check(r["frames_saved"] == len(frames) > 0 and all(not f["read"] and (ws / f["path"]).is_file() for f in frames),
          "on a dry box the frames are put in the employee's workspace for this thread, marked not read")
    rows = attachments.fetch_for_turn({"slack_channel": CH, "slack_thread": "1902.1"}, ws)
    prompt = "\n".join(attachments.prompt_lines(rows))
    check([x for x in rows if x["kind"] == "frame"] and "not read yet" in prompt and "photo_notes" in prompt,
          "the employee's next turn lists the frames to read like photos and write the walk from")
    text = wv.render(t, r)
    check("dry mode" in text and "$" in text and "AGENTS_VIDEO_DRY_PAID" in text and _clean(text),
          "the dry post says nothing was read, what reading costs, and how a dry box can read")

    # ------------------------------------------------------------------ live: read, priced by underwrite
    config.MODE = "live"
    before = snap()
    t = task({"address": ADDR, "notes": NOTES}, "1902.1")
    r = wv.run(t)
    check(r["ok"] and r["mode"] == "read" and calls["t"] == before["t"] + 1 and calls["v"] > before["v"],
          "a short video on a live box is transcribed in one chunk and its frames are read, with no ask first")
    check(len(net.gets) == before["g"], "the video downloaded for the dry run is reused, not downloaded again")
    check(r["rooms"] == ["Front exterior", "Kitchen", "Hall bath", "Roof"], "the rooms come from the narration, in order")
    walk = json.loads((Path(r["run_dir"]) / "files" / r["walk_file"]).read_text(encoding="utf-8"))
    notes = walk["photo_notes"]
    frame_notes = [n for n in notes if n["file"].endswith(".jpg")]
    said_notes = [n for n in notes if "said on the walk" in n["file"]]
    check(frame_notes and any(n["file"].startswith("v1-0m42s") for n in frame_notes),
          "a frame is pulled two seconds into each spoken room change")
    check(any(n["room"] == "Kitchen" and n["condition"] == "dated" for n in frame_notes)
          and not any(n["room"] == "Roof" and n["file"].endswith(".jpg") for n in frame_notes),
          "each usable frame becomes a photo_note with its room and condition; a frame the reader calls unusable does not")
    check(any("20 years old" in n["finding"] and "3:42" in n["file"] for n in said_notes),
          "a condition said aloud is kept in the walk with its time")
    quote = next(n for n in said_notes if "8,500" in n["finding"])
    check("not priced" in quote["condition"] and not walk.get("flags") and r["said_money"],
          "a dollar figure said on the walk is a note marked not priced, never a priced flag")
    check(r["money_at"] and any("1:35" in x for x in r["money_at"]),
          "a dollar figure in the narration is named with its time, so someone checks the video before a buyer sees it")
    ur = r.get("underwrite") or {}
    check(ur.get("ok") and ur.get("mode") == "rehab" and ur.get("rehab", 0) > 0, "underwrite builds the rehab from the video's walk")
    direct = uw.run(task({"address": ADDR, "notes": NOTES, "walk": json.dumps(walk)}, "1902.9"))
    check(direct["ok"] and direct["rehab"] == ur["rehab"],
          "the repair number is underwrite's own figure for the walk the video produced, not a second calculation")
    out = Path(r["run_dir"]) / "files"
    check(all((out / f).is_file() for f in ur["files"]), "underwrite's workbook is kept next to this run's files, where the Slack link finds it")
    md = (out / next(f for f in r["files"] if f.endswith("_Walkthrough_Index.md"))).read_text(encoding="utf-8")
    check("Kitchen" in md and "0:42" in md and "Hall bath" in md and "Back yard" not in md,
          "the buyer index lists each room with its time and what it shows")
    check("$" not in md and "65" not in md and "seller" not in md.lower().replace("about price, the deal or the seller", ""),
          "the buyer index carries no dollar figure and nothing said about the seller, even when a frame or the narration has it")
    check("Said on the walk: soft floor by the toilet" in md, "a plain condition said aloud is in the buyer index")
    tr = (out / next(f for f in r["files"] if f.endswith("_Walkthrough_Transcript.md"))).read_text(encoding="utf-8")
    check("65k" in tr and "not for buyers" in tr, "the narration is kept for the team, marked not for buyers")
    zname = next(f for f in r["files"] if f.endswith("_Frames.zip"))
    with zipfile.ZipFile(out / zname) as z:
        check(sorted(z.namelist()) == sorted(n["file"] for n in frame_notes), "the frames zip holds the frames the walk used")
    check(store.spent(task_id=t["id"]) > 0 and r["video_usd"] > 0 and float(store.kv_get("video:usd:%s" % store.today()) or 0) > 0,
          "what the read cost is booked to the task and to today's video budget")
    frames = attachments.frames_on_thread(CH, "1902.1")
    check(frames and all(f["read"] for f in frames) and len(frames) == len(frame_notes),
          "after a read the employee's frames are the usable ones, marked already read")
    prompt = "\n".join(attachments.prompt_lines(attachments.fetch_for_turn({"slack_channel": CH, "slack_thread": "1902.1"}, ws)))
    check("already read by walkthrough_video" in prompt and "not read yet" not in prompt,
          "the employee is told the frames were read, to open one only when asked about that room")
    text = wv.render(t, r)
    check(_clean(text) and "77 Nowhere Test Ln" in text and "Rehab" in text and "not priced" in text and "1:35" in text,
          "the post gives the house, the repair number, the not-priced quote and when money is said")
    check("Room index for buyers" in text and "team only" in text, "the post links the buyer index and the team's transcript")

    # ------------------------------------------------------------------ two parts, a plan, drift
    post_video("1903.1", "W2", "walk1.mov", 240, "walk1")
    box = attachments.links_from_text("<%s>" % DROPBOX_2)[0]
    net.files[box["fetch"]] = _Resp(_video(120, "walk2"), ctype="video/mp4")
    attachments.add(CH, "1903.1", [box])
    drift["walk2"] = 1.25
    r = wv.run(task({"address": ADDR, "notes": NOTES, "plan": "full gut"}, "1903.1"))
    drift.clear()
    md = (Path(r["run_dir"]) / "files" / next(f for f in r["files"] if f.endswith("_Index.md"))).read_text(encoding="utf-8")
    check(r["ok"] and len(r["parts"]) == 2 and "Video 1" in md and "Video 2" in md and "Back yard" in md,
          "a walk filmed in parts is read part by part, and the index names each video")
    walk = json.loads((Path(r["run_dir"]) / "files" / r["walk_file"]).read_text(encoding="utf-8"))
    yard = next(n for n in walk["photo_notes"] if n.get("room") == "Back yard" and n["file"].endswith(".jpg"))
    check(yard["file"].startswith("v2-1m02s"), "a narration clock that runs long is scaled back onto the video's real length")
    check(walk["single_scenario"]["key"] == "gut_t2" and walk["single_scenario"]["beds"] == 3,
          "a plan named in the ask is the engine's own plan, held to the house's layout")
    gut = uw.run(task({"address": ADDR, "notes": NOTES, "walk": json.dumps(walk)}, "1903.9"))
    check(gut["rehab"] == r["underwrite"]["rehab"] > ur["rehab"], "the full gut plan prices through underwrite, above the engine's mid plan")
    check(any("unreadable" in p for p in r["problems"]), "a frame the reader answered with no JSON is counted and left out")
    r = wv.run(task({"address": ADDR, "notes": NOTES, "plan": "palace"}, "1903.1"))
    check(any("not one the engine knows" in p for p in r["problems"]) and "single_scenario" not in json.loads(
        (Path(r["run_dir"]) / "files" / r["walk_file"]).read_text(encoding="utf-8")),
          "a plan the engine does not know is said, and the engine's one plan is used")
    r = wv.run(task({"address": ADDR, "notes": NOTES, "video": "walk-part2.mov"}, "1903.1"))
    check(r["ok"] and len(r["parts"]) == 1 and r["parts"][0]["name"] == "walk-part2.mov",
          "the video input picks one video on the thread by its name")
    check(abs(wv.parse_narration("## TRANSCRIPT\n[3:00] hall\n[6:00] end", 300)["said"][0][0] - 150.0) < 0.2,
          "parse_narration scales a stated clock that runs past the chunk back onto its real length")

    # ------------------------------------------------------------------ the estimate first, then go
    post_video("1904.1", "W3", "long.mov", 1200, "long")
    before = snap()
    t = task({"address": ADDR, "notes": NOTES}, "1904.1")
    r = wv.run(t)
    check(r["ok"] and r["mode"] == "asked" and r.get("needs_go") and calls["t"] == before["t"] and calls["v"] == before["v"],
          "a video longer than the ask limit gets the estimate first: nothing transcribed, no frame read")
    text = wv.render(t, r)
    check("Say go" in text and "$" in text and "20 min" in text and _clean(text), "the estimate says the length, the cost and to say go")
    downloads = len([g for g in net.gets if g["url"].endswith("/W3/download")])
    r = wv.run(task({"address": ADDR, "notes": NOTES, "go": "yes"}, "1904.1"))
    check(r["ok"] and r["mode"] == "read" and calls["t"] == before["t"] + 2
          and len([g for g in net.gets if g["url"].endswith("/W3/download")]) == downloads,
          "after go the same download is read, in ten-minute chunks")
    check(any(x.startswith("Bedroom 3") or x == "Bedroom 3" for x in r["rooms"]) and "Bedroom 3" in "".join(r["rooms"]),
          "a room said in the second chunk is placed at its real time in the video")

    with _env(AGENTS_VIDEO_MAX_FRAMES="5", AGENTS_VIDEO_MAX_MIN="30"):
        post_video("1905.1", "W4", "huge-walk.mov", 2700, "long")
        calls["a"].clear()
        r = wv.run(task({"address": ADDR, "notes": NOTES, "go": "yes"}, "1905.1"))
        check(r["ok"] and abs(r["heard_s"] - 1800) < 1 and sum(length for _s, length in calls["a"]) <= 1800.5,
              "one ask transcribes at most AGENTS_VIDEO_MAX_MIN minutes of video")
        check(r["frames_read"] <= 5, "one ask reads at most AGENTS_VIDEO_MAX_FRAMES frames")
        text = wv.render(task({}, "1905.1"), r)
        check(_clean(text), "the capped post is clean")

    # ------------------------------------------------------------------ the caps refuse, and say why
    with _env(AGENTS_VIDEO_RUN_CAP_USD="0.0001", AGENTS_VIDEO_ASK_USD="5"):
        before = snap()
        t = task({"address": ADDR, "notes": NOTES}, "1902.1")
        r = wv.run(t)
        check(r["ok"] and r["mode"] == "asked" and calls["t"] == before["t"] and calls["v"] == before["v"],
              "a short video whose estimate passes what one ask may spend is asked first, even under the ask limits")
        text = wv.render(t, r)
        check("AGENTS_VIDEO_RUN_CAP_USD" in text and "stop at the cap" in text and "Say go" in text,
              "the estimate says it passes the cap and that reading would stop there")
        t = task({"address": ADDR, "notes": NOTES, "go": "yes"}, "1902.1")
        r = wv.run(t)
        check(r["ok"] and calls["t"] == before["t"] and calls["v"] == before["v"] and store.spent(task_id=t["id"]) == 0,
              "with go and the run cap too low nothing is transcribed or read, and nothing is charged")
        check(any("would pass the cap" in s for s in r["skipped"]), "the run says the cap stopped the reading")
    day = "video:usd:%s" % store.today()
    used = store.kv_get(day)
    store.kv_set(day, "2.9999")
    before = snap()
    r = wv.run(task({"address": ADDR, "notes": NOTES}, "1902.1"))
    check(calls["t"] == before["t"] and any("AGENTS_VIDEO_DAILY_USD" in s for s in r["skipped"]),
          "today's video budget, once used, stops further reads and says so")
    store.kv_set(day, used or "0")

    # ------------------------------------------------------------------ what goes wrong, said once each
    fail["t"] = True
    before = snap()
    r = wv.run(task({"address": ADDR, "notes": NOTES}, "1902.1"))
    fail["t"] = False
    check(r["ok"] and calls["v"] > before["v"] and sum("could not be read" in p for p in r["problems"]) == 1,
          "when the narration cannot be transcribed it is said once and the frames are still read")
    post_video("1906.1", "W5", "good.mov", 60, "quiet")
    private = attachments.links_from_text("<https://drive.google.com/file/d/1PrivateWalkIdP0/view>")[0]
    net.files[private["fetch"]] = _Resp(b"<html>Sign in</html>", ctype="text/html")
    attachments.add(CH, "1906.1", [private])
    r = wv.run(task({"address": ADDR, "notes": NOTES}, "1906.1"))
    check(r["ok"] and len(r["parts"]) == 1 and sum("Google Drive link" in p for p in r["problems"]) == 1,
          "a link that cannot be fetched is said once, and the other video is still read")
    rows = attachments.fetch_for_turn({"slack_channel": CH, "slack_thread": "1906.1"}, ws)
    gone = next(x for x in rows if x.get("link"))
    check(gone.get("told") and "already told" in gone["why"], "a later turn lists the failed link as already told, so it is not said twice")
    post_video("1907.1", "W6", "audio-only.mov", 60, "quiet", has_video=False)
    r = wv.run(task({"address": ADDR, "notes": NOTES}, "1907.1"))
    check(not r["ok"] and any("no video" in p for p in r["problems"]), "a file with no picture in it is said plainly and not read")
    post_video("1908.1", "W7", "silent.mov", 60, "quiet", audio=False)
    r = wv.run(task({"address": ADDR, "notes": NOTES}, "1908.1"))
    check(r["ok"] and any("no sound" in p for p in r["problems"]) and r["frames_read"] > 0,
          "a video with no sound is read from its frames alone, and says there was no narration")

    # ------------------------------------------------------------------ keeping the video, and no house named
    with _env(AGENTS_VIDEO_KEEP_MB="0.0000001"):
        r = wv.run(task({"address": ADDR, "notes": NOTES}, "1906.1"))
        check(not [f for f in r["files"] if f.endswith(".mov")] and any("stays in the Slack thread" in k for k in r["kept_why"]),
              "a video over the keep size is not copied onto the volume; the index says the original stays in Slack")
    r = wv.run(task({"address": ADDR, "notes": NOTES}, "1903.1"))
    check(not any(p["kept"] for p in r["parts"] if p["link"]) and any(p["link"] for p in r["parts"]),
          "a share link is kept as the link, never copied onto the box")
    md = (Path(r["run_dir"]) / "files" / next(f for f in r["files"] if f.endswith("_Index.md"))).read_text(encoding="utf-8")
    check("shared link: https://www.dropbox.com/" in md, "the buyer index carries the share link dispo sends")
    r = wv.run(task({"address": "", "notes": ""}, "1902.1"))
    check(r["ok"] and r["mode"] == "read" and "underwrite" not in r and "no house was named" in r["no_number"]
          and any(f.endswith("_Index.md") for f in r["files"]),
          "with no house named the video is still read for the index and findings, with no repair number")

    # ------------------------------------------------------------------ the work folder
    root = Path(os.environ["AGENTS_VIDEO_WORK_DIR"])
    stale = root / "old-video"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "video.mov").write_bytes(b"x")
    past = time.time() - 30 * 3600
    os.utime(stale, (past, past))
    wv.run(task({"address": ADDR, "notes": NOTES}, "1901.1"))
    check(not stale.exists(), "a download older than AGENTS_VIDEO_WORK_HOURS is removed from the work folder")
    check(not list(root.glob("frames-*")), "the frames' scratch folder is removed after each run")


def _hygiene(check) -> None:
    print("  source hygiene")
    src = Path(__file__).resolve().parent
    for name in ("attachments.py", "workflows/walkthrough_video.py", "selftest_walkvideo.py"):
        text = (src / name).read_text(encoding="utf-8")
        check(chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text,
              "%s carries no long dash and no backspace byte" % name)
    wv = (src / "workflows" / "walkthrough_video.py").read_text(encoding="utf-8")
    at = (src / "attachments.py").read_text(encoding="utf-8")
    for name, text in (("walkthrough_video.py", wv), ("attachments.py", at)):
        check(not re.search(r"[\"']/api/(?:internal|v1|token)", text) and ".get_detail(" not in text
              and "C:/Users" not in text and "DATASIFT_PASSWORD" not in text,
              "%s calls no DataSift route, no SiftMap detail, no workstation path and no password" % name)
