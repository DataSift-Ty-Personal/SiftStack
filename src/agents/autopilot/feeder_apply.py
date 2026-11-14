"""Send an approved feeder change list to SiftMap (build plan 0.8).

`feeder_audit.py` only reads, on purpose: it cannot change a feeder even by accident. This module is
the other half. It sends the list the audit wrote, after the owner has read it and said yes.

    python src/agents/autopilot/feeder_apply.py apply --plan <plan.json> [--only ID ...] [--commit]
    python src/agents/autopilot/feeder_apply.py undo  --log  <applied.jsonl>           [--commit]

How it stays safe:

  * TWO SWITCHES. Nothing is sent unless --commit is passed AND FEEDER_APPLY_ALLOW=1 is set in the
    environment. One of them alone is a dry run that reads and reports.
  * READ FIRST. Each feeder is read fresh before its change. If its tags or its auto-add switch are
    no longer what the list saw, that feeder is skipped with the reason, because the list was
    written about a feeder that has since changed.
  * READ BACK. After each write the feeder is read again and every field sent must read the same.
    The first difference stops the run: a write that did not stick is not a reason to send more.
  * A LOG OF WHAT WAS THERE. Every write is appended to a log with the values it replaced, and
    `undo` sends those values back, newest first.

What a change may carry (the audit's `set`):

    auto_add_enabled   false only. This module never switches auto-add ON: a feeder that starts
                       adding records spends the record allowance the same night.
    tags               the whole list of tag names the feeder stamps from now on.
    filters            keys merged INTO the feeder's own criteria (filter_data.filters). Nothing
                       already there is dropped, and the addresses block is left alone.

Switching auto-add off deletes nothing, and taking a tag off a feeder changes only what it stamps
from now on: records it already added keep the old tag.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would be first on the path and shadow modules of the same name.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

MAP = "https://map.reisift.io"
OUT_DIR = ROOT / "output" / "autopilot" / "feeder_audit"
ALLOW_ENV = "FEEDER_APPLY_ALLOW"
_STD = {"accept": "application/json, text/plain, */*", "origin": "https://beta.reisift.io",
        "referer": "https://beta.reisift.io/", "content-type": "application/json"}


class Refused(RuntimeError):
    """A write was asked for without both switches."""


class FeederError(RuntimeError):
    pass


def may_write(commit: bool) -> bool:
    """Both switches, or nothing is sent."""
    return bool(commit) and os.environ.get(ALLOW_ENV) == "1"


# ------------------------------------------------------------------------------------------ the wire
class Wire:
    """GET and PATCH on one feeder, with the Open API key. PATCH refuses unless `allowed`."""

    def __init__(self, key: str, allowed: bool = False, gap: float = 1.0, sleep=time.sleep):
        self.key, self.allowed, self.gap, self.sleep = key, allowed, gap, sleep
        self.requests = 0
        self._last = 0.0

    def _send(self, method: str, url: str, body=None):
        if not url.startswith(MAP + "/filters/"):
            raise FeederError("refusing %s %s: not a SiftMap filter address" % (method, url))
        wait = self.gap - (time.time() - self._last)
        if wait > 0:
            self.sleep(wait)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=dict(_STD, authorization="Api-Key " + self.key), method=method)
        for attempt in range(4):
            try:
                self._last = time.time()
                self.requests += 1
                with urllib.request.urlopen(req, timeout=45) as r:
                    text = r.read().decode("utf-8")
                    return json.loads(text) if text.strip().startswith(("{", "[")) else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429 and attempt < 3:
                    try:
                        hold = float((e.headers.get("Retry-After") if e.headers else "") or "")
                    except ValueError:
                        hold = 10.0 * (attempt + 1)
                    self.sleep(min(hold, 90))
                    continue
                # A write is never retried on a server error: it may have landed.
                if method == "GET" and e.code in (500, 502, 503, 504) and attempt < 2:
                    self.sleep(3 + 4 * attempt)
                    continue
                raise FeederError("HTTP %s on %s %s: %s" % (e.code, method, url.replace(MAP, ""), text))
            except (urllib.error.URLError, TimeoutError) as e:
                if method == "GET" and attempt < 2:
                    self.sleep(3 + 4 * attempt)
                    continue
                raise FeederError("could not reach SiftMap on %s: %s" % (method, e))
        raise FeederError("SiftMap kept refusing")

    def get(self, fid) -> dict:
        return self._send("GET", "%s/filters/%s/" % (MAP, fid))

    def patch(self, fid, body: dict) -> dict:
        if not self.allowed:
            raise Refused("refused: a write needs --commit AND %s=1" % ALLOW_ENV)
        return self._send("PATCH", "%s/filters/%s/" % (MAP, fid), body)


# ------------------------------------------------------------------------------------------ the change
def _names(v) -> list[str]:
    out = []
    for t in v or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def body_for(change: dict, current: dict) -> tuple[dict, dict]:
    """(what to send, what it replaces) for one change against the feeder as it stands now."""
    want = change.get("set") or {}
    unknown = sorted(set(want) - {"auto_add_enabled", "tags", "filters"})
    if unknown:
        raise FeederError("feeder %s: the list asks for something this tool does not send: %s" % (change.get("id"), unknown))
    send, before = {}, {}
    if "auto_add_enabled" in want:
        if want["auto_add_enabled"] is not False:
            raise FeederError("feeder %s: this tool only ever switches auto-add OFF" % change.get("id"))
        send["auto_add_enabled"] = False
        before["auto_add_enabled"] = bool(current.get("auto_add_enabled"))
    if "tags" in want:
        send["tags"] = [str(t) for t in want["tags"]]
        before["tags"] = _names(current.get("tags"))
    if "filters" in want:
        data = copy.deepcopy(current.get("filter_data") or {})
        crit = dict(data.get("filters") or {})
        crit.update(want["filters"])
        data["filters"] = crit
        send["filter_data"] = data
        before["filter_data"] = copy.deepcopy(current.get("filter_data") or {})
    return send, before


def still_as_listed(change: dict, listed: dict | None, current: dict) -> str:
    """'' when the feeder still looks the way the list saw it, else the reason to skip it."""
    if not current or current.get("id") != change.get("id"):
        return "the feeder could not be read"
    if current.get("is_active") is False:
        return "the feeder has been deleted since the list was written"
    if listed is None:
        return ""
    if sorted(_names(current.get("tags"))) != sorted(listed.get("tags") or []):
        return "its tags changed since the list was written (now %s)" % (_names(current.get("tags")) or "none")
    if bool(current.get("auto_add_enabled")) != bool(listed.get("auto_add")):
        return "its auto-add switch changed since the list was written"
    return ""


def reads_as_sent(send: dict, after: dict) -> str:
    """'' when the feeder reads back as sent, else what differs."""
    if "auto_add_enabled" in send and bool(after.get("auto_add_enabled")) is not False:
        return "auto-add still reads on"
    if "tags" in send and sorted(_names(after.get("tags"))) != sorted(send["tags"]):
        return "tags read %s, sent %s" % (_names(after.get("tags")), send["tags"])
    if "filter_data" in send:
        got = (after.get("filter_data") or {}).get("filters") or {}
        for k, v in (send["filter_data"].get("filters") or {}).items():
            if got.get(k) != v:
                return "criteria key %s reads %r, sent %r" % (k, got.get(k), v)
        if (after.get("filter_data") or {}).get("addresses") != send["filter_data"].get("addresses"):
            return "the addresses block changed"
    return ""


def already_done(send: dict, current: dict) -> bool:
    return not reads_as_sent(send, current)


# ------------------------------------------------------------------------------------------ apply
def apply_plan(wire: Wire, plan: dict, *, only: list | None = None, log_path: Path | None = None, out=print) -> dict:
    """Walk the list. With a wire that may not write, this is a dry run that still reads each feeder
    and says what it would send."""
    listed = {f["id"]: f for f in plan.get("feeders") or []}
    never = {n["id"] for n in plan.get("never_on") or []}
    want_ids = {str(x) for x in only or []}
    done = {"allowed": wire.allowed, "changed": [], "already": [], "skipped": [], "would": [], "stopped": ""}
    for ch in plan.get("changes") or []:
        fid = ch["id"]
        if want_ids and str(fid) not in want_ids:
            continue
        if fid in never:
            done["skipped"].append("%s %s: on the never-touch list" % (fid, ch.get("title")))
            continue
        cur = wire.get(fid)
        why = still_as_listed(ch, listed.get(fid), cur)
        send, before = body_for(ch, cur) if not why else ({}, {})
        if not why and already_done(send, cur):
            done["already"].append("%s %s" % (fid, ch.get("title")))
            continue
        if why:
            done["skipped"].append("%s %s: %s" % (fid, ch.get("title"), why))
            out("skip   %s %s: %s" % (fid, ch.get("title"), why))
            continue
        if not wire.allowed:
            done["would"].append("%s %s: %s" % (fid, ch.get("title"), ch.get("do")))
            out("would  %s %s: %s" % (fid, ch.get("title"), ch.get("do")))
            continue
        wire.patch(fid, send)
        after = wire.get(fid)
        diff = reads_as_sent(send, after)
        if log_path is not None:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "id": fid, "title": ch.get("title"),
                                     "group": ch.get("group"), "sent": send, "before": before, "read_back_ok": not diff}) + "\n")
        if diff:
            done["stopped"] = "%s %s did not read back as sent: %s" % (fid, ch.get("title"), diff)
            out("STOP   " + done["stopped"])
            break
        done["changed"].append("%s %s: %s" % (fid, ch.get("title"), ch.get("do")))
        out("done   %s %s: %s" % (fid, ch.get("title"), ch.get("do")))
    return done


def undo(wire: Wire, log_path: Path, out=print) -> dict:
    """Send back the values each logged write replaced, newest first."""
    rows = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    done = {"allowed": wire.allowed, "restored": [], "would": [], "stopped": ""}
    for row in reversed(rows):
        fid, before = row["id"], row.get("before") or {}
        if not before:
            continue
        if not wire.allowed:
            done["would"].append("%s %s" % (fid, row.get("title")))
            out("would restore %s %s" % (fid, row.get("title")))
            continue
        wire.patch(fid, before)
        after = wire.get(fid)
        bad = ""
        if "auto_add_enabled" in before and bool(after.get("auto_add_enabled")) != bool(before["auto_add_enabled"]):
            bad = "auto-add did not go back"
        if "tags" in before and sorted(_names(after.get("tags"))) != sorted(before["tags"]):
            bad = "tags did not go back"
        if "filter_data" in before and (after.get("filter_data") or {}) != before["filter_data"]:
            bad = "criteria did not go back"
        if bad:
            done["stopped"] = "%s %s: %s" % (fid, row.get("title"), bad)
            out("STOP   " + done["stopped"])
            break
        done["restored"].append("%s %s" % (fid, row.get("title")))
        out("restored %s %s" % (fid, row.get("title")))
    return done


# ------------------------------------------------------------------------------------------ command line
def _key() -> str:
    from datasift_api_upload import env
    key = (env().get("REISIFT_API_KEY") or "").strip()
    if not key:
        raise SystemExit("REISIFT_API_KEY is not set")
    return key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send an approved feeder change list; dry unless --commit and %s=1" % ALLOW_ENV)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("apply")
    a1.add_argument("--plan", required=True)
    a1.add_argument("--only", action="append", default=[], help="a feeder id; repeat for more")
    a1.add_argument("--commit", action="store_true")
    a1.add_argument("--gap", type=float, default=1.0)
    a2 = sub.add_parser("undo")
    a2.add_argument("--log", required=True)
    a2.add_argument("--commit", action="store_true")
    a2.add_argument("--gap", type=float, default=1.0)
    args = ap.parse_args(argv)
    allowed = may_write(args.commit)
    if args.commit and not allowed:
        print("refused: --commit was passed but %s is not 1. Nothing was sent." % ALLOW_ENV)
        return 3
    wire = Wire(_key(), allowed=allowed, gap=args.gap)
    if args.cmd == "apply":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        log_path = OUT_DIR / ("applied_%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
        done = apply_plan(wire, plan, only=args.only, log_path=log_path if allowed else None)
        print("%s: %d changed, %d already as listed, %d skipped%s (%d requests)" % (
            "APPLIED" if allowed else "DRY RUN, nothing sent", len(done["changed"]) if allowed else len(done["would"]),
            len(done["already"]), len(done["skipped"]), "; STOPPED: " + done["stopped"] if done["stopped"] else "", wire.requests))
        if allowed:
            print("log: %s" % log_path)
        return 1 if done["stopped"] else 0
    done = undo(wire, Path(args.log))
    print("%s: %d restored%s" % ("UNDONE" if allowed else "DRY RUN, nothing sent", len(done["restored"]) if allowed else len(done["would"]),
                                 "; STOPPED: " + done["stopped"] if done["stopped"] else ""))
    return 1 if done["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())
