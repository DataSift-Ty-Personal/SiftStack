"""Curative Title board selftest (Ty, 2026-11-23): the board's nine columns and words; Ready for Marketing
(every ready house waits there, the morning pass starts at most daily_starts a business day, the
longest-waiting first, and a waiting card gets no task and no letter); the round (one column a business day,
every living verified heir with a number dialed, an heir with no number never holds a day); Heir Engaged
and its one alert, from any column; Exhausted and its one follow-up task; a lead touched only by its card
move; the letters (a full address only, staged, released by a person twice, never while mail is held);
no text, ever; plan mode; undo; the heirs lane marker in both gate copies; the county board and the heir
lane skipping a curative record; the loader (dry unless both switches, the address match with the
scoring's old ids as a cross-check, the zip lookups only in a real run, the tag last); reset-statuses (the
keep list, a house another hold keeps off, the log and its undo); and the board tool (a missing column
made in its place on a board made before, the order read back).

Zero network: fakes for DataSift, the board and its columns, tasks, documents, the geocoder and the PDF
renderer. Every name, street, number and id here is invented.
"""
from __future__ import annotations

import copy
import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMERSON, EMPLOYEES, _rec
from .selftest_phase0 import status_ev
from .selftest_textlane import Crm, _wipe, at, call_ev

TZ = "America/New_York"
DANA = {"uuid": "u-dana", "first_name": "Dana", "is_active": True}
# The line this track hands the orchestrator for autopilot/registry.py. Until it is in that file the
# section adds it for its own run and takes it out again.
REGISTRY = {"curative": ("curative", "run_curative", "outreach", False, False)}


class Missing(RuntimeError):
    def __init__(self, text):
        super().__init__(text)
        self.code = 404


class CurCrm(Crm):
    """The fake DataSift with the routes the curative board and its loader use: the SiftLine board, tasks,
    a record's documents, tags and the address search. A card listing carries no record id, as the
    reference does not promise one, so the stage has to know its own cards."""
    BOARD_ID = "board-cur"

    def __init__(self, records, users=(EMERSON, DANA), board=True, tagged=True):
        from .autopilot import curative
        super().__init__(records, users=users)
        self.cols = {t: "ccol-%d" % i for i, t in enumerate(curative.COLUMNS)}
        self.boards = [{"uuid": self.BOARD_ID, "title": curative.BOARD}] if board else []
        self.tag_rows = [{"uuid": "t-curative", "title": "Curative"}] if tagged else []
        self.cards: dict = {}
        self.tasks: dict = {}
        self.docs: dict = {}
        self.seq = 0
        self.log: list = []
        self.pins: list = []

    def _id(self, prefix: str) -> str:
        self.seq += 1
        return "%s-%d" % (prefix, self.seq)

    def in_column(self, title: str) -> set:
        return {c["prop"] for c in self.cards.values() if c["column"] == self.cols[title]}

    def open_tasks(self, u: str = "") -> list:
        return [t for t in self.tasks.values() if (not u or t.get("assigned_to_property") == u) and not t.get("completed")]

    def said(self, u, rec=None):
        return "\n".join([str((rec or {}).get("notes") or "")] + [t for (x, t) in self.msgs if x == u])

    def message(self, u, text, pin=False):
        if pin:
            self.pins.append((u, text))
        return super().message(u, text, pin)

    def sent(self) -> list:
        """Every request that could change the account."""
        return [x for x in self.log if x[0] != "GET" and not (x[0] == "POST" and x[1] == "/api/internal/property/")]

    def call(self, path, method="GET", body=None, headers=None):
        import urllib.parse
        base = path.split("?")[0]
        q = urllib.parse.parse_qs(path.split("?", 1)[1]) if "?" in path else {}
        self.log.append((method, base, copy.deepcopy(body)))
        parts = [p for p in base.split("/") if p]
        if base == "/api/internal/user/":
            return {"email": "key@example.test"}
        if parts[:3] == ["api", "internal", "siftline"]:
            return self._board(parts[3:], q, method, body, path)
        if base == "/api/internal/task/" and method == "POST":
            tid = self._id("task")
            self.tasks[tid] = dict(body, uuid=tid, completed=None)
            self.writes.append(("task", body.get("assigned_to_property"), body.get("title")))
            return {"uuid": tid}
        m = re.fullmatch(r"/api/internal/task/([^/]+)/", base)
        if m:
            t = self.tasks.get(m.group(1))
            if t is None:
                raise Missing("HTTP 404 on %s" % path)
            if method == "DELETE":
                del self.tasks[m.group(1)]
                self.writes.append(("task_delete", t.get("assigned_to_property"), t.get("title")))
                return {}
            return dict(t)
        m = re.fullmatch(r"/api/internal/property/([^/]+)/document/(presigned-url/|[^/]+/)?", base)
        if m:
            u, rest = m.group(1), m.group(2) or ""
            docs = self.docs.setdefault(u, [])
            if rest == "presigned-url/":
                return {"presigned_post": {"url": "https://files.example.test/upload", "fields": {"key": "k"}},
                        "storage_key": self._id("key")}
            if rest and method == "DELETE":
                self.docs[u] = [d for d in docs if d["uuid"] != rest.strip("/")]
                self.writes.append(("doc_delete", u))
                return {}
            if method == "POST":
                d = dict(body, uuid=self._id("doc"))
                docs.append(d)
                self.writes.append(("doc", u, body.get("filename")))
                return {"uuid": d["uuid"]}
            return {"results": list(docs)}
        if base == "/api/internal/tag/":
            if method == "POST":
                self.tag_rows.append({"uuid": self._id("tag"), "title": body["title"]})
                self.writes.append(("tag_create", body["title"]))
                return {}
            return {"results": list(self.tag_rows)}
        if base == "/api/internal/property/" and method == "POST" and (headers or {}).get("x-http-method-override") == "GET":
            s = str((((body or {}).get("query") or {}).get("must") or {}).get("search") or "")
            want = s.split(":", 1)[1].strip().lower() if ":" in s else ""
            rows = [self._row(r) for r in self.recs.values() if str(r["address"]["street"]).lower().startswith(want)]
            return {"count": len(rows), "results": rows}
        return super().call(path, method, body, headers)

    def _board(self, rest, q, method, body, path):
        if rest == ["board"]:
            if method == "POST":
                b = {"uuid": self._id("board"), "title": body["title"]}
                self.boards.append(b)
                self.writes.append(("board_create", body["title"]))
                return b
            return {"results": list(self.boards)}
        if len(rest) == 2 and rest[0] == "board":
            b = next((x for x in self.boards if x["uuid"] == rest[1]), None)
            if b is None:
                raise Missing("HTTP 404 on %s" % path)
            return dict(b)
        if len(rest) >= 4 and rest[:2] == ["board", "column"] and rest[3] == "card":
            col = rest[2]
            if len(rest) == 4:
                if method == "GET":
                    rows = [{"uuid": k} for k, c in sorted(self.cards.items()) if c["column"] == col]
                    off, lim = int((q.get("offset") or ["0"])[0]), int((q.get("limit") or ["100"])[0])
                    return {"results": rows[off:off + lim]}
                if method == "POST":
                    k = self._id("card")
                    self.cards[k] = {"column": col, "prop": body["prop"]}
                    self.writes.append(("card_create", body["prop"], col))
                    return {"uuid": k}
            card = rest[4]
            c = self.cards.get(card)
            if c is None or c["column"] != col:
                raise Missing("HTTP 404 on %s %s" % (method, path))
            if len(rest) == 6 and rest[5] == "timeline":
                return {cu: {"title": t, "order": i, "time": 0, "current": cu == col} for i, (t, cu) in enumerate(self.cols.items())}
            if method == "GET":
                return {"uuid": card}
            if method == "PATCH":
                c["column"] = body["column"]
                self.writes.append(("card_move", c["prop"], body["column"]))
                return {"uuid": card}
            if method == "DELETE":
                del self.cards[card]
                self.writes.append(("card_remove", c["prop"], col))
                return {}
        raise AssertionError("the fake board has no route for %s %s" % (method, path))


class ColCrm:
    """The workstation's login, for the board's columns only (siftline_columns.py). Two server behaviors the
    published API does not settle, both tried: `shift` makes room for a column put at a place already taken
    (the later columns move up one); without it the new column shares the place. `patch` lets the column's
    own route take a new place; without it that route refuses."""

    def __init__(self, shift: bool = False, patch: bool = True):
        self.cols: dict = {}
        self.shift, self.patch = shift, patch
        self.patched: list = []

    def call(self, path, method="GET", body=None, headers=None):
        m = re.fullmatch(r"/api/internal/siftline/board/([^/]+)/column/(?:([^/]+)/)?", path.split("?")[0])
        if not m:
            raise AssertionError("the login fake has no route for %s %s" % (method, path))
        rows = self.cols.setdefault(m.group(1), [])
        if m.group(2):
            if method != "PATCH":
                raise AssertionError("the login fake has no route for %s %s" % (method, path))
            if not self.patch:
                raise Missing("HTTP 405 on PATCH %s" % path)
            c = next(x for x in rows if x["uuid"] == m.group(2))
            if body.get("title", c["title"]) != c["title"]:
                raise AssertionError("a reorder must never rename a column: %s" % body)
            c["order"] = int(body["order"])
            self.patched.append((c["title"], c["order"]))
            return dict(c)
        if method == "POST":
            if self.shift:
                for x in rows:
                    if x["order"] >= int(body["order"]):
                        x["order"] += 1
            c = {"uuid": "col-%s-%d" % (m.group(1), len(rows) + 1), "title": body["title"], "order": int(body["order"])}
            rows.append(c)
            return dict(c)
        return {"results": [dict(x) for x in rows]}

    def titles(self, board: str) -> list:
        return [c["title"] for c in sorted(self.cols.get(board) or [], key=lambda c: (c["order"], c["title"]))]


# ---------------------------------------------------------------------- the world
def num(n: int) -> str:
    return "865555%04d" % n


def phone(n: int, tier: str = "Dial First", kind: str = "MOBILE", tags=(), status: str = "UNKNOWN") -> dict:
    return {"number": num(n), "type": kind, "tags": [tier] + list(tags), "status": status}


def heir(name: str, rel: str, share: str, n: int = 0, living=True, verified=True, tier: str = "Dial First",
         kind: str = "MOBILE", street: str = "", city: str = "", state: str = "", zip5: str = "") -> dict:
    from .autopilot import heirlane
    return {"key": heirlane.heir_key(name), "name": name, "first": name.split()[0], "relationship": rel, "share": share,
            "living": living, "verified": verified, "numbers": [{"number": num(n), "tier": tier, "type": kind}] if n else [],
            "street": street, "city": city, "state": state, "zip": zip5}


def tree(u: str, heirs: list, pdf: bool = True, loaded: str = "2026-11-15T12:00:00+00:00") -> dict:
    return {"version": 1, "source": "the loader", "record_uuid": u, "slug": u, "loaded": loaded,
            "pdf": {"name": "curative-title-%s.pdf" % u, "filed": True, "at": loaded} if pdf else None,
            "call_first": heirs[0]["key"] if heirs else "", "heirs": heirs}


