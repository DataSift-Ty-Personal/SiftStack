"""Flows selftest: the three marketing flows (foreclosure, estates, Top 1,000) built as one change list,
then apply, a second apply and undo against the restructure selftest's fake account. Zero network.
The folder and preset titles are the account's structure, which the tool reads by name; every
record, owner, id and value here is invented.
"""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

DAY = date(2026, 11, 17)


def _layout(st):
    """The account as it stood on 2026-11-17, in miniature: every folder the tool renames or reads."""
    _id, _call, _needs, _mail = st._id, st._call, st._needs, st._mail
    x = ["recently sold", "Mail Only", "Do Not Market"]
    top_x = x + ["FTM"]
    ob, pb = _id("list", "Obituary"), _id("list", "Probate")

    def obit(attempt=None, mail=False):
        m = {"all_lists": [ob], "must_not": dict(st._not(x), any_lists=[_id("list", "DNC"), pb])}
        if mail:
            m.update({"directmail_attempts": [0, 0]})
        elif attempt is not None:
            m.update({"phone": 1, "predictivecall_attempts": [attempt, attempt]})
        return m

    def top(m):
        m = copy.deepcopy(m)
        m["must_not"]["any_lists"] = list(m["must_not"]["any_lists"]) + [ob]
        return m

    calls = [("FTM - 00 Needs Skipped", _needs("FTM", x)), ("FTM - 01 Skipped No Numbers", _needs("FTM", x, traced=1)),
             ("FTM - 02 Ready to Call", _call("FTM", 0, x)), ("FTM - 03 Call Attempt 1", _call("FTM", 1, x)),
             ("FTM - 04 Call Attempt 2", _call("FTM", 2, x)), ("FTM - 05 Call Attempt 3", _call("FTM", 3, x)),
             ("FTM - 06 Call Attempt 4", _call("FTM", 4, x))]
    mails = [("FTM - Mail 01 Handwritten Letter", _mail("FTM", 0, x)), ("FTM - Mail 02 Family Postcard", _mail("FTM", 1, x)),
             ("FTM - Mail 03 Soft-Offer Check", _mail("FTM", 2, x)), ("FTM - Mail 04 Handwritten Letter", _mail("FTM", 3, x)),
             ("FTM - Mail 05 Family Postcard", _mail("FTM", 4, x)), ("FTM - Mail 06 Soft-Offer Check", _mail("FTM", 5, x))]
    return [
        ("01. TOP 1000 - TEXT", [("Priority - 01 Ready to Text", top(dict(_call("Top 1000", 0, top_x), sms_attempts=[0, 0])))]),
        ("02. TOP 1000 - CALL", [("Priority - 02 Ready to Call", top(_call("Top 1000", 0, top_x)))]),
        ("03. TOP 1000 - MAIL", [("Priority - Mail 01 Handwritten Letter", top(_mail("Top 1000", 0, top_x)))]),
        ("04. AGENCY", [("Agency - 01 Queue", {"phone": 1, "must_not": st._not(x + ["FTM", "Top 1000"])})]),
        ("05. FTM - CALL", calls),
        ("06. FTM - MAIL", mails),
        ("11. DEEP PROSPECTING (ALL TIERS)", [
            ("02 Obituary / Deceased", {"all_lists": [ob]}),
            ("Obituary - All", {"any_lists": [ob], "must_not": {"any_lists": [pb]}}),
            ("06 Obituary - Ready to Call", obit(0)), ("07 Obituary - Call Attempt 1", obit(1)),
            ("08 Obituary - Call Attempt 2", obit(2)), ("09 Obituary - Call Attempt 3", obit(3)),
            ("10 Obituary - Call Attempt 4", obit(4)), ("10 Obituary - Ready to Mail", obit(mail=True))]),
        ("13. HOT AUCTION SALE", [
            ("Hot Auction + Buy Box", {"all_tags": [_id("tag", "FTM"), _id("tag", "Auction Soon")],
                                       "all_lists": [_id("list", "Foreclosure")],
                                       "must_not": dict(st._not(x), any_lists=[_id("list", "DNC"), ob])})]),
        ("12. REACTIVATION", [
            ("Not Interested - FTM Foreclosure (30d)", {"any_tags": [_id("tag", "Foreclosure")],
                                                         "any_property_status": ["not_interested"]}),
            ("Not Interested - FTM Probate (30d)", {"any_tags": [_id("tag", "Probate")],
                                                    "any_property_status": ["not_interested"]}),
            ("Not Interested - FTM rest (90d)", {"any_tags": [_id("tag", "FTM")],
                                                 "any_property_status": ["not_interested"]})]),
        ("20. CALLER QUEUES", [("Jordan - Assigned (All)", {"assigned_to": "u-jordan"})]),
    ]


