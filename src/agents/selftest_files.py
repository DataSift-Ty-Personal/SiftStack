"""Selftest: files dropped in Slack (agents/attachments.py, intake, the turn's prompt). Zero network:
Slack is a fake that serves invented files; nothing here names a real person or place."""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
import zipfile
from pathlib import Path

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


class _Resp:
    def __init__(self, data=None, *, content=b"", ctype="application/json", status=200, headers=None):
        self._d, self.content, self.status_code = data or {}, content, status
        self.headers = {"content-type": ctype, **(headers or {})}

    def json(self):
        return self._d


class _Slack:
    """chat.postMessage, reactions.add, auth.test (with the scopes header), files.info and the
    private file URLs, all invented."""

    def __init__(self):
        self.posts, self.reactions, self.downloads, self.scopes = [], [], [], "chat:write,files:read"
        self.files: dict[str, _Resp] = {}
        self.info: dict[str, dict] = {}

    def post(self, url, json=None, timeout=None, headers=None):
        if url.endswith("chat.postMessage"):
            self.posts.append(dict(json or {}))
        elif url.endswith("reactions.add"):
            self.reactions.append(dict(json or {}))
        elif url.endswith("auth.test"):
            return _Resp({"ok": True}, headers={"x-oauth-scopes": self.scopes})
        return _Resp({"ok": True, "ts": "1900000000.%06d" % len(self.posts)})

    def get(self, url, params=None, timeout=None, headers=None):
        if url.endswith("files.info"):
            f = self.info.get((params or {}).get("file", ""))
            return _Resp({"ok": bool(f), "file": f or {}})
        if url in self.files:
            self.downloads.append(url)
            return self.files[url]
        return _Resp({"ok": False})


def _file(fid, name, mime="image/jpeg", thumb=True, size=2_000_000, **extra):
    base = "https://files.example.test/%s" % fid
    f = {"id": fid, "name": name, "mimetype": mime, "filetype": name.rsplit(".", 1)[-1], "size": size,
         "url_private": base + "/orig", "url_private_download": base + "/download"}
    if thumb:
        f["thumb_1024"] = base + "/thumb_1024.jpg"
    f.update(extra)
    return f