def crec(u: str, street: str, phones=(), tags=("Curative", "Courthouse Data"), **kw) -> dict:
    """A curative record: the owner of record has died (the flag and the Obituary list)."""
    kw.setdefault("lists", ["Obituary"])
    kw.setdefault("deceased", True)
    r = _rec(u, street, tags=list(tags), phones=list(phones), **kw)
    return r


def playbook(mode: str = "live", **over) -> dict:
    from .autopilot import curative
    board = {"kind": "curative", "owner": "sales", "mode": mode, "schedule": ["02:50 Mon-Fri", "08:35 Mon-Fri", "17:00 Mon-Fri"],
             "lane": "heirs", "gate_allow": ["deceased", "heir", "in_research"], "caller": "Dana",
             "column_ids": {t: "ccol-%d" % i for i, t in enumerate(curative.COLUMNS)}, "daily_pieces": 0}
    lanes_block = over.pop("lanes", {"heirs": {"tags": ["Heir Identified", "Curative"]}})
    board.update(over)
    return {
        "business": {"tz": TZ, "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
        "caps": {"daily_usd": 25, "record_usd": 1.0, "outreach_daily_usd": 60},
        "people": {"callers": ["Emerson"]},
        "gate": {"research_tags": ["Tax Sale Redemption", "Curative"]},
        "lanes": lanes_block,
        "stages": {
            "c_board": board,
            "heir_lane": {"kind": "heirlane", "owner": "ops", "mode": "live", "schedule": "08:20 Mon-Fri",
                          "gate_allow": ["heir", "in_research"], "heir_caller": "Dana", "texts": False, "daily_pieces": 0},
        },
    }


def _register(policy, registry):
    added = [k for k in REGISTRY if k not in registry.EXTRA]
    saved = (policy.KINDS, policy.NO_PRESETS)
    spend_added = [k for k in REGISTRY if k not in policy.SPEND_BY_KIND]
    for k in added:
        registry.EXTRA[k] = REGISTRY[k]
    policy.KINDS = tuple(dict.fromkeys(tuple(policy.KINDS) + tuple(REGISTRY)))
    policy.SPEND_BY_KIND.update({k: v[2] for k, v in REGISTRY.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(tuple(policy.NO_PRESETS) + tuple(k for k, v in REGISTRY.items() if not v[3])))

    def restore():
        for k in added:
            registry.EXTRA.pop(k, None)
        for k in spend_added:
            policy.SPEND_BY_KIND.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved
    return restore


def _clean() -> None:
    from . import store
    from .autopilot import activity, limits, scrub
    _wipe(store, activity, limits, scrub)
    activity.reset()
    folder = Path(config.DATA_DIR) / "curative" / "heirs"
    if folder.is_dir():
        for f in folder.glob("*.json"):
            f.unlink()           # only files this section wrote into its own temp folder


def _sweep(sid, crm, when, pol):
    from .autopilot import sweep
    return sweep.run(sid, crm=crm, now=when, pol=pol)


def _ten_digits(*texts) -> bool:
    return any(re.search(r"\d{10}", json.dumps(t, default=str)) for t in texts)


def _no_dash(*texts) -> bool:
    return not any(chr(0x2014) in json.dumps(t, default=str) or chr(0x2013) in json.dumps(t, default=str) for t in texts)


def _texts_count() -> int:
    from . import store
    with store._LOCK, store._db() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM ap_texts").fetchone()["n"])


def _live_starts() -> list:
    """The days the live curative board remembers starting houses on."""
    from . import store
    with store._LOCK, store._db() as c:
        return [r["key"] for r in c.execute("SELECT key FROM kv WHERE key LIKE 'autopilot:curative:starts:live:%'").fetchall()]


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import policy, registry
    names = ("DATA_DIR", "DB_PATH", "DAILY_CAP_USD", "RUN_CAP_USD", "MODE")
    keep = {k: getattr(config, k) for k in names}
    config.DATA_DIR = Path(tmp) / "curative_section"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DAILY_CAP_USD, config.RUN_CAP_USD, config.MODE = 100000.0, 5.0, "live"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    restore = _register(policy, registry)
    saved_env = os.environ.pop("CURATIVE_ALLOW", None)
    try:
        _pieces(check)
        _blocks(check)
        _lanes_and_skips(check, tmp)
        _week(check)
        _ready(check)
        _more(check)
        _plan(check)
        _letters(check)
        _undo(check)
        _research_path(check, tmp)
        _loader(check, tmp)
        _reset(check, tmp)
        _board_tool(check)
        _source(check)
    finally:
        restore()
        os.environ.pop("CURATIVE_ALLOW", None)
        if saved_env is not None:
            os.environ["CURATIVE_ALLOW"] = saved_env
        for k, v in keep.items():
            setattr(config, k, v)


# ---------------------------------------------------------------------- the pieces
def _pieces(check) -> None:
    from datetime import date
    from .autopilot import curative as cv
    print("  the pieces")
    check(list(cv.COLUMNS) == ["Research", "Ready for Marketing", "Day 1", "Day 2", "Day 3", "Day 4", "Day 5", "Heir Engaged",
                               "Exhausted"]
          and [cv.column_no(t) for t in cv.COLUMNS] == [0, 8, 1, 2, 3, 4, 5, 6, 7] and cv.column_no("Elsewhere") == -1
          and [cv.column_title(n) for n in range(7)] == ["Research"] + list(cv.DAY_COLUMNS) + ["Exhausted"],
          "the board's nine columns read Research, Ready for Marketing, Day 1 to Day 5, Heir Engaged, Exhausted, and a "
          "sixth day is Exhausted")
    check(cv.relationship_tag("son of Pat Example") == "Son" and cv.relationship_tag("wife") == "Spouse"
          and cv.relationship_tag("great-niece") == "Grandniece" and cv.relationship_tag("granddaughter through her father") == "Granddaughter"
          and cv.relationship_tag("a family friend") == "Relative" and cv.relationship_head("daughter of Pat Example") == "daughter",
          "a phone tag and a task carry the heir's tie and never the name the research wrote beside it")
    check(cv.share_text("1/17 (working)") == "1/17" and cv.share_text("") == "" and cv.legal_name('Kim "Kimmy" Example (Smith)') == "Kim Example",
          "a share reads as a fraction, and a name drops research's asides")
    check([cv.tier_from_activity(s) for s in (95, 81, 80, 61, 60, 41, 40, 21, 20, None)]
          == ["Dial First", "Dial First", "Dial Second", "Dial Second", "Dial Third", "Dial Third", "Dial Fourth", "Dial Fourth",
              "Drop", ""], "an activity score maps to the house tiers (81 and up Dial First ... below 21 Drop)")
    days = frozenset(range(5))
    a, b, c_ = (cv.Heir(key="a", name="A Example", living=True, verified=True, numbers=[{"number": num(1)}]),
                cv.Heir(key="b", name="B Example", living=True, verified=True, numbers=[{"number": num(2)}]),
                cv.Heir(key="c", name="C Example", living=True, verified=True))
    usable = [{"number": num(1), "tier": "Dial First", "status": "UNKNOWN", "type": "MOBILE"},
              {"number": num(2), "tier": "Dial Second", "status": "UNKNOWN", "type": "LANDLINE"}]
    need = cv.need_of([a, b, c_], usable)
    check([h.key for h, _ in need] == ["a", "b"], "an heir with no usable number is not in the round: they never hold a day")
    mon, tue, wed = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
    dialed = {mon: {num(1)}, tue: {num(1), num(2)}}
    check(not cv.round_done(mon, need, dialed, set()) and cv.round_done(tue, need, dialed, set()),
          "a day counts only when every heir with a number had their best number dialed")
    check(cv.round_done(mon, need, dialed, {num(2)}) is False and cv.round_done(mon, need, {mon: {num(1), num(2)}}, {num(2)}),
          "a number marked wrong since still counts on the day it was dialed: it was the best one then")
    check(not cv.round_done(mon, [], {mon: {num(1)}}, set()), "with no heir to call, no day counts: the card cannot walk itself")
    n, misses = cv.judge(1, mon, wed, lambda d: cv.round_done(d, need, dialed, set()), days)
    check(n == 2 and misses == [mon, wed], "a day with an undialed heir does not count; one day earns one column at most")
    check(cv.judge(5, mon, mon, lambda d: True, days)[0] == 6 and cv.column_title(6) == cv.EXHAUSTED,
          "the fifth full round takes the card to Exhausted")
    letter = cv.render_letter(cv.LETTER, "Kim", "12 Oak St", "Dana", "865-555-0100")
    check(not cv.audit_letter(letter, "12 Oak St") and "Kim" in letter and "12 Oak St" in letter and "$" not in letter,
          "the default letter greets the heir, names the house, gives no price and passes the audit")
    for bad, needle, what in (("I am sorry for your loss about 12 Oak St.", "names a death", "names a loss"),
                              ("The estate at 12 Oak St.", "names a death", "says estate"),
                              ("I can pay 50,000 for 12 Oak St.", "price", "names a figure"),
                              ("I can pay $50000 for 12 Oak St.", "dollar", "names a dollar amount"),
                              ("My attorney can clear the title on 12 Oak St.", "legal", "gives a legal matter"),
                              ("I represent buyers for 12 Oak St.", "claim", "claims to represent anyone"),
                              ("Is 12 Oak St in foreclosure?", "list", "names the list")):
        check(any(needle in p for p in cv.audit_letter(bad, "12 Oak St")), "a curative letter that %s is refused" % what)
    check(cv.audit_setting({"letter_copy": "Hi {first}, about {street}, {nope}."}) != [],
          "a letter setting with a blank the board cannot fill is refused before anything is staged")
    on_estate = cv.render_letter(cv.LETTER, "Kim", "4 Old Estate Ln", "Dana", "865-555-0100")
    check(not cv.audit_letter(on_estate, "4 Old Estate Ln") and cv.audit_letter(on_estate, ""),
          "the house's own street is not copy: a house on Old Estate Ln still gets its letter")


def _blocks(check) -> None:
    from .autopilot import policy
    print("  the playbook block")
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    st = pol.stage("c_board")
    check(st.spend == "outreach" and st.kind == "curative", "the board stage is outreach: its letters cost money")
    for over, why in (({"column_ids": {"Day 6": "x"}}, "a column the board does not have"),
                      ({"lane": "ftm"}, "a lane other than heirs"),
                      ({"caller": 7}, "a caller that is neither a name nor {name, uuid}"),
                      ({"letter_copy": 12}, "a letter that is not text"),
                      ({"daily_pieces": -1}, "a negative count"),
                      ({"pipeline": "ftm_pipeline"}, "a pipeline to follow")):
        try:
            policy.parse(playbook(**over), EMPLOYEES)
            check(False, "a block with %s is refused when the playbook loads" % why)
        except policy.PolicyError:
            check(True, "a block with %s is refused when the playbook loads" % why)


# ---------------------------------------------------------------------- the lanes, and the two boards that skip it
def _lanes_and_skips(check, tmp) -> None:
    from sms_agent import gate_rules
    from .autopilot import ftmpipeline as fp
    from .autopilot import heirlane, lanes, policy, stages
    print("  the heirs lane, and the boards that skip a curative record")
    check(lanes.lane_of({"uuid": "x", "tags": ["Curative"], "lists": []}) == "heirs"
          and lanes.lane_of({"uuid": "x", "tags": ["Curative", "FTM", "Top 1000"], "lists": ["Obituary"]}) == "heirs",
          "the Curative tag puts a record in the heirs lane, above first to market and the obituary lane")
    check(lanes.DEFAULT_MARKERS["heirs"]["tags"][0] == "Heir Identified" and "Curative" in lanes.DEFAULT_MARKERS["heirs"]["tags"],
          "the heirs lane keeps Heir Identified first (a lane's first tag is the one a count reads) and adds Curative")
    check(gate_rules.DEFAULT_MARKERS == lanes.DEFAULT_MARKERS and gate_rules.lane_of({"uuid": "x", "tags": ["Curative"]}) == "heirs",
          "the texting app's copy of the lanes says the same")
    check(fp.options({}).get("skip_tags") == ["Curative"] and heirlane.DEFAULTS.get("skip_tags") == ["Curative"],
          "the county board and the heir lane skip the Curative tag by default")
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)

    class Ctx:
        class policy:
            gate = pol.gate
            lanes = pol.lanes
    ftm = _rec("fx", "40 Skip St", phones=[phone(4001)], tags=["FTM", "Courthouse Data", "Foreclosure"])
    state, _ = fp.record_state(Ctx(), ftm, fp.options({}))
    both = dict(copy.deepcopy(ftm), tags=["FTM", "Courthouse Data", "Foreclosure", "Curative"])
    state2, why2 = fp.record_state(Ctx(), both, fp.options({}))
    col, _ = fp.placement(Ctx(), both, None, fp.options({}), None, "ftm")
    check(state == "work" and state2 == "gone" and "Curative Title" in why2 and col == "",
          "a first-to-market record that carries the Curative tag never gets a First to Market card: %s" % why2)
    st = pol.stage("heir_lane")
    ctx = stages.Ctx(policy=pol, stage=st, crm=CurCrm([]), run_id="t", now=at(16, 8, 20), live=False,
                     out=stages.Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=False))
    rec = crec("hx", "41 Skip St", tags=["Heir Identified", "Curative"])
    check("Curative Title" in heirlane.still_due(ctx, rec, {"kind": "letter"}),
          "an heir letter or text staged before the record went curative is held back at release")