def _records(st):
    rec = st.rec
    return [
        rec(1, "o1", ("FTM",), ("Foreclosure",)),                                        # foreclosure, fresh
        rec(2, "o2", ("FTM",), ("Foreclosure",), texts=1),                               # texted, not called
        rec(3, "o3", ("FTM",), ("Foreclosure",), calls=1, mailed=1, mailed_on="2026-11-03"),  # mailed 14 days ago
        rec(4, "o4", ("FTM",), ("Foreclosure",), mailed=1, mailed_on="2026-11-14"),      # mailed 3 days ago
        rec(5, "o5", ("FTM", "probate"), ("Probate",)),                                  # courthouse probate
        dict(rec(6, "o6", ("FTM", "probate"), ("Probate", "Obituary")), obit="2026-08-01"),  # probate with an obituary
        rec(7, "o7", ("FTM", "probate"), ("Foreclosure", "Probate")),                    # a foreclosure on an estate
        dict(rec(8, "o8", (), ("Obituary",)), obit="2026-09-01"),                        # obituary, 77 days past the death
        dict(rec(9, "o9", ("FTM",), ("Foreclosure", "Obituary")), obit="2026-10-01"),    # a foreclosure with an obituary
        rec(10, "o10", ("Top 1000",)),                                                   # Top 1,000
        rec(11, "o11", (), ("Probate",)),                                                # a probate from bulk data
        rec(12, "o12", ("FTM", "probate"), ("Probate",), mailed=1, mailed_on="2026-11-01"),  # probate mailed 16 days ago
        rec(13, "o13", ("FTM", "Do Not Market"), ("Foreclosure",)),                      # held by the exclusions
        rec(14, "o14", ("FTM",), ("Foreclosure",), status="not_interested"),             # said no, list only
        rec(15, "o15", ("FTM", "probate"), ("Probate",), status="not_interested"),       # said no, list only
        rec(16, "o16", ("FTM", "Foreclosure"), ("Foreclosure",), status="not_interested"),  # said no, carries the tag
        dict(rec(17, "o17", (), ("Obituary",)), obit="2026-11-07"),                      # obituary, 10 days past the death
        rec(18, "o18", ("FTM", "Auction Soon"), ("Foreclosure",), phone=False, mailed=1, mailed_on="2026-11-16"),
        rec(19, "o19", ("FTM", "probate", "Auction Soon"), ("Foreclosure", "Probate"), phone=False, mailed=1,
            mailed_on="2026-11-16"),                                                     # an estate with a sale date
    ]


def _fake_class(st):
    """The restructure fake, plus what the thirty-day tag needs: a search that returns record ids, a
    record's own add-tags and remove-tags, and the obituary date (a record's `obit`)."""
    import copy as _copy
    import re as _re

    class Fake(st.FakeAccount):
        def call(self, path, method="GET", body=None, headers=None):
            base = path.split("?")[0]
            if base == "/api/internal/property/" and method == "POST" and (headers or {}).get("x-http-method-override") == "GET":
                must = _copy.deepcopy(body["query"]["must"])
                n = self.count(_copy.deepcopy(must), counted=False)
                hits = [r for r in self.records if self._match(r, must)]
                off, lim = int(body.get("offset") or 0), int(body.get("limit") or 100)
                self.calls += 1
                self.log.append((method, path))
                return {"count": n, "results": [{"uuid": "r%d" % r["n"]} for r in hits[off:off + lim]]}
            m = _re.match(r"^/api/internal/property/r(\d+)/(add|remove)-tags/$", base)
            if m and method == "POST":
                r = next(x for x in self.records if x["n"] == int(m.group(1)))
                t = body["tags"]
                if m.group(2) == "add" and t not in r["tags"]:
                    r["tags"].append(t)
                if m.group(2) == "remove" and t in r["tags"]:
                    r["tags"].remove(t)
                self.calls += 1
                self.log.append((method, path))
                return {}
            m2 = _re.match(r"^/api/internal/filter-preset/([^/]+)/$", base)
            if m2 and method == "PATCH" and "title" in (body or {}):
                # The app renames a preset with a PATCH that carries the new title (updateFilterPreset in
                # its bundle); a title is unique in an account.
                pre = self.presets.get(m2.group(1))
                if pre is None:
                    raise self._no(method, path)
                if any(q["title"] == body["title"] and q is not pre for q in self.presets.values()):
                    raise self._no(method, path, 400, '{"non_field_errors": ["The fields title must make a unique set."]}')
                super().call(path, method, {k: v for k, v in body.items() if k != "title"}, headers)
                pre["title"] = body["title"]
                return _copy.deepcopy(pre)
            return super().call(path, method, body, headers)

        def _has(self, r, key, v):
            if key == "last_obituary_date":
                x = r.get("obit") or ""
                return bool(x) and (v[0] is None or x >= v[0]) and (v[1] is None or x <= v[1])
            return super()._has(r, key, v)

    return Fake