def run(check, tmp) -> None:
    from . import attachments, bus, config, intake, runner, slack_app, store, team

    fake = _Slack()
    keep = (slack_app._http, config.SLACK_BOT_TOKEN, config.WORKSPACES_DIR, config.FILE_SETTLE_S,
            config.MAX_FILE_MB, config.MAX_FILES, config.DB_PATH)
    # A database of its own: "nothing is ready to run" must not depend on what earlier sections left queued.
    config.DB_PATH = Path(tmp) / "files.db"
    store.init()
    slack_app._http, config.SLACK_BOT_TOKEN = fake, "xoxb-test"
    config.WORKSPACES_DIR, config.FILE_SETTLE_S = Path(tmp) / "ws_files", 40.0
    slack_app.forget_scopes()
    roster = team.load()
    try:
        def say(text, ts, *, thread="", files=None, user="UBOSS", subtype="", channel="CFILES", bot=""):
            ev = {"type": "message", "channel": channel, "ts": ts, "text": text, "user": user}
            if thread:
                ev["thread_ts"] = thread
            if files:
                ev["files"] = files
            if subtype:
                ev["subtype"] = subtype
            if bot:
                ev["bot_id"] = bot
            return intake.handle_body({"event": ev, "event_id": "Ev" + ts.replace(".", "")}, False, lambda: roster)

        check("files:read" in team.BOT_SCOPES and "files:read" in team.slack_manifest("https://x.test")["oauth_config"]["scopes"]["bot"],
              "the Slack app manifest asks for files:read, so an employee can open what is dropped in a thread")

        # ------------------------------------------------------------ intake: a burst becomes one turn
        r = say("Roma, rehab breakdown from these", "1900.1", files=[_file("F1", "kitchen.jpg"), _file("F2", "roof.jpg")],
                subtype="file_share")
        tid = r.get("queued") or 0
        task = store.task(tid) or {}
        check(r.get("agent") == "sales" and task.get("not_before", "") > store.now(),
              "a message with photos addressed to an employee is queued with a short wait, not started at once")
        check([f["id"] for f in attachments.on_thread("CFILES", "1900.1")] == ["F1", "F2"],
              "the photos are recorded on the thread the employee will answer in")
        check(store.claim_next() is None, "a turn still waiting for more photos is not claimed by the worker")
        r2 = say("", "1900.2", thread="1900.1", files=[_file("F3", "bath.jpg")], subtype="file_share")
        r3 = say("tier 2 finishes", "1900.3", thread="1900.1")
        task = store.task(tid) or {}
        check(r2.get("joined") == tid and r3.get("joined") == tid and len(store.on_slack_thread("CFILES", "1900.1")) == 1,
              "more photos and a follow-up on the same thread join the waiting turn instead of starting new ones")
        check("tier 2 finishes" in store.payload_of(task).get("text", "")
              and len(attachments.on_thread("CFILES", "1900.1")) == 3,
              "the waiting turn now carries the follow-up text and all three photos")
        check(say("edited", "1900.4", subtype="message_changed").get("ignored")
              and say("ours", "1900.5", bot="B1").get("ignored"),
              "an edit and the app's own posts are still not asks; only a person's file post or broadcast is")
        with store._db() as c:
            c.execute("UPDATE tasks SET not_before=? WHERE id=?", ("2000-01-01T00:00:00+00:00", tid))
        got = store.claim_next() or {}
        check(got.get("id") == tid, "once the wait has passed the worker takes the turn")
        store.finish(tid, "done", {"text": "ok"})
        r4 = say("Roma, and the garage?", "1900.6", thread="1900.1")
        check(r4.get("queued") and r4.get("queued") != tid, "after the turn has run, a new ask on the thread is a new turn")
        store.finish(r4["queued"], "done", {"text": "ok"})

        # ------------------------------------------------------- photos first, the ask second
        r = say("", "1901.1", files=[_file("F10", "front.jpg")], subtype="file_share")
        check(r.get("held") == 1 and not store.on_slack_thread("CFILES", "1901.1"),
              "photos posted with no ask are held for that person, and no employee is started")
        r = say("Roma, what would this rehab run?", "1901.2")
        check(r.get("queued") and [f["id"] for f in attachments.on_thread("CFILES", "1901.2")] == ["F10"]
              and not attachments.claim("CFILES", "UBOSS"),
              "that person's next ask in the channel takes the held photos, and they are not taken twice")
        store.finish(r["queued"], "done", {"text": "ok"})
        attachments.hold("CFILES", "UOTHER", [attachments.brief(_file("F11", "old.jpg"))])
        old = attachments._load(attachments._pending_key("CFILES", "UOTHER"))
        store.kv_set(attachments._pending_key("CFILES", "UOTHER"),
                     json.dumps([dict(f, held_at=time.time() - 31 * 60) for f in old]))
        check(attachments.claim("CFILES", "UOTHER") == [], "photos held longer than half an hour are not attached to a later ask")
        check(attachments.claim("CFILES", "USOMEONE") == [], "one person's held photos never go to someone else's ask")

        # ----------------------------------------------------------------- the worker saves them
        ws = config.WORKSPACES_DIR / "sales"
        ws.mkdir(parents=True, exist_ok=True)
        for fid in ("F1", "F2", "F3"):
            fake.files["https://files.example.test/%s/thumb_1024.jpg" % fid] = _Resp(content=JPEG, ctype="image/jpeg")
        task = store.task(tid)
        rows = attachments.fetch_for_turn(task, ws)
        check([r["path"].rsplit("/", 1)[-1] for r in rows] == ["01-kitchen.jpg", "02-roof.jpg", "03-bath.jpg"]
              and all((ws / r["path"]).read_bytes() == JPEG for r in rows),
              "each photo is saved into the employee's own workspace as Slack's 1024px preview")
        check(all(r["path"].startswith("inbox/") for r in rows) and all(runner._inside(ws, r["path"]) for r in rows),
              "the saved photos sit inside the workspace, where the Read guard lets the employee open them")
        n = len(fake.downloads)
        attachments.fetch_for_turn(task, ws)
        check(len(fake.downloads) == n, "a photo already saved for the thread is not downloaded again")
        guard = runner.make_guard({"Read"}, ws)
        inside = asyncio.run(guard({"tool_name": "Read", "tool_input": {"file_path": str(ws / rows[0]["path"])}}, "t", None))
        outside = asyncio.run(guard({"tool_name": "Read", "tool_input": {"file_path": str(Path(tmp) / "agents.db")}}, "t", None))
        check(inside == {} and outside.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
              "reading a saved photo is allowed and reading anything outside the workspace still is not")

        prompt = runner.prompt_for(task, roster, roster.employees["sales"], rows)
        check("inbox/" in prompt and "01-kitchen.jpg" in prompt and "every photo" in prompt,
              "the employee is told where each photo is and to look at every one before answering")
        check(runner.budget_now(files=True) == min(max(config.AGENT_BUDGET_USD, config.FILE_TURN_USD),
                                                   config.DAILY_CAP_USD - store.spent(day=store.today()))
              and runner.budget_now() <= config.AGENT_BUDGET_USD,
              "a turn carrying photos gets the photo cap; a plain turn keeps the plain cap")

        # ---------------------------------------------------------------- what can go wrong
        boundary = io.BytesIO()
        with zipfile.ZipFile(boundary, "w") as z:
            z.writestr("doc.kml", "<kml><coordinates>-83.9,35.9 -83.8,35.9 -83.8,36.0</coordinates></kml>")
        files = [_file("F20", "lot.kmz", mime="application/vnd.google-earth.kmz", thumb=False),
                 _file("F21", "panel.heic", mime="image/heic", thumb=False),
                 _file("F22", "big.pdf", mime="application/pdf", thumb=False),
                 _file("F23", "notes.docx", mime="application/msword", thumb=False),
                 {"id": "F24", "name": "stub.jpg", "file_access": "check_file_info"}]
        fake.files["https://files.example.test/F20/download"] = _Resp(content=boundary.getvalue(), ctype="application/zip")
        fake.files["https://files.example.test/F22/download"] = _Resp(content=b"%PDF" + b"0" * 3_000_000,
                                                                     ctype="application/pdf")
        fake.info["F24"] = _file("F24", "stub.jpg")
        fake.files["https://files.example.test/F24/thumb_1024.jpg"] = _Resp(content=JPEG, ctype="image/jpeg")
        config.MAX_FILE_MB = 2.0
        r = say("Roma, boundary and a few photos", "1902.1", files=files, subtype="file_share")
        rows = attachments.fetch_for_turn(store.task(r["queued"]), ws)
        by = {row["name"]: row for row in rows}
        check(by["lot.kmz"]["path"].endswith(".kml") and "coordinates" in (ws / by["lot.kmz"]["path"]).read_text(),
              "a Google Earth KMZ is unzipped to its KML, so a pasted boundary can be read as text")
        check(not by["panel.heic"]["path"] and "no viewable copy" in by["panel.heic"]["why"],
              "a photo Slack made no preview for, in a format the employee cannot view, is listed with why")
        check(not by["big.pdf"]["path"] and "larger than 2 MB" in by["big.pdf"]["why"],
              "a file over the size limit is listed, not saved")
        check(not by["notes.docx"]["path"] and "not a photo" in by["notes.docx"]["why"],
              "a file that is not a photo, PDF or text is listed, not saved")
        check(by["stub.jpg"]["path"].endswith(".jpg"),
              "a file Slack sent only as a stub is completed through files.info and saved")
        store.finish(r["queued"], "done", {"text": "ok"})

        # ------------------------------------------------------------- the app lacks files:read
        fake.scopes = "chat:write,reactions:write"
        slack_app.forget_scopes()
        n = len(fake.posts)
        r = say("Roma, rehab from these", "1903.1", files=[_file("F30", "deck.jpg")], subtype="file_share")
        say("", "1903.2", thread="1903.1", files=[_file("F31", "porch.jpg")], subtype="file_share")
        said = [p["text"] for p in fake.posts[n:]]
        check(len(said) == 1 and "files:read" in said[0] and "Reinstall" in said[0],
              "without files:read the thread is told once, before any turn runs, exactly what to change")
        rows = attachments.fetch_for_turn(store.task(r["queued"]), ws)
        check(rows and not any(row["path"] for row in rows) and all("files:read" in row["why"] for row in rows)
              and store.kv_get(attachments.LAST_KEY).startswith("blocked"),
              "the turn sees why the files could not be opened, and /health says the files are blocked")
        store.finish(r["queued"], "done", {"text": "ok"})
        fake.scopes = "chat:write,files:read"
        slack_app.forget_scopes()
        fake.files["https://files.example.test/F40/thumb_1024.jpg"] = _Resp(content=b"<html>sign in</html>",
                                                                          ctype="text/html; charset=utf-8")
        r = say("Roma, one more", "1904.1", files=[_file("F40", "yard.jpg")], subtype="file_share")
        rows = attachments.fetch_for_turn(store.task(r["queued"]), ws)
        check(rows[0]["path"] == "" and "files:read" in rows[0]["why"]
              and not list((ws / "inbox").rglob("01-yard.jpg")),
              "a sign-in page in place of the file is caught as a missing permission, and nothing half-saved is kept")
        store.finish(r["queued"], "done", {"text": "ok"})

        # ------------------------------------------------------------- an ask-back answered with photos
        r = say("Roma, rehab for the house on the corner", "1905.1")
        store.wait_for_operator(r["queued"], "Can you send the walkthrough photos?", "sess-1")
        r = say("", "1905.2", thread="1905.1", files=[_file("F50", "hall.jpg")], subtype="file_share")
        resumed = store.task(r.get("task") or 0) or {}
        check(r.get("resumed") and resumed.get("workflow") == "resume" and resumed.get("not_before", "") > store.now()
              and [f["id"] for f in attachments.on_thread("CFILES", "1905.1")] == ["F50"],
              "photos sent in answer to a question resume the employee, after the same short wait for more")
        r = say("", "1905.3", thread="1905.1", files=[_file("F51", "stairs.jpg")], subtype="file_share")
        check(r.get("joined") == resumed.get("id"), "a second batch of photos joins that resumed turn")

        # ------------------------------------------------------------------------- housekeeping
        stale = ws / "inbox" / "COLD-1000.1"
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "01-old.jpg").write_bytes(JPEG)
        past = time.time() - (config.FILES_KEEP_DAYS + 1) * 86400
        os.utime(stale, (past, past))
        attachments.fetch_for_turn(store.task(tid), ws)
        check(not stale.exists() and (ws / rows_dir(store.task(tid))).exists(),
              "saved files are deleted after the keep window, and a thread being worked on is kept")
        config.MAX_FILES = 2
        attachments.add("CFILES", "1906.1", [attachments.brief(_file("F6%d" % i, "p%d.jpg" % i)) for i in range(4)])
        check(len(attachments.on_thread("CFILES", "1906.1")) == 2 and attachments.over("CFILES", "1906.1") == 2,
              "past the per-thread limit files are counted, not kept, and the employee is told how many")
        for d in (chr(0x2014), chr(0x2013)):
            check(all(d not in p.get("text", "") for p in fake.posts), "nothing posted about files carries a long dash")
            check(d not in attachments.SCOPE_HELP and d not in prompt, "what the employee is told carries no long dash")
    finally:
        (slack_app._http, config.SLACK_BOT_TOKEN, config.WORKSPACES_DIR, config.FILE_SETTLE_S,
         config.MAX_FILE_MB, config.MAX_FILES, config.DB_PATH) = keep
        slack_app.forget_scopes()


def rows_dir(task: dict) -> str:
    from . import attachments
    return "%s/%s" % (attachments.INBOX, attachments._safe("%s-%s" % (task["slack_channel"], task["slack_thread"]), 80))