# ---------------------------------------------------------------------- a week on the board
def _week_world() -> list:
    return [
        crec("c1", "11 Heir St", phones=[phone(1101, tags=["Daughter", "Heir", "Curative"]),
                                         phone(1102, "Dial Second", "LANDLINE", tags=["Son", "Heir", "Curative"])],
             status="Deep Prospecting"),
        crec("c2", "12 Heir St", phones=[phone(1201)]),                             # its heir map is not on the record yet
        crec("c3", "13 Heir St"),                                                   # no heir tree on this box
        crec("c4", "14 Heir St", status="Hot Lead"),                                # already a lead
        crec("c5", "15 Heir St", tags=["Curative", "Do Not Market"]),
        crec("c6", "16 Heir St", first="", last="", company="Example Holdings LLC"),
        _rec("c7", "17 Heir St", phones=[phone(1701)]),                             # not curative at all
        crec("c8", "18 Heir St", phones=[phone(1801, tags=["Son", "Heir", "Curative"])]),
        crec("c9", "19 Heir St", phones=[phone(1901, tags=["Wife", "Heir", "Curative"])]),
        crec("c11", "21 Heir St", phones=[phone(2101)], tags=["Curative", "Tax Sale Redemption"]),
    ]


def _week_trees() -> None:
    from .autopilot import curative as cv
    from .autopilot import ledger
    folder = cv.heir_dir(cv.DEFAULTS)
    folder.mkdir(parents=True, exist_ok=True)
    c1 = tree("c1", [heir("Kim Example", "daughter", "1/3", 1101, street="1 Elm St", city="Maryville", state="TN", zip5="37801"),
                     heir("Lee Example", "son", "1/3", 1102, tier="Dial Second", kind="LANDLINE", street="2 Oak St", city="Alcoa",
                          state="TN"),
                     heir("Ann Example", "granddaughter", "1/6", street="3 Pine St", city="Knoxville", state="TN", zip5="37917"),
                     heir("Ray Example", "nephew", "1/6", living=None, verified=False)])
    (folder / "c1.json").write_text(json.dumps(c1), encoding="utf-8")      # the loader's file, as the brain gets it
    ledger.set_fact("c2", cv.TREE_FACT, tree("c2", [heir("Bo Example", "son", "1/1", 1201)], pdf=False))
    ledger.set_fact("c8", cv.TREE_FACT, tree("c8", [heir("Cal Example", "son", "1/1", 1801)]))
    ledger.set_fact("c9", cv.TREE_FACT, tree("c9", [heir("Dee Example", "wife", "1/1", 1901)]))
    ledger.set_fact("c11", cv.TREE_FACT, tree("c11", [heir("Eve Example", "daughter", "1/1", 2101)]))


def _week(check) -> None:
    from .autopilot import curative as cv
    from .autopilot import ledger, policy
    print("  a week on the curative board")
    _clean()
    _week_trees()
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    crm = CurCrm(_week_world())

    def where(u):
        return (ledger.fact(u, cv.FACT) or {}).get("column")

    s = _sweep("c_board", crm, at(16, 8, 35), pol)
    check({u: where(u) for u in ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "c9", "c11")}
          == {"c1": "Day 1", "c2": "Research", "c3": "Research", "c4": None, "c5": None, "c6": None, "c7": None,
              "c8": "Day 1", "c9": "Day 1", "c11": None},
          "each tagged record is placed where it belongs, and a lead, Do Not Market, a company, another program's research "
          "tag and an untagged record are not: %s" % s.head)
    check(crm.in_column("Day 1") == {"c1", "c8", "c9"} and crm.in_column("Research") == {"c2", "c3"},
          "and the cards are on the board in those columns")
    check(s.tally.get("in Research: the heir map (the PDF) is not on the record yet", 0) == 0
          and s.live and "Placed 5" in s.head, "a live run places five cards: %s" % s.head)
    check("Ready for Marketing 3" in s.head and "started 3 house(s) into Day 1 (daily_starts 10)" in s.head
          and ("card_move", "c1", crm.cols["Day 1"]) in crm.writes,
          "the three ready houses are placed in Ready for Marketing and this morning pass starts them into Day 1")
    check(any("tag Tax Sale Redemption" in k for k in s.tally),
          "a record another program's research tag holds (Tax Sale Redemption) is left alone: the board lifts only its own tag")
    t1 = crm.open_tasks("c1")
    check(len(t1) == 2 and all(t["assigned_to_users"] == ["u-dana"] for t in t1)
          and sorted(t["title"] for t in t1) == ["Curative Day 1: call Kim (daughter, 1/3 share), number ending 1101",
                                                 "Curative Day 1: call Lee (son, 1/3 share), number ending 1102"],
          "Day 1 writes one call task per heir with a number, for the curative caller, with the last four digits only: %s"
          % [t["title"] for t in t1])
    check("No number yet for Ann (granddaughter, 1/6 share)" in t1[0]["notes"] and "Not confirmed living: Ray" in t1[0]["notes"]
          and "Call 1 is discovery" in t1[0]["notes"], "the task says who has no number yet and who is not confirmed living")
    check(not _ten_digits([t["title"] for t in crm.tasks.values()], [t.get("notes") for t in crm.tasks.values()], s.head, s.lines),
          "no task, line or head shows a phone number in full")
    check(s.tally.get("letters owed, held while mail is held (daily_pieces is 0)", 0) == 2,
          "letters are owed to the two heirs with a full address, and held while daily_pieces is 0")
    check(_texts_count() == 0 and not [b for b in ledger.batches() if "text" in b["kind"]],
          "the board never texts: nothing is staged for any texting tool")
    check((ledger.fact("c1", cv.TREE_FACT) or {}).get("record_uuid") == "c1",
          "the loader's heir file on the box is read into the board's memory")

    # Monday: Kim is dialed, Lee is not. The foreclosure-less one-heir records are dialed.
    crm.log_rows += [call_ev("w1", "o-c1", at(16, 10, 0), num(1101)), call_ev("w2", "o-c8", at(16, 10, 30), num(1801)),
                     call_ev("w3", "o-c9", at(16, 11, 0), num(1901))]
    s = _sweep("c_board", crm, at(17, 8, 35), pol)
    check(where("c1") == "Day 1" and "no call to every heir with a number on Mon Nov 16" in s.head,
          "a day with an heir whose best number was not dialed does not count: the card holds and the run says so")
    check(where("c8") == "Day 2" and where("c9") == "Day 2", "a day with every heir dialed moves the card one column")
    t1 = crm.open_tasks("c1")
    check(len(t1) == 2 and all(t["title"].startswith("Curative Day 1") for t in t1)
          and sum(1 for w in crm.writes if w[0] == "task_delete" and w[1] == "c1") == 2,
          "the day's tasks are written fresh and yesterday's open ones are taken off, so a caller sees today's calls")
    check([t["title"] for t in crm.open_tasks("c8")] == ["Curative Day 2: call Cal (son, 1/1 share), number ending 1801"],
          "a card that moved gets the new day's task")

    # Tuesday: both of c1's heirs are dialed; c9's record becomes a lead.
    crm.log_rows += [call_ev("w4", "o-c1", at(17, 10, 0), num(1101)), call_ev("w5", "o-c1", at(17, 10, 5), num(1102)),
                     call_ev("w6", "o-c8", at(17, 10, 30), num(1801)), status_ev("w7", "c9", at(17, 12, 0), None, "Warm Lead")]
    crm.recs["c9"]["status"] = "Warm Lead"
    tasks_c9 = len(crm.open_tasks("c9"))
    n0 = len(crm.writes)
    s = _sweep("c_board", crm, at(18, 8, 35), pol)
    check(where("c1") == "Day 2", "with every heir with a number dialed, the round moves on; the heir with no number never held it")
    check(where("c9") == "Heir Engaged" and "c9" in crm.in_column("Heir Engaged"), "a lead status takes the card to Heir Engaged")
    c9_writes = [w for w in crm.writes[n0:] if len(w) > 1 and w[1] == "c9"]
    check(c9_writes == [("card_move", "c9", crm.cols["Heir Engaged"])] and len(crm.open_tasks("c9")) == tasks_c9,
          "a lead is touched by nothing but its card move: its tasks stay as they are")
    check(cv.ALERT % "19 Heir St" in s.alerts, "the team is told once: %s" % s.alerts)
    n1 = len([w for w in crm.writes if w[0].startswith("card")])
    _sweep("c_board", crm, at(18, 8, 40), pol)
    n2 = len([w for w in crm.writes if w[0].startswith("card")])
    check(n2 == n1 and where("c1") == "Day 2" and where("c8") == "Day 3", "a second pass the same morning moves nothing")

    # Wednesday: Lee picks up for a minute and a half.
    crm.log_rows += [call_ev("w8", "o-c1", at(18, 11, 0), num(1102), kind="answered", duration=90),
                     call_ev("w9", "o-c8", at(18, 10, 30), num(1801))]
    s = _sweep("c_board", crm, at(18, 17, 0), pol)
    check(where("c1") == "Heir Engaged", "an answered call of a minute or more on an heir's number takes the card to Heir Engaged, "
                                         "even on a day the card already moved")
    check(cv.ALERT % "11 Heir St" in s.alerts and not _ten_digits(s.alerts) and _no_dash(s.alerts),
          "one alert names the house and never a phone number: %s" % s.alerts)
    check(not crm.open_tasks("c1"), "Heir Engaged stops the day tasks: the open ones come off")
    s = _sweep("c_board", crm, at(19, 8, 35), pol)
    check(not crm.open_tasks("c1") and where("c1") == "Heir Engaged" and where("c8") == "Day 4",
          "no new task is written for a card in Heir Engaged")
    check(cv.ALERT % "11 Heir St" not in s.alerts, "the alert is said on the move only, not again the next morning")

    crm.log_rows += [call_ev("w10", "o-c8", at(19, 10, 30), num(1801))]
    _sweep("c_board", crm, at(20, 8, 35), pol)
    check(where("c8") == "Day 5", "a fifth day of the round")
    crm.log_rows += [call_ev("w11", "o-c8", at(20, 10, 30), num(1801))]
    s = _sweep("c_board", crm, at(23, 8, 35), pol)
    left = crm.open_tasks("c8")
    check(where("c8") == "Exhausted" and len(left) == 1 and left[0]["title"].startswith("Curative follow-up")
          and left[0]["due_date"][:10] >= "2026-12-22",
          "five rounds and no heir engaged: Exhausted, and one call task thirty days out: %s" % [t["title"] for t in left])
    check(_texts_count() == 0 and _no_dash(s.head, s.lines, s.problems), "a week on the board sent no text and says nothing with a dash")