def run(check, tmp: Path) -> None:
    from . import selftest_restructure as st
    from .autopilot import flows as fl
    from .autopilot import preset_patch as pp
    from .autopilot import restructure as rs
    tmp.mkdir(parents=True, exist_ok=True)
    _id = st._id

    def fake():
        f = _fake_class(st)(folders=_layout(st), recs=_records(st))
        for t in ("Top 1000", "Foreclosure", "Probate", "probate", "Auction Soon"):
            f.tags[_id("tag", t)] = t
        for t in ("Foreclosure", "Probate"):
            f.lists[_id("list", t)] = t
        return f

    def build(account):
        return fl.build_plan(pp.Reader(account, now=DAY, max_requests=100000), day="2026-11-17")

    def apply(account, plan, name, commit=True):
        with st.environ({fl.ALLOW_ENV: "1"}):
            return fl.apply_plan(account, plan, commit=commit, log_path=tmp / ("%s.jsonl" % name),
                                 backup_dir=tmp / ("backups_%s" % name), out=st._quiet, today=DAY)

    def holds(account, title):
        p = account.preset_by_title(title)
        if p is None:
            return None
        must = rs._must(p)
        shaped = pp.Reader(account, now=DAY, max_requests=100000)
        return shaped.count(must)

    # ------------------------------------------------------------------ the pieces
    print("  pieces")
    F, P, O = "L-F", "L-P", "L-O"
    m = fl.foreclosure_must({"any_tags": ["T"], "any_lists": ["X", "Y"], "must_not": {"any_lists": ["D"]}}, F, P, O)
    check(m["all_lists"] == [F] and m["any_lists"] == ["X", "Y"] and m["must_not"]["any_lists"] == ["D", P, O],
          "a foreclosure filter requires the list with all_lists, so a filter's own either-or keeps its meaning")
    check(fl.text_ready({"predictivecall_attempts": [0, 0], "a": 1}) == {"a": 1, "sms_attempts": [0, 0]}
          and fl.text_sent({"predictivecall_attempts": [0, 0]}) == {"predictivecall_attempts": [0, 0], "sms_attempts": [1, None]},
          "Ready to Text swaps the call counter for the text counter; Texted Not Yet Called keeps both")
    check(fl.mail_step({"last_direct_mailed": ["36-months", "month"], "directmail_attempts": [0, 0]}, [0, 0], None)
          == {"directmail_attempts": [0, 0]}
          and fl.mail_step({"directmail_attempts": [0, 0]}, [2, 2], fl.WEEK_GATE)["last_direct_mailed"] == ["36-months", "week"],
          "a first piece is never gated; a later piece waits for its gap")

    # ------------------------------------------------------------------ the plan
    print("  the plan")
    acct = fake()
    plan = build(acct)
    s = plan["summary"]
    check(not plan["blocking"], "the plan reads the account without a blocking item: %s" % (plan["blocking"] or "ok"))
    check(s["folders_to_rename"] == 7 and s["folders_to_create"] == 6,
          "seven folders renamed (the hot auction folder too) and six made: %s and %s"
          % (s["folders_to_rename"], s["folders_to_create"]))
    hp = plan["hot"]
    check(hp["action"] == "convert" and hp["convert_from"] == fl.HOT_FROM and hp["count"] == 1
          and _id("list", "Probate") in hp["must"]["must_not"]["any_lists"],
          "the hot auction list is folded into the foreclosure calls and drops the estate on it: %s record(s)"
          % hp.get("count"))
    check(s["presets_to_narrow"] == 13, "the seven call and six mail presets are narrowed: %s" % s["presets_to_narrow"])
    check(s["presets_to_create"] == 24 and s["presets_to_convert"] == 0,
          "24 presets made (2 text, 6 weekly, 7 estate call, 2 probate text, 7 probate mail): %s" % s["presets_to_create"])
    rd = plan["ready"]
    check(rd["action"] == "create" and rd["due"] == 2 and rd["waiting"] == 1,
          "two obituary records are 30+ days past the death and due the tag, one is inside the 30 days: %s and %s"
          % (rd["due"], rd["waiting"]))
    check(s["presets_to_move"] == 6, "six obituary presets move (Obituary - All is already in Deep Prospecting): %s" % s["presets_to_move"])
    ra = {r["title"]: r for r in plan["reanchor"]}
    check(s["presets_to_reanchor"] == 3 and ra["Not Interested - FTM Foreclosure (30d)"]["count_before"] == 1
          and ra["Not Interested - FTM Foreclosure (30d)"]["count_after"] == 2,
          "the 30-day foreclosure second chance reads the list, not the tag few records carry: %s -> %s"
          % (ra["Not Interested - FTM Foreclosure (30d)"]["count_before"], ra["Not Interested - FTM Foreclosure (30d)"]["count_after"]))
    check(ra["Not Interested - FTM Probate (30d)"]["count_after"] == 1 and ra["Not Interested - FTM rest (90d)"]["count_before"] == 3
          and ra["Not Interested - FTM rest (90d)"]["count_after"] == 0,
          "the probate one finds its no, and the 90-day one no longer counts a foreclosure or probate twice")
    check(plan["split"] == {"ftm": 15, "foreclosure": 8, "probate": 6, "obituary_only": 1, "neither": 0},
          "the FTM tag splits by list, an estate list winning: %s" % plan["split"])
    rows = {r["title"]: r for r in plan["narrow"]}
    check(rows["FTM - 02 Ready to Call"]["count_before"] == 8 and rows["FTM - 02 Ready to Call"]["count_after"] == 3,
          "the first-to-market Ready to Call keeps only foreclosures off the estate lists: %s -> %s"
          % (rows["FTM - 02 Ready to Call"]["count_before"], rows["FTM - 02 Ready to Call"]["count_after"]))
    made = {p["title"]: p for p in plan["presets"]}
    check(made["FTM - 01 Ready to Text"]["count"] == 3, "Ready to Text holds the untexted foreclosures: %s"
          % made["FTM - 01 Ready to Text"]["count"])
    check(made["FTM - 02 Texted, Not Yet Called"]["count"] == 1, "the texted foreclosure waits for its first call")
    check(made["FTM - Weekly 01 Handwritten Letter"]["count"] == 2 and made["FTM - Weekly 02 Options Letter"]["count"] == 1,
          "the second weekly piece waits a week: one mailed 14 days ago is due, one mailed 3 days ago is not")
    check(made["Estate - 02 Ready to Call"]["count"] == 4 and "Probate - 02 Ready to Call" not in made,
          "before the tag exists the estate Ready to Call counts the courthouse probates only: %s"
          % made["Estate - 02 Ready to Call"]["count"])
    em = made["Estate - 02 Ready to Call"]["must"]
    check(em["any_tags"][0] == _id("tag", "probate") and em["any_tags"][1] == {"$ref": "tag", "title": fl.READY_TAG}
          and "all_lists" not in em,
          "the estate presets read the probate notice tag or the thirty-day tag, a marker until apply makes it")
    check(made["Probate - Mail 02 Handwritten Note"]["count"] == 1, "the probate mailed 16 days ago is due its second piece")
    check(made["Estate - 02 Ready to Call"]["folder"] == fl.ES_CALL and made["FTM - Weekly 03 Check Letter"]["folder"] == fl.FC_MAIL,
          "each new preset lands in its flow's folder")
    check(all(made[t]["sources"] for t in made), "every new preset names the preset it was built from")

    # ------------------------------------------------------------------ apply
    print("  apply")
    before = {p["title"]: (p["folder"], copy.deepcopy(p["filters"])) for p in acct.presets.values()}
    folders_before = sorted(acct.folder_titles())
    dry = fl.apply_plan(None, plan, commit=False, out=st._quiet)
    check(not dry["allowed"] and len(dry["would"]) == 7 + 6 + 1 + 13 + 24 + 6 + 3 + 1 + 2,
          "a dry apply lists every step and sends nothing: %d" % len(dry["would"]))
    plan_file = tmp / "plan.json"
    plan_file.write_text(__import__("json").dumps(plan), encoding="utf-8")
    with st.environ({fl.ALLOW_ENV: None}):
        half = fl.apply_plan(acct, plan, commit=True, out=st._quiet)
        code = fl.main(["apply", "--plan", str(plan_file), "--commit"])
    check(not half["allowed"] and code == 3,
          "--commit without %s=1 stays dry, and the command line refuses it (exit %s)" % (fl.ALLOW_ENV, code))
    check(not acct.writes(), "nothing was written before both switches")
    done = apply(acct, plan, "first")
    titles = acct.folder_titles()
    check(all(t in titles for t in ["01. FORECLOSURE - TEXT", "02. FORECLOSURE - CALL", "03. FORECLOSURE - MAIL (WEEKLY)",
                                    "04. ESTATES - TEXT", "05. ESTATES - CALL", "06. ESTATES - MAIL", "07. TOP 1000 - TEXT",
                                    "08. TOP 1000 - CALL", "09. TOP 1000 - MAIL", "10. AGENCY",
                                    "99. Archive - FTM - MAIL (monthly)"]),
          "the folders read as three flows, the Top 1,000 and the agency below them")
    check("05. FTM - CALL" not in titles and "01. TOP 1000 - TEXT" not in titles, "no folder keeps its old title")
    ftm_ready = acct.preset_by_title("FTM - 02 Ready to Call")
    check(ftm_ready and ftm_ready["folder"] == _id("folder", "05. FTM - CALL") and holds(acct, "FTM - 02 Ready to Call") == 3,
          "the first-to-market call preset keeps its title and folder, now holding the three foreclosures")
    ob_ready = acct.preset_by_title("06 Obituary - Ready to Call")
    folder_of = {f["title"]: f["uuid"] for f in acct.folders}
    check(ob_ready["folder"] == folder_of[fl.OBIT_ARCHIVE] and ob_ready["filters"] == before["06 Obituary - Ready to Call"][1]
          and acct.preset_by_title("Obituary - All")["folder"] == folder_of[fl.DP_FOLDER],
          "the old obituary call presets go to the archive as they were, and Obituary - All to Deep Prospecting")
    check(len(done["made"]) == 7 + 6 + 1 + 13 + 24 + 6 + 3 + 1 + 2 and done["backup"] and done.get("tagged") == 2,
          "every step was made, a backup written first, and two records tagged: %s" % done.get("tagged"))
    hot_p = acct.preset_by_title(fl.HOT_TO)
    check(hot_p is not None and hot_p["uuid"] == _id("preset", fl.HOT_FROM) and hot_p["folder"] == folder_of[fl.FC_CALL]
          and acct.preset_by_title(fl.HOT_FROM) is None and holds(acct, fl.HOT_TO) == 1
          and "99. Archive - 13. HOT AUCTION SALE" in folder_of and "13. HOT AUCTION SALE" not in folder_of,
          "the hot auction list now sits in the foreclosure calls under its new name, same id, and its old folder is "
          "archived")
    recs = {r["n"]: r for r in acct.records}
    check(fl.READY_TAG in recs[8]["tags"] and fl.READY_TAG in recs[9]["tags"] and fl.READY_TAG not in recs[17]["tags"]
          and fl.READY_TAG not in recs[6]["tags"],
          "an obituary record 30+ days past the death gets the tag; one 10 days past it and a probate do not")
    check(holds(acct, "Estate - 02 Ready to Call") == 6,
          "the estate Ready to Call now holds the four probates and the two obituary records past 30 days: %s"
          % holds(acct, "Estate - 02 Ready to Call"))
    again = apply(acct, build(acct), "second")
    check(not again["made"], "a second plan and apply change nothing: %s" % (again["made"][:3] or "ok"))

    # ------------------------------------------------------------------ preflight
    print("  preflight")
    other = fake()
    p2 = build(other)
    other.preset_by_title("FTM - 03 Call Attempt 1")["filters"]["must"]["phone"] = 0
    check(st._raises(lambda: apply(other, p2, "changed"), SystemExit) and not other.writes(),
          "a preset edited after the plan stops the run before its first write")

    # ------------------------------------------------------------------ undo
    print("  undo")
    with st.environ({fl.ALLOW_ENV: "1"}):
        u = fl.undo(acct, tmp / "first.jsonl", commit=True, out=st._quiet)
    after = {p["title"]: (p["folder"], p["filters"]) for p in acct.presets.values()}
    check(not u["stopped"] and not u["left"], "undo puts every logged write back: %s" % (u["left"][:2] or u["stopped"] or "ok"))
    check(after == before, "every preset is back in its folder with its old filter, and the made ones are gone")
    check(sorted(acct.folder_titles()) == folders_before, "every folder has its old title and the made ones are gone")
    check(not any(fl.READY_TAG in r["tags"] for r in acct.records) and fl.READY_TAG not in acct.tags.values(),
          "the thirty-day tag comes off every record it went on, and the tag itself goes")

    # ------------------------------------------------------------------ the account as the first run left it
    print("  conversion")
    conv = fake()
    apply(conv, build(conv), "conv1")
    original = {t: m for _f, items in _layout(st) for t, m in items}
    ids = {}
    for t in fl.CALL_PRESETS:
        step = t[len("FTM - "):]
        e = conv.preset_by_title(fl.ESTATE_PREFIX + step)
        e["title"] = fl.PROBATE_PREFIX + step
        e["filters"]["must"] = fl.probate_must(original[t], _id("list", "Probate"))
        ids[step] = e["uuid"]
    for r in conv.records:
        if fl.READY_TAG in r["tags"]:
            r["tags"].remove(fl.READY_TAG)
    conv.tags = {u: t for u, t in conv.tags.items() if t != fl.READY_TAG}
    p3 = build(conv)
    s3 = p3["summary"]
    check(not p3["blocking"] and s3["presets_to_convert"] == 7 and s3["presets_to_create"] == 0,
          "on the account as the first run left it, the seven probate call presets are converted, none made: %s"
          % str(p3["blocking"][:1] or (s3["presets_to_convert"], s3["presets_to_create"])))
    apply(conv, p3, "conv2")
    after_ids = {p["title"][len(fl.ESTATE_PREFIX):]: p["uuid"] for p in conv.presets.values()
                 if p["title"].startswith(fl.ESTATE_PREFIX)}
    check(after_ids == ids and not any(conv.preset_by_title(fl.PROBATE_PREFIX + t[len("FTM - "):]) for t in fl.CALL_PRESETS),
          "each converted preset keeps its id, and no probate call preset is left")
    with st.environ({fl.ALLOW_ENV: "1"}):
        u2 = fl.undo(conv, tmp / "conv2.jsonl", commit=True, out=st._quiet)
    back = conv.preset_by_title(fl.PROBATE_PREFIX + "02 Ready to Call")
    check(not u2["stopped"] and back is not None and back["uuid"] == ids["02 Ready to Call"],
          "undo turns a converted preset back into the probate preset it was, same id")

    # ------------------------------------------------------------------ the daily upkeep
    print("  the daily upkeep")
    daily = fake()
    apply(daily, build(daily), "daily1")

    class Stub:
        def __init__(self, f):
            self.f = f

        def call(self, *a, **k):
            return self.f.call(*a, **k)

        def count(self, must):
            return self.f.count(must)

        def list_uuid(self, t):
            return next((u for u, x in self.f.lists.items() if x.lower() == t.lower()), "")

        def tag_uuid(self, t):
            return next((u for u, x in self.f.tags.items() if x == t), "")

    later = date(2026, 12, 10)                           # the recent death is now 33 days old
    with st.environ({fl.ALLOW_ENV: None}):
        dry_day = fl.ready(Stub(daily), commit=True, today=later, out=st._quiet)
    with st.environ({fl.ALLOW_ENV: "1"}):
        live_day = fl.ready(Stub(daily), commit=True, today=later, log_path=tmp / "ready.jsonl", out=st._quiet)
        again_day = fl.ready(Stub(daily), commit=True, today=later, log_path=tmp / "ready.jsonl", out=st._quiet)
    recs = {r["n"]: r for r in daily.records}
    check(not dry_day["allowed"] and dry_day["due"] == 1 and live_day["tagged"] == 1 and fl.READY_TAG in recs[17]["tags"]
          and again_day["tagged"] == 0,
          "the daily run tags a record the day it crosses 30 days past the death, once, and is dry without the switch")