# ---------------------------------------------------------------------- Ready for Marketing and the morning start
def _ready_rec(u: str, n: int, tagged: bool = True) -> dict:
    """A one-heir curative house at `n` Ready St; its heir's number ends in n."""
    return crec(u, "%d Ready St" % n, phones=[phone(n, tags=["Son", "Heir", "Curative"])],
                tags=["Curative", "Courthouse Data"] if tagged else ["Courthouse Data"])


def _ready_tree(u: str, n: int, pdf: bool = True) -> None:
    from .autopilot import curative as cv
    from .autopilot import ledger
    ledger.set_fact(u, cv.TREE_FACT, tree(u, [heir("Kip Example", "son", "1/1", n, street="%d Home Rd" % n, city="Knoxville",
                                                   state="TN", zip5="37917")], pdf=pdf))


def _ready_world(*specs) -> list:
    """(record id, number, tagged) -> the records, each with its heir tree on this box."""
    recs = []
    for u, n, tagged in specs:
        recs.append(_ready_rec(u, n, tagged))
        _ready_tree(u, n)
    return recs


def _ready(check) -> None:
    from .autopilot import curative as cv
    from .autopilot import ledger, policy
    print("  Ready for Marketing and the morning start")

    def where(u):
        return (ledger.fact(u, cv.FACT) or {}).get("column")

    def card_writes(crm):
        return [w for w in crm.writes if w[0].startswith("card")]

    # Two houses join on Monday evening, two more on Tuesday morning; two may start a day.
    _clean()
    pol = policy.parse(copy.deepcopy(playbook(daily_starts=2, daily_pieces=5)), EMPLOYEES)
    crm = CurCrm(_ready_world(("rz1", 6101, True), ("rz2", 6102, True), ("ra1", 6103, False), ("ra2", 6104, False)))
    s = _sweep("c_board", crm, at(16, 17, 0), pol)
    check(where("rz1") == cv.READY and where("rz2") == cv.READY and crm.in_column(cv.READY) == {"rz1", "rz2"}
          and not crm.in_column("Day 1"),
          "a ready house is placed in Ready for Marketing, never straight into Day 1: %s" % s.head)
    check(not crm.tasks and not s.batch and s.tally.get("in Ready for Marketing, waiting for the next morning's start") == 2,
          "a card in Ready for Marketing gets no call task and no letter, and an afternoon pass starts nobody")
    for u in ("ra1", "ra2"):
        crm.recs[u]["tags"].append("Curative")
    s = _sweep("c_board", crm, at(17, 8, 35), pol)
    check(crm.in_column("Day 1") == {"rz1", "rz2"} and crm.in_column(cv.READY) == {"ra1", "ra2"},
          "the morning pass starts daily_starts houses, the longest-waiting first: the two waiting since last evening, not "
          "the two placed this morning (whose ids sort first): %s" % s.head)
    check("started 2 house(s) into Day 1 (daily_starts 2)" in s.head and "from Ready for Marketing" not in s.head
          and any(line.startswith("Started today: 6101 Ready St; 6102 Ready St") for line in s.lines),
          "the run says the starts on their own and names the houses: %s" % s.head)
    check({t["assigned_to_property"] for t in crm.open_tasks()} == {"rz1", "rz2"}
          and all(t["title"].startswith("Curative Day 1: call Kip (son, 1/1 share)") for t in crm.open_tasks()),
          "the start is the house's Day 1: its call tasks are written, and none for the houses still waiting")
    b = ledger.batch(s.batch) if s.batch else None
    check(b is not None and {it["record_uuid"] for it in b["items"]} == {"rz1", "rz2"},
          "the letters fall due for the started houses only")
    n0 = len(card_writes(crm))
    s = _sweep("c_board", crm, at(17, 8, 50), pol)
    check(len(card_writes(crm)) == n0 and crm.in_column(cv.READY) == {"ra1", "ra2"}
          and s.tally.get("in Ready for Marketing, waiting its turn (daily_starts 2)") == 2,
          "a second morning pass the same day starts no more: the day's starts are remembered")
    crm.log_rows += [call_ev("rd1", "o-rz1", at(17, 10, 0), num(6101)), call_ev("rd2", "o-rz2", at(17, 10, 5), num(6102))]
    s = _sweep("c_board", crm, at(17, 17, 0), pol)
    check(len(card_writes(crm)) == n0 and crm.in_column(cv.READY) == {"ra1", "ra2"}
          and {t["assigned_to_property"] for t in crm.open_tasks()} == {"rz1", "rz2"},
          "nor does the afternoon pass, and still no task for a waiting card")
    s = _sweep("c_board", crm, at(18, 8, 35), pol)
    check(crm.in_column("Day 2") == {"rz1", "rz2"} and crm.in_column("Day 1") == {"ra1", "ra2"} and not crm.in_column(cv.READY),
          "the next morning the first two move a day and the next two start: never more than daily_starts in one day: %s %s"
          % ({t: sorted(crm.in_column(t)) for t in cv.COLUMNS if crm.in_column(t)}, s.head))
    check(len([w for w in crm.writes if w[0] == "card_move" and w[2] == crm.cols["Day 1"]]) == 4,
          "four starts over two days, two a day")

    # The read cap never cuts the head of the line.
    _clean()
    crm = CurCrm(_ready_world(("rq-z", 6201, True), ("rq-a", 6202, False), ("rq-b", 6203, False)))
    one = policy.parse(copy.deepcopy(playbook(daily_starts=1)), EMPLOYEES)
    _sweep("c_board", crm, at(16, 17, 0), one)
    for u in ("rq-a", "rq-b"):
        crm.recs[u]["tags"].append("Curative")
    _sweep("c_board", crm, at(17, 17, 0), one)
    check(crm.in_column(cv.READY) == {"rq-z", "rq-a", "rq-b"}, "three houses wait, one since Monday, two since Tuesday")
    capped = policy.parse(copy.deepcopy(playbook(daily_starts=1, max_records=1)), EMPLOYEES)
    s = _sweep("c_board", crm, at(18, 8, 35), capped)
    check(crm.in_column("Day 1") == {"rq-z"} and crm.in_column(cv.READY) == {"rq-a", "rq-b"},
          "with the run's read cap at one record, the card read is the one waiting longest, so it is the one that starts: %s"
          % s.head)
    crm.recs["rq-b"]["owner"]["phones"][0]["status"] = "WRONG"     # its heir's only number turns out wrong
    s = _sweep("c_board", crm, at(19, 8, 35), one)
    check(where("rq-b") == cv.RESEARCH and crm.in_column("Day 1") >= {"rq-a"} and not crm.open_tasks("rq-b"),
          "a waiting card that is no longer ready goes back to Research, and the next in line starts in its place: %s" % s.head)

    # The Day 1 cards placed before Ready for Marketing existed stay where they are.
    _clean()
    crm = CurCrm(_ready_world(("od1", 6301, True), ("od2", 6302, True), ("od3", 6303, True), ("nw1", 6311, True),
                              ("nw2", 6312, True)))
    for u in ("od1", "od2", "od3"):
        crm.cards["card-%s" % u] = {"column": crm.cols["Day 1"], "prop": u}
        ledger.set_fact(u, cv.FACT, {"card": "card-%s" % u, "column": "Day 1", "column_uuid": crm.cols["Day 1"],
                                     "entered": "2026-11-23T17:00:00-05:00", "placed": "2026-11-23T17:00:00-05:00",
                                     "due": "2026-11-24", "moved_day": "2026-11-23", "owner": "o-" + u, "source": "placed",
                                     "why": "the heir tree is ready"})
    s = _sweep("c_board", crm, at(24, 8, 35), one)
    check(crm.in_column("Day 1") == {"od1", "od2", "od3", "nw1"} and crm.in_column(cv.READY) == {"nw2"},
          "the three cards already in Day 1 stay, and do not count against the day's starts: one new house starts beside them")
    check(not [w for w in crm.writes if w[0] in ("card_move", "card_remove") and w[1].startswith("od")]
          and all((ledger.fact(u, cv.FACT) or {}).get("card") == "card-%s" % u for u in ("od1", "od2", "od3"))
          and {t["assigned_to_property"] for t in crm.open_tasks()} == {"od1", "od2", "od3", "nw1"},
          "nothing moves an old Day 1 card; each keeps its card and gets its Day 1 call task on the day it was due")

    # An heir who talks with us, from Ready for Marketing or from Research.
    _clean()
    crm = CurCrm([_ready_rec("eg1", 6401), _ready_rec("eg2", 6402)])
    _ready_tree("eg1", 6401)
    _ready_tree("eg2", 6402, pdf=False)               # its heir map is not on the record yet: Research
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    _sweep("c_board", crm, at(16, 17, 0), pol)
    check(where("eg1") == cv.READY and where("eg2") == cv.RESEARCH, "one card waits in Ready for Marketing, one in Research")
    crm.log_rows += [call_ev("eg-a", "o-eg1", at(16, 17, 30), num(6401), kind="answered", duration=95),
                     call_ev("eg-b", "o-eg2", at(16, 17, 40), num(6402), kind="received", duration=120)]
    s = _sweep("c_board", crm, at(17, 8, 35), pol)
    check(where("eg1") == cv.ENGAGED and where("eg2") == cv.ENGAGED and cv.ALERT % "6401 Ready St" in s.alerts
          and cv.ALERT % "6402 Ready St" in s.alerts and not crm.open_tasks() and "started" not in s.head,
          "an heir who talks with us takes the card to Heir Engaged from Ready for Marketing and from Research too: %s" % s.alerts)
    check(_texts_count() == 0 and _no_dash(s.head, s.lines), "and still no text, and no dash")


def _more(check) -> None:
    from .autopilot import curative as cv
    from .autopilot import ledger, policy
    print("  more rules")
    _clean()
    _week_trees()
    pol = policy.parse(copy.deepcopy(playbook(lanes={"heirs": {"tags": ["Heir Identified"]}})), EMPLOYEES)
    s = _sweep("c_board", CurCrm(_week_world()), at(16, 8, 35), pol)
    check(any("set `heirs` under lanes" in p for p in s.problems),
          "a playbook whose heirs lane does not carry the Curative tag is told which setting to make")

    _clean()
    _week_trees()
    pol = policy.parse(copy.deepcopy(playbook(caller="Nobody")), EMPLOYEES)
    crm = CurCrm(_week_world())
    s = _sweep("c_board", crm, at(16, 8, 35), pol)
    check(not crm.tasks and s.tally.get("call tasks not written: no curative caller", 0) >= 1
          and any("curative caller is not named" in p for p in s.problems) and (ledger.fact("c1", cv.FACT) or {}).get("column") == "Day 1",
          "with no caller the cards still move, no task is written, and the run says which setting to make")

    _clean()
    _week_trees()
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    crm = CurCrm(_week_world())
    _sweep("c_board", crm, at(16, 8, 35), pol)
    crm.recs["c8"]["tags"].append("Do Not Market")
    crm.log_rows += [call_ev("m1", "o-c1", at(16, 10, 0), num(1101)), call_ev("m2", "o-c1", at(16, 10, 5), num(1102))]
    capped = policy.parse(copy.deepcopy(playbook(max_records=3)), EMPLOYEES)
    s = _sweep("c_board", crm, at(17, 8, 35), capped)
    check((ledger.fact("c1", cv.FACT) or {}).get("column") == "Day 2" and s.tally.get("not judged this run (read cap; next run)", 0) >= 2,
          "the days of the round are judged first; the cards waiting in Research are the ones the read cap leaves for the next run")
    check(ledger.fact("c8", cv.FACT) is None and "c8" not in {c["prop"] for c in crm.cards.values()} and not crm.open_tasks("c8"),
          "a record that goes Do Not Market leaves the board, and its open call task comes off with it")

    _clean()
    from .autopilot import ledger as lg
    twins = [crec("cA", "51 Twin St", phones=[phone(5101)]), crec("cB", "53 Twin St", phones=[phone(5101)])]
    for r in twins:
        r["owner"]["uuid"] = "o-shared"
        lg.set_fact(r["uuid"], cv.TREE_FACT, tree(r["uuid"], [heir("Gus Example", "son", "1/1", 5101)]))
    crm = CurCrm(twins)
    s = _sweep("c_board", crm, at(16, 8, 35), pol)
    check(len(crm.open_tasks()) == 1 and s.tally.get("call tasks already written today on another house of the same owner") == 1,
          "one family on two houses gets one call task for its heir, not two")
    crm.log_rows += [call_ev("t1", "o-shared", at(16, 10, 0), num(5101))]
    _sweep("c_board", crm, at(17, 8, 35), pol)
    check({(lg.fact(u, cv.FACT) or {}).get("column") for u in ("cA", "cB")} == {"Day 2"},
          "and the one dial counts for both houses, since the call log files it under their one owner")

    _clean()
    lg.set_fact("cM", cv.TREE_FACT, tree("cM", [heir("Hal Example", "son", "1/1", 5501)]))
    crm = CurCrm([crec("cM", "55 Mail St", phones=[phone(5501)], tags=["Curative", "Mail Only"])])
    _sweep("c_board", crm, at(16, 8, 35), pol)
    f = lg.fact("cM", cv.FACT) or {}
    check(f.get("column") == "Research" and "Mail Only" in str(f.get("why")) and "gate_allow" in str(f.get("why")) and not crm.tasks,
          "a record still tagged Mail Only from before waits in Research, and the card says how to clear it: %s" % f.get("why"))
    with_mail = policy.parse(copy.deepcopy(playbook(gate_allow=["deceased", "heir", "in_research", "mail_only"])), EMPLOYEES)
    _clean()
    lg.set_fact("cM", cv.TREE_FACT, tree("cM", [heir("Hal Example", "son", "1/1", 5501)]))
    crm = CurCrm([crec("cM", "55 Mail St", phones=[phone(5501)], tags=["Curative", "Mail Only"])])
    _sweep("c_board", crm, at(16, 8, 35), with_mail)
    check((lg.fact("cM", cv.FACT) or {}).get("column") == "Day 1" and len(crm.open_tasks("cM")) == 1,
          "with mail_only in the stage's gate_allow, the heirs on that record are called")


def _plan(check) -> None:
    from .autopilot import curative as cv
    from .autopilot import ledger, policy
    print("  plan mode")
    _clean()
    _week_trees()
    pol = policy.parse(copy.deepcopy(playbook(mode="plan")), EMPLOYEES)
    crm = CurCrm(_week_world())
    s = _sweep("c_board", crm, at(16, 8, 35), pol)
    check(not s.live and s.head.startswith("Would place 5") and "write 4 call task(s)" in s.head and not crm.sent() and not crm.writes,
          "a plan run says what it would place and write, and sends nothing to DataSift: %s" % s.head)
    check("start 3 house(s) into Day 1" in s.head and not _live_starts(),
          "a plan run says which houses it would start, and its starts are kept apart from the live board's")
    check(ledger.fact("c1", cv.FACT) is None and (ledger.fact("c1", cv.PLAN_FACT) or {}).get("column") == "Day 1"
          and not ledger.changes(), "the plan's board lives in its own memory, and no change is recorded to put back")
    crm.log_rows += [call_ev("p1", "o-c8", at(16, 10, 30), num(1801))]
    s = _sweep("c_board", crm, at(17, 8, 35), pol)
    check((ledger.fact("c8", cv.PLAN_FACT) or {}).get("column") == "Day 2" and "Would move" in s.head and not crm.sent(),
          "on the second morning the plan shows the move the live board would make: %s" % s.head)


def _letters(check) -> None:
    from . import release as rel
    from .autopilot import curative as cv
    from .autopilot import heirlane, ledger, policy
    print("  letters")
    _clean()
    _week_trees()
    pol = policy.parse(copy.deepcopy(playbook(daily_pieces=5)), EMPLOYEES)
    crm = CurCrm([r for r in _week_world() if r["uuid"] in ("c1", "c8")])
    s = _sweep("c_board", crm, at(16, 8, 35), pol)
    b = ledger.batch(s.batch) if s.batch else None
    names = sorted(it["name"] for it in (b or {}).get("items") or [] if it["record_uuid"] == "c1")
    check(b is not None and b["kind"] == cv.KIND_MAIL and b["status"] == "staged" and names == ["Ann Example", "Kim Example"],
          "one letter per living verified heir with a full address (street, city, state, a 5-digit zip): %s" % names)
    check(s.tally.get("heir with no full address (street, city, state and a 5-digit zip): no letter", 0) >= 1
          and "Ray Example" not in names and "Lee Example" not in names,
          "no letter without a full address, and none to an heir not confirmed living")
    items = b["items"]
    check(all(it["street"] and len(it["zip"]) == 5 and it["street"] != it["property_street"]
              and not cv.audit_letter(it["text"], it["property_street"]) and not heirlane.DEATH_WORDS.search(it["text"].replace(it["property_street"], ""))
              for it in items),
          "every staged letter goes to the heir's own address and passes the audit (no death, no price, no legal word)")
    path = Path(config.DATA_DIR) / "mail" / ("batch_%d.csv" % b["id"])
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    check(len(rows) == len(items) and rows[0]["zip"], "the batch is a file a person hands to the mail house")
    check(not [e for e in ledger.events(action="mail")], "nothing is mailed until a person releases it")
    s2 = _sweep("c_board", crm, at(16, 8, 50), pol)
    check(not s2.batch, "a letter already staged is not staged again")
    dry = rel.run(b["id"], commit=False, crm=crm, pol=pol, now=at(16, 9, 0))
    check(dry.get("dry") and dry.get("would_hold") == 0, "a dry release checks every letter and changes nothing")
    first = rel.run(b["id"], commit=True, crm=crm, pol=pol, now=at(16, 9, 5))
    check(first.get("file") and first.get("next") and ledger.batch(b["id"])["status"] == "staged",
          "the first step writes the file fresh and waits for the upload")
    done = rel.run(b["id"], commit=True, uploaded=True, crm=crm, pol=pol, now=at(16, 9, 30))
    mailed = [e for e in ledger.events(action="mail") if e["detail"].get("series") == cv.SERIES]
    check(done.get("sent") == len(items) and len(mailed) == len(items) and ledger.batch(b["id"])["status"] == "sent"
          and any("Mailed: curative letter" in t for _, t in crm.msgs),
          "after the upload each letter is written down under its heir, with a note on the record")
    s3 = _sweep("c_board", crm, at(17, 8, 35), pol)
    check(not s3.batch, "an heir mailed once is not mailed again: one letter each")
    # A letter staged, then the record went Do Not Market: held at release.
    _clean()
    _week_trees()
    crm = CurCrm([r for r in _week_world() if r["uuid"] == "c1"])
    s = _sweep("c_board", crm, at(16, 8, 35), pol)
    crm.recs["c1"]["tags"].append("Do Not Market")
    res = rel.run(s.batch, commit=True, crm=crm, pol=pol, now=at(16, 9, 0))
    check(res.get("cancelled") and ledger.batch(s.batch)["status"] == "cancelled",
          "a letter whose record went Do Not Market after it was staged is held at release")
    check(rel.RELEASE.get(cv.KIND_MAIL) == ("autopilot.curative", "release") and cv.KIND_MAIL in rel.FILE_KINDS
          and rel.label(cv.KIND_MAIL) == "curative letter(s)",
          "the release command knows the curative letters, as a two-step file batch")


def _undo(check) -> None:
    from .autopilot import curative as cv
    from .autopilot import ledger, policy, undo
    print("  undo")
    _clean()
    _week_trees()
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    crm = CurCrm([r for r in _week_world() if r["uuid"] == "c8"])
    _sweep("c_board", crm, at(16, 8, 35), pol)
    crm.log_rows += [call_ev("u1", "o-c8", at(16, 10, 30), num(1801))]
    _sweep("c_board", crm, at(17, 8, 35), pol)
    runs = [r["run_id"] for r in ledger.runs() if r["stage"] == "c_board"]
    check(len(runs) == 2, "each live run that changed the board is a run undo can find")
    later = runs[0]
    steps = undo.plan(later)
    check({st["what"] for st in steps} >= {"card_move", "task_create"} and all(st["do"] for st in steps if st["what"] != "task_complete"),
          "a card move and a task the board wrote both have an undo step")
    res = undo.run(later, crm=crm, commit=True)
    check(res.get("undone", 0) >= 2 and crm.in_column("Day 1") == {"c8"}, "undoing the move puts the card back in Day 1")
    made = next(c for c in ledger.changes(include_undone=True) if c["what"] == "card_create" and c["record_uuid"] == "c8")
    check((made["after"] or {}).get("kind") == cv.KIND, "a card this board placed says so, for the undo that removes it")
    k = next(iter(crm.cards))
    crm.cards[k]["column"] = crm.cols["Day 3"]
    ledger.set_fact("c8", cv.FACT, dict(ledger.fact("c8", cv.FACT) or {}, column="Day 3", column_uuid=crm.cols["Day 3"]))
    check(cv.undo_create(crm, made) == "1 card(s) removed" and not crm.cards and ledger.fact("c8", cv.FACT) is None,
          "undoing the placement takes the card off the board, from wherever it sits")


def _research_path(check, tmp) -> None:
    from . import store
    from .autopilot import curative as cv
    from .autopilot import ledger, policy
    print("  heirs from the record's own research; the heir lane leaves it alone")
    _clean()
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    recs = [crec("r10", "30 Research St", phones=[phone(3001, tags=["Daughter"])], tags=["Heir Identified", "Curative"]),
            crec("r12", "32 Research St", phones=[phone(3201, tags=["Son"])], tags=["Heir Identified"])]
    crm = CurCrm(recs)
    for u, slug, who, n in (("r10", "30-research-st", "Kim Sample", 3001), ("r12", "32-research-st", "Lee Sample", 3201)):
        run_dir = Path(tmp) / "curative_runs" / slug
        (run_dir / "analysis").mkdir(parents=True, exist_ok=True)
        full = {"owner_status": "Deceased", "must_sign": [{"who": who, "status": "Living"}],
                "call_order": [{"who": who, "number": num(n)}], "slug": slug, "run_dir": str(run_dir), "verified": [who]}
        (run_dir / "agent_result.json").write_text(json.dumps(full), encoding="utf-8")
        (run_dir / "analysis" / (slug + ".json")).write_text(json.dumps({"slug": slug, "people": [
            {"name": who, "relationship": "daughter of Pat Sample", "living": "Living", "role": "signer",
             "trace_address": {"street": "7 Ash Ct", "city": "Knoxville", "state": "TN", "zip": "37917"}}]}), encoding="utf-8")
        (run_dir / "writeback_audit.json").write_text(json.dumps({slug: {"document": {"filed": True, "name": "deep-prospect-%s.pdf" % slug}}}),
                                                      encoding="utf-8")
        tid, _ = store.enqueue("research", "deep_prospect", u, force=True)
        store.finish(tid, "done", full)
    s = _sweep("c_board", crm, at(16, 8, 35), pol)
    t = cv.research_tree("r10", crm.record("r10"), "deep_prospect")
    check((ledger.fact("r10", cv.FACT) or {}).get("column") == "Day 1" and t and t["source"] == "research"
          and cv.pdf_on(t) and t["heirs"][0]["relationship"] == "daughter",
          "a record with its own research on the box is worked from that research, its PDF read from the write-back: %s" % s.head)
    check("r12" not in {u for u in crm.in_column("Day 1") | crm.in_column("Research")},
          "a record without the Curative tag never reaches the curative board")
    n0 = len(crm.writes)
    h = _sweep("heir_lane", crm, at(16, 8, 20), pol)
    r10 = [w for w in crm.writes[n0:] if len(w) > 1 and w[1] == "r10"]
    r12 = [w for w in crm.writes[n0:] if len(w) > 1 and w[1] == "r12"]
    check(not r10 and r12 and h.tally.get("on the Curative Title board: worked there, not here") == 1,
          "the heir lane leaves a curative record alone and still works its own: %s" % h.tally)


# ---------------------------------------------------------------------- the loader
def _house(root: Path, slug: str, street: str, heirs: list, checked: list | None = None, story: bool = True,
           occupancy: str = "Unknown. The county mails the bill there.") -> None:
    folder = root / "houses" / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "heirs.json").write_text(json.dumps({"house": street, "slug": slug, "heirs": heirs, "occupancy": occupancy,
                                                   "confidence": "high"}), encoding="utf-8")
    (folder / "input.json").write_text(json.dumps({"street": street, "city": "Knoxville", "zip": "37917"}), encoding="utf-8")
    if checked is not None:
        (folder / "phones_checked.json").write_text(json.dumps(checked), encoding="utf-8")
    if story:
        (folder / "story.md").write_text("# %s\n\nThe heir story for %s.\n" % (street, street), encoding="utf-8")


def _pilot(root: Path) -> None:
    def h(name, rel, share, living="yes", conf="high", street="", city="", state="", phones=()):
        return {"name": name, "relationship": rel, "share": share, "share_num": 0.5 if share == "1/2" else 1.0,
                "living": living, "confidence": conf, "street": street, "city": city, "state": state,
                "phones_found": list(phones)}

    def ck(heir, n, grade="A", activity=95, match=True, lit=False, kind="Mobile"):
        return {"heir": heir, "phone": num(n), "match": match, "grade": grade, "activity": activity, "linetype": kind,
                "valid": True, "litigator": lit, "error": None, "when": "2026-11-20T10:00:00"}
    _house(root, "101-alpha-st", "101 Alpha St",
           [h('Kim "Kimmy" Sample', "daughter of Pat Sample", "1/2", street="5 Maple Ave", city="Maryville", state="Tennessee"),
            h("Lee Sample", "son of Pat Sample", "1/2", living="yes (as of Dec 2021)", conf="medium", city="Alcoa", state="TN"),
            h("Moe Sample", "son", "", living="no"),
            h("Ray Sample", "nephew", "1/4", living="unknown", conf="low")],
           [ck("Kim Sample", 2001), ck("Kim Sample", 2002, activity=30, kind="Landline"), ck("Lee Sample", 2003, grade="B", activity=70,
                                                                                            kind="Landline"),
            ck("Lee Sample", 2004, grade="F"), ck("Ray Sample", 2005, activity=90), ck("Kim Sample", 2006, lit=True),
            ck("Lee Sample", 2007, activity=85)])
    _house(root, "102-beta-rd", "102 Beta Rd", [h("Ann Sample", "daughter", "1/1", street="9 Cedar Ln", city="Knoxville", state="TN")], [])
    _house(root, "103-gamma-ln", "103 Gamma Ln", [h("Bo Sample", "son", "1/1")], [])
    _house(root, "104-delta-dr", "104 Delta Dr", [h("Cy Sample", "son", "1/1")], [])
    _house(root, "105-eps-ct", "105 Eps Ct", [h("Di Sample", "son", "1/1")], [])
    _house(root, "106-zeta-way", "106 Zeta Way", [h("Ed Sample", "son", "1/1")], [])
    _house(root, "107-eta-pl", "107 Eta Pl", [h("Flo Sample", "daughter", "1/1")], [])
    rows = [{"slug": s, "street": st, "verdict": v, "equity_per_heir": e} for s, st, v, e in (
        ("101-alpha-st", "101 Alpha St", "Work now", 90000), ("102-beta-rd", "102 Beta Rd", "Work now, no confirmed number yet", 70000),
        ("103-gamma-ln", "103 Gamma Ln", "Work now", 60000), ("104-delta-dr", "104 Delta Dr", "Work now", 55000),
        ("105-eps-ct", "105 Eps Ct", "Work now", 52000), ("106-zeta-way", "106 Zeta Way", "Research more", 99000),
        ("107-eta-pl", "107 Eta Pl", "Work now", 51000))]
    (root / "stories_results.json").write_text(json.dumps(rows), encoding="utf-8")
    scored = [{"street": "101 ALPHA ST", "zip": "37917", "uuids": ["rec-h1"], "dataflik_id": 551},
              {"street": "102 Beta Road", "zip": "37917", "uuids": ["rec-h2"], "dataflik_id": 552},
              {"street": "103 Gamma Ln", "zip": "37917", "uuids": [], "dataflik_id": 555001},
              {"street": "104 Delta Dr", "zip": "37917", "uuids": ["rec-other"], "dataflik_id": 554},
              {"street": "105 Eps Ct", "zip": "37917", "uuids": ["rec-old"], "dataflik_id": 556}]
    (root.parent / "work").mkdir(parents=True, exist_ok=True)
    (root.parent / "work" / "scored.json").write_text(json.dumps(scored), encoding="utf-8")


def _loader_world() -> list:
    return [_rec("rec-h1", "101 Alpha St", phones=[phone(2007, status="WRONG", tags=["Dial First"])], tags=[], deceased=True),
            _rec("rec-h2", "102 Beta Rd", tags=[], deceased=True),
            _rec("rec-h4", "104 Delta Dr", tags=[], deceased=True),
            _rec("rec-h7", "107 Eta Pl", tags=[], status="not_interested", deceased=True)]


def _loader(check, tmp) -> None:
    from .autopilot import curative as cv
    from .autopilot import curative_setup as cs
    from .autopilot import gate, ledger, policy
    print("  the loader")
    _clean()
    root = Path(tmp) / "curative_pilot" / "pilot"
    _pilot(root)
    out_dir = Path(config.DATA_DIR) / "curative"
    asked, slept, rendered, posted, said = [], [], [], [], []

    def geo(street, city, state):
        asked.append((street, city, state))
        return ("37801", "matched") if street == "5 Maple Ave" else ("", "no match")

    def render(story, pdf):
        rendered.append(str(story))
        Path(pdf).parent.mkdir(parents=True, exist_ok=True)
        Path(pdf).write_bytes(b"%PDF-1.4 curative test")
        return str(pdf)

    def post(url, fields, name, data):
        posted.append(name)
        return 204

    def load(crm, **kw):
        return cs.load(crm, results=root, commit=kw.pop("commit", False), geocode=geo, render=render, post=post,
                       out=said.append, folder=out_dir, gate_cfg=gate.GateConfig(), now=datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc),
                       sleep=slept.append, **kw)

    crm = CurCrm(_loader_world())
    rep = load(crm)
    c = rep["counts"]
    check(c["matched: both agree"] == 2 and c["matched: neither"] == 1 and c["matched: both disagree"] == 1
          and c["matched: old id only"] == 1 and c["matched: address only"] == 1,
          "each house is matched by its address, with the scoring's old id as a cross-check: %s" % dict(c))
    check([r["slug"] for r in rep["would"]] == ["101-alpha-st", "102-beta-rd"] and [r["slug"] for r in rep["held"]] == ["107-eta-pl"]
          and "not_interested" in rep["held"][0]["why"],
          "a dry run says which houses it would load, and holds a finished status the gate holds even from research")
    check(sorted(r["slug"] for r in rep["person"]) == ["104-delta-dr", "105-eps-ct"]
          and rep["not_in_datasift"] == [{"slug": "103-gamma-ln", "street": "103 Gamma Ln", "zip": "37917", "siftmap": ["555001"]}],
          "a disagreement and an old id alone go to a person; a house in neither is listed with its SiftMap id, never pulled")
    check(all(r["slug"] != "106-zeta-way" for r in rep["would"] + rep["person"] + rep["held"]) and rep["houses"] == 6,
          "only the houses the verdict says to work now are read")
    h1 = next(r for r in rep["would"] if r["slug"] == "101-alpha-st")
    check(h1["numbers"] == 2 and h1["verified"] == 2 and h1["heirs"] == 3,
          "only Dial First and Second confirmed numbers of living heirs are planned (no low tier, no F grade, no litigator, "
          "no unconfirmed heir, none a person marked wrong): %s" % h1)
    check(not crm.sent() and not crm.writes and not asked and not rendered and not (out_dir / "heirs" / "rec-h1.json").exists()
          and not (out_dir / "zip_cache.json").exists() and not (out_dir / "pdf").exists() and ledger.fact("rec-h1", cv.TREE_FACT) is None,
          "a dry run writes nothing, renders nothing and never asks the geocoder: %s" % (crm.sent()[:2] or crm.writes[:2] or asked[:1]))
    rep = load(crm, commit=True)
    check(not rep["allowed"] and not crm.writes and not asked, "--commit alone writes nothing: the env switch is the second key")
    check(not cs.may_write(True) and not cs.may_write(False), "may_write needs both switches")

    os.environ["CURATIVE_ALLOW"] = "1"
    try:
        rep = load(crm, commit=True)
    finally:
        os.environ.pop("CURATIVE_ALLOW", None)
    check(rep["allowed"] and [r["slug"] for r in rep["loaded"]] == ["101-alpha-st", "102-beta-rd"],
          "a real run loads the two houses it matched")
    check(asked == [("5 Maple Ave", "Maryville", "TN"), ("9 Cedar Ln", "Knoxville", "TN")] and len(slept) == 1,
          "the real run asks the geocoder one address a call, paced, and only for an heir with a street, city and state")
    cache = json.loads((out_dir / "zip_cache.json").read_text(encoding="utf-8"))
    check(cache.get("5 MAPLE AVE|MARYVILLE|TN", {}).get("zip") == "37801" and cache.get("9 CEDAR LN|KNOXVILLE|TN", {}).get("zip") == "",
          "every answer is kept, a miss too")
    owner = {p["number"]: p for p in crm.recs["rec-h1"]["owner"]["phones"]}
    check({"Daughter", "Heir", "Curative", "Dial First"} <= set(owner[num(2001)]["tags"])
          and {"Son", "Heir", "Curative", "Dial Second"} <= set(owner[num(2003)]["tags"])
          and num(2002) not in owner and num(2004) not in owner and num(2005) not in owner and num(2006) not in owner,
          "each living heir's confirmed numbers land on the owner tagged with the tie, Heir, Curative and the tier, and nothing else does")
    check(owner[num(2007)]["status"] == "WRONG" and "Curative" not in owner[num(2007)]["tags"],
          "a number a person marked wrong is left exactly as it is")
    w = [x for x in crm.writes if len(x) > 1 and x[1] in ("rec-h1", "o-rec-h1")]
    kinds = [x[0] for x in w]
    check(kinds.index("upsert") < kinds.index("doc") < kinds.index("message") < kinds.index("add_tags")
          and w[-1][0] == "add_tags" and w[-1][2] == ("Curative",),
          "the numbers, the PDF and the note go first; the Curative tag, the board's door, goes last: %s" % kinds)
    note = next(t for u, t in crm.pins if u == "rec-h1")
    check(note.startswith(cs.NOTE_MARK) and "Kim Sample, daughter, 1/2 share. Call first." in note and "Ray Sample" in note
          and "Not confirmed living" in note and "Moe Sample" not in note and not re.search(r"\d{10}", note)
          and "curative-title-101-alpha-st.pdf" in note,
          "one pinned note: the heirs, their shares and who to call first, never a phone number: %s" % note.replace("\n", " | "))
    t = ledger.fact("rec-h1", cv.TREE_FACT) or {}
    f = json.loads((out_dir / "heirs" / "rec-h1.json").read_text(encoding="utf-8"))
    check(t.get("record_uuid") == "rec-h1" and cv.pdf_on(t) and f == t and t["call_first"] == "kim sample"
          and next(x for x in t["heirs"] if x["name"] == "Kim Sample")["zip"] == "37801",
          "the heir tree is kept for the stage, as a fact and as the file the brain reads, with the zip the geocoder gave")
    check(posted == ["curative-title-101-alpha-st.pdf", "curative-title-102-beta-rd.pdf"] and len(rendered) == 2
          and crm.docs["rec-h1"][0]["filename"] == "curative-title-101-alpha-st.pdf",
          "the heir story is rendered and filed in the record's documents")
    n0, asked_before = len(crm.writes), len(asked)
    os.environ["CURATIVE_ALLOW"] = "1"
    try:
        load(crm, commit=True)
    finally:
        os.environ.pop("CURATIVE_ALLOW", None)
    again = [x[0] for x in crm.writes[n0:]]
    check("message" not in again and "add_tags" not in again and "doc" not in again and len(asked) == asked_before,
          "a second run pins no second note, files no second PDF, adds no tag twice and asks the geocoder nothing new: %s" % again)

    # The stage reads what the loader wrote, and works the house.
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    crm2 = CurCrm(_loader_world())
    crm2.recs = copy.deepcopy(crm.recs)
    s = _sweep("c_board", crm2, at(23, 8, 35), pol)
    check((ledger.fact("rec-h1", cv.FACT) or {}).get("column") == "Day 1"
          and (ledger.fact("rec-h2", cv.FACT) or {}).get("column") == "Research",
          "the board works the loaded house, and a house with no confirmed number waits in Research: %s" % s.head)
    check(cs.zip_from_census({"result": {"addressMatches": [{"matchedAddress": "5 MAPLE AVE, MARYVILLE, TN, 37801",
                                                             "addressComponents": {"zip": "37801"}}]}}, "5 Maple Ave") == ("37801", "matched")
          and cs.zip_from_census({"result": {"addressMatches": [{"matchedAddress": "7 MAPLE AVE, MARYVILLE, TN, 37801",
                                                                 "addressComponents": {"zip": "37801"}}]}}, "5 Maple Ave")[0] == ""
          and cs.zip_from_census({"result": {"addressMatches": [
              {"matchedAddress": "5 MAPLE AVE, A, TN, 37801", "addressComponents": {"zip": "37801"}},
              {"matchedAddress": "5 MAPLE AVE, B, TN, 37803", "addressComponents": {"zip": "37803"}}]}}, "5 Maple Ave")[0] == "",
          "a zip is taken only when the geocoder's matches agree on one, for the same house number")


# ---------------------------------------------------------------------- reset-statuses
RESET_SPEC = (    # slug, street, the status earlier marketing left, extra tags
    ("301-able-st", "301 Able St", "not_interested", ()),
    ("302-baker-st", "302 Baker St", "Hot Lead", ()),
    ("303-cedar-st", "303 Cedar St", "new_lead", ()),
    ("304-dover-st", "304 Dover St", "sold", ()),
    ("305-elm-st", "305 Elm St", "dnc", ()),
    ("306-fern-st", "306 Fern St", "opt_out", ()),
    ("307-grove-st", "307 Grove St", "Close Out", ()),
    ("308-hill-st", "308 Hill St", "Deep Prospecting", ()),
    ("309-iris-st", "309 Iris St", "Dead Lead", ()),
    ("310-june-st", "310 June St", None, ()),
    ("311-kale-st", "311 Kale St", "not_interested", ("Do Not Market",)),
    ("312-lark-st", "312 Lark St", "Exhausted", ()),
    ("313-moss-st", "313 Moss St", "under_contract", ()),
    ("314-nova-st", "314 Nova St", "follow_up", ()),
)
RESET_CLEAR = ["301-able-st", "302-baker-st", "303-cedar-st", "308-hill-st", "309-iris-st", "312-lark-st", "314-nova-st"]


def _reset_world(root: Path) -> list:
    """Work-now houses carrying what earlier marketing left, each with one living heir and a confirmed number,
    plus a house not in DataSift and one the research says to look at again."""
    def h(name):
        return {"name": name, "relationship": "son of Pat Sample", "share": "1/1", "share_num": 1.0, "living": "yes",
                "confidence": "high", "street": "8 Birch Rd", "city": "Knoxville", "state": "TN", "phones_found": []}
    rows, recs = [], []
    for i, (slug, street, status, tags) in enumerate(RESET_SPEC):
        checked = [{"heir": "Al Sample", "phone": num(3100 + i), "match": True, "grade": "A", "activity": 95,
                    "linetype": "Mobile", "valid": True, "litigator": False, "error": None, "when": "2026-11-20T10:00:00"}]
        _house(root, slug, street, [h("Al Sample")], checked)
        rows.append({"slug": slug, "street": street, "verdict": "Work now", "equity_per_heir": 50000})
        recs.append(_rec("rs" + slug[:3], street, status=status, tags=["Courthouse Data"] + list(tags), deceased=True))
    _house(root, "315-oak-st", "315 Oak St", [h("Bea Sample")], [])
    _house(root, "316-pine-st", "316 Pine St", [h("Cid Sample")], [])
    rows += [{"slug": "315-oak-st", "street": "315 Oak St", "verdict": "Work now", "equity_per_heir": 40000},
             {"slug": "316-pine-st", "street": "316 Pine St", "verdict": "Research more", "equity_per_heir": 99000}]
    (root / "stories_results.json").write_text(json.dumps(rows), encoding="utf-8")
    (root.parent / "work").mkdir(parents=True, exist_ok=True)
    (root.parent / "work" / "scored.json").write_text("[]", encoding="utf-8")
    return recs


def _reset(check, tmp) -> None:
    from .autopilot import curative as cv
    from .autopilot import curative_setup as cs
    from .autopilot import gate, ledger, policy
    print("  reset-statuses, then the loader, then the board")
    _clean()
    check(all(cs.status_verdict(s) == "keep" for s in cs.KEEP_STATUSES)
          and all(cs.status_verdict(s) == "keep" for s in ("Close Out", "Under Contract", "BUYER FOUND", " dnc ", "Opt Out"))
          and all(cs.status_verdict(s) == "clear" for s in ("new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead", "Hot Lead",
                                                             "Ghosting Lead", "qualified lead", "follow_up", "not_interested",
                                                             "prospecting", "Deep Prospecting", "Exhausted", "Dead Lead",
                                                             "Return Mail", "Auction Date Passed", "A Status Nobody Planned"))
          and cs.status_verdict(None) == "none" and cs.status_verdict("") == "none",
          "the keep list is a sale, a deal and a do-not-contact, matched without case, spaces or underscores; every other "
          "status clears, the lead statuses included")
    check(all(cs.is_lead_status(s) for s in ("new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead", "Hot Lead",
                                              "Ghosting Lead", "qualified lead", "follow_up"))
          and not any(cs.is_lead_status(s) for s in ("Dead Lead", "not_interested", "Exhausted", "Deep Prospecting")),
          "a lead status is told apart (each cleared one is named by street); a dead lead is not one")

    root = Path(tmp) / "curative_reset" / "pilot"
    recs = _reset_world(root)
    fresh = copy.deepcopy(recs)
    before = {r["uuid"]: (r["status"], list(r["tags"])) for r in recs}
    crm = CurCrm(recs)
    out_dir = Path(config.DATA_DIR) / "curative_reset"
    when = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
    cleared_ids = {"rs" + s[:3] for s in RESET_CLEAR}

    def reset(crm_=None, **kw):
        lines = []
        rep_ = cs.reset_statuses(crm_ or crm, results=root, out=lines.append, folder=kw.pop("folder", out_dir),
                                 gate_cfg=gate.GateConfig(), now=when, **kw)
        return rep_, lines

    def allowed(fn, *a, **kw):
        os.environ["CURATIVE_ALLOW"] = "1"
        try:
            return fn(*a, **kw)
        finally:
            os.environ.pop("CURATIVE_ALLOW", None)

    rep, said = reset()
    check(sorted(r["slug"] for r in rep["clear"]) == RESET_CLEAR and not crm.writes and not crm.sent(),
          "a dry run says which statuses it would clear and writes nothing: %s" % [r["before"] for r in rep["clear"]])
    check(sorted(r["slug"] for r in rep["kept"]) == ["304-dover-st", "305-elm-st", "306-fern-st", "307-grove-st", "313-moss-st"],
          "a sale, a deal and a do-not-contact are kept (sold, dnc, opt_out, Close Out, under_contract)")
    check([r["slug"] for r in rep["held"]] == ["311-kale-st"] and "Do Not Market" in rep["held"][0]["why"],
          "a house another hold keeps off the board (Do Not Market) keeps its status: clearing it would let nothing on")
    check(rep["counts"]["no status"] == 1 and [r["slug"] for r in rep["not_in_datasift"]] == ["315-oak-st"]
          and rep["houses"] == 15 and rep["counts"]["matched: address only"] == 14,
          "each house is found by its address; one with no status is left alone, one not in DataSift is listed, and only "
          "the houses the research says to work now are read")
    check(all(any("would clear a lead status: %s" % st in line for line in said)
              for st in ("302 Baker St (Hot Lead)", "303 Cedar St (new_lead)", "314 Nova St (follow_up)"))
          and not any("lead status: 309 Iris St" in line for line in said),
          "each house whose lead status would be cleared is named by street; a dead lead is not a lead")
    rep, _ = reset(only=("302-baker-st", "304-dover-st"))
    check(rep["houses"] == 2 and [r["slug"] for r in rep["clear"]] == ["302-baker-st"], "--only reads just the houses named")
    rep, _ = reset(commit=True)
    check(not rep["allowed"] and not crm.writes, "--commit alone writes nothing: the env switch is the second key")
    check(cs.main(["reset-statuses", "--commit"]) == 3 and cs.main(["reset-statuses", "--undo", "x.jsonl", "--commit"]) == 3,
          "from the command line, --commit without the env switch is refused before any client is made")

    class Stuck(CurCrm):
        def set_status(self, u, s):
            self.writes.append(("status", u, s))          # the server answered, and the status did not change
    stuck = Stuck(copy.deepcopy(fresh))
    try:
        allowed(reset, stuck, commit=True, folder=out_dir / "stuck")
        check(False, "a first status that does not read back stops the run")
    except SystemExit:
        check(len(stuck.writes) == 1, "a first status that does not read back stops the run before any other record is touched")

    rep, said = allowed(reset, commit=True)
    check(rep["allowed"] and sorted(r["slug"] for r in rep["cleared"]) == RESET_CLEAR
          and sorted(crm.writes) == sorted(("status", u, None) for u in cleared_ids),
          "a real run clears exactly those statuses and writes nothing else: no tag, no list, no flag")
    check(all(crm.recs[u]["status"] is None for u in cleared_ids)
          and all(crm.recs[u]["status"] == before[u][0] for u in before if u not in cleared_ids)
          and all(crm.recs[u]["tags"] == before[u][1] for u in before),
          "the cleared records read no status; the kept ones and the Do Not Market house read as before; every tag is as it was")
    logged = [json.loads(x) for x in Path(rep["log"]).read_text(encoding="utf-8").splitlines() if x.strip()]
    check(len(logged) == 7 and {(x["record"], x["before"]) for x in logged} == {(u, before[u][0]) for u in cleared_ids},
          "each change is logged with the status before it, for --undo")
    check(any("cleared a lead status: 302 Baker St (Hot Lead)" in line for line in said)
          and any(line.startswith("Cleared 7 status(es)") and "not_interested 1" in line and "Kept 5" in line for line in said),
          "the run names each cleared lead status by street and counts the statuses by kind: %s" % said[1:2])
    n0 = len(crm.writes)
    again, _ = allowed(reset, commit=True)
    check(not again["clear"] and len(crm.writes) == n0, "a second run finds nothing more to clear")

    crm.recs["rs302"]["status"] = "Warm Lead"          # a person set a status on one of them since
    und = cs.undo_reset(crm, rep["log"], out=[].append)
    check(not und["allowed"] and len(und["would"]) == 6 and len(crm.writes) == n0,
          "a dry undo says what it would put back and writes nothing")
    und = allowed(cs.undo_reset, crm, rep["log"], commit=True, out=[].append)
    check(all(crm.recs[u]["status"] == before[u][0] for u in cleared_ids - {"rs302"}) and crm.recs["rs302"]["status"] == "Warm Lead"
          and [x["street"] for x in und["left"]] == ["302 Baker St"] and len(und["restored"]) == 6,
          "--undo puts every status back in its stored spelling, and leaves a record a person gave a new status since")

    # Reset again, load, and the board takes the house that read not_interested.
    allowed(reset, commit=True)

    def render(story, pdf):
        Path(pdf).parent.mkdir(parents=True, exist_ok=True)
        Path(pdf).write_bytes(b"%PDF-1.4 curative test")
        return str(pdf)
    lrep = allowed(cs.load, crm, results=root, commit=True, geocode=lambda *a: ("", "no match"), render=render,
                   post=lambda *a: 204, out=[].append, folder=Path(config.DATA_DIR) / "curative", gate_cfg=gate.GateConfig(),
                   now=when, sleep=lambda s: None)
    loaded, held = [r["slug"] for r in lrep["loaded"]], [r["slug"] for r in lrep["held"]]
    check("301-able-st" in loaded and "302-baker-st" in loaded and "311-kale-st" in held and "304-dover-st" in held,
          "after the reset the loader takes the houses that read not_interested and Warm Lead, and still holds the Do Not "
          "Market and the sold ones: loaded %s, held %s" % (loaded, held))
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    s = _sweep("c_board", crm, at(24, 8, 35), pol)
    check((ledger.fact("rs301", cv.FACT) or {}).get("column") == "Day 1" and crm.open_tasks("rs301")
          and (ledger.fact("rs311", cv.FACT) or {}).get("column") is None,
          "and the board puts the cleared house on (Ready for Marketing, then the morning start into Day 1); the Do Not "
          "Market house never reaches it: %s" % s.head)


def _board_tool(check) -> None:
    from .autopilot import curative as cv
    from .autopilot import curative_setup as cs
    print("  the board tool")
    said = []
    crm = CurCrm([], board=False, tagged=False)
    done = cs.board(crm, None, commit=False, out=said.append)
    check(not done["allowed"] and len(done["would"]) == 11 and not crm.writes,
          "a dry board run says it would make the tag, the board and its nine columns, and makes nothing")
    done = cs.board(crm, ColCrm(), commit=True, out=said.append)
    check(not done["allowed"] and not crm.writes, "--commit alone is still a dry run: the env switch is the second key")
    os.environ["CURATIVE_ALLOW"] = "1"
    try:
        try:
            cs.board(crm, None, commit=True, out=said.append)
            check(False, "a real run with no login for the columns is refused before any write")
        except SystemExit:
            check(not crm.writes, "with both switches but no login for the columns, nothing is written")
        cols = ColCrm()
        done = cs.board(crm, cols, commit=True, out=said.append)
    finally:
        os.environ.pop("CURATIVE_ALLOW", None)
    check(("tag_create", "Curative") in crm.writes and ("board_create", cv.BOARD) in crm.writes
          and list(done["column_ids"]) == list(cv.COLUMNS) and cols.titles(done["board"]) == list(cv.COLUMNS)
          and not cols.patched and done["order_problem"] == "",
          "both switches on: the tag, the board and the nine columns in order are made and read back")
    check(any(cv.EXHAUSTED in line and done["column_ids"][cv.EXHAUSTED] in line for line in said),
          "and the run prints the column ids for the stage's block")

    # The board as it was made on 2026-11-23: eight columns, no Ready for Marketing.
    old = ("Research",) + cv.DAY_COLUMNS + ("Heir Engaged", "Exhausted")

    def made_board(**kw):
        c = ColCrm(**kw)
        c.cols[CurCrm.BOARD_ID] = [{"uuid": "old-%d" % i, "title": t, "order": i} for i, t in enumerate(old)]
        return c
    said = []
    cols = made_board(shift=True)
    crm = CurCrm([])
    done = cs.board(crm, cols, commit=False, out=said.append)
    check(done["would"] == ["column Ready for Marketing, right after Research, then the order read back"]
          and len(done["already"]) == 10 and not crm.writes and cols.titles(CurCrm.BOARD_ID) == list(old),
          "on the board made before, a dry run says it would add Ready for Marketing right after Research, and nothing else")

    def real(cols_):
        lines = []
        os.environ["CURATIVE_ALLOW"] = "1"
        try:
            return cs.board(CurCrm([]), cols_, commit=True, out=lines.append), lines
        finally:
            os.environ.pop("CURATIVE_ALLOW", None)
    done, lines = real(cols)
    check(cols.titles(CurCrm.BOARD_ID) == list(cv.COLUMNS) and not cols.patched and done["order_problem"] == ""
          and done["made"] == ["column Ready for Marketing"],
          "a board that makes room at the place asked for: Ready for Marketing lands right after Research, nothing reordered")
    check(all(done["column_ids"][t] == "old-%d" % i for i, t in enumerate(old))
          and done["column_ids"][cv.READY].startswith("col-")
          and any('"Ready for Marketing": "%s"' % done["column_ids"][cv.READY] in line for line in lines),
          "the eight columns keep their ids, and the new column's id is printed for the stage's block")
    cols = made_board(shift=False)
    done, lines = real(cols)
    check(cols.titles(CurCrm.BOARD_ID) == list(cv.COLUMNS) and cols.patched and done["order_problem"] == ""
          and any("putting the columns in order" in line for line in lines),
          "a board that puts the new column on a place already taken is read back out of order, and each column is given "
          "its place through its own route, then read back in order: %s" % cols.patched)
    cols = made_board(shift=False, patch=False)
    done, lines = real(cols)
    check(done["order_problem"] and any("did not take" in line and "drag the columns" in line for line in lines)
          and all(done["column_ids"].get(t) for t in cv.COLUMNS),
          "a board that refuses a new place: the run says so and how to fix it by hand, and still prints all nine ids")
    person = [{"uuid": "a", "title": "Research", "order": 0}, {"uuid": "b", "title": "Notes", "order": 1},
              {"uuid": "c", "title": "Ready for Marketing", "order": 2}, {"uuid": "d", "title": "Day 1", "order": 3}]
    check(cs.order_problem(person) == "" and cs.order_problem(person[:1] + person[2:]) == ""
          and cs.order_problem([dict(person[0]), dict(person[3], order=1), dict(person[2], order=2)])
          and cs.order_problem([dict(person[0]), dict(person[2], order=1), dict(person[3], order=1)]) == "two columns share one place"
          and [c["title"] for c in cs.wanted_order([person[3], person[1], person[0]])] == ["Research", "Day 1", "Notes"],
          "a column a person added does not count as out of order; the board's own columns are put in their order, "
          "the person's after them")


def _source(check) -> None:
    print("  source")
    here = Path(__file__).resolve().parent
    files = [here / "autopilot" / "curative.py", here / "autopilot" / "curative_setup.py", here / "selftest_curative.py"]
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    check(all(chr(0x2014) not in t and chr(0x2013) not in t and "\x08" not in t for t in texts.values()),
          "the curative files carry no long dash and no backspace byte")
    src = texts["curative.py"]
    check(not re.search(r"stage_text|textlane|scpush|sc_load|smarter_contact|send_sms|sms_attempts", src),
          "the board's code has no road to a texting tool")
