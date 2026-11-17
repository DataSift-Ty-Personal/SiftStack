"""The low-cost research engine (src/agents/research/open_research.py). Zero network: every
model call, search and page open is stubbed. Every name here is invented.

    .venv/Scripts/python.exe -m pytest tests/test_open_research.py -q -p no:cacheprovider
"""
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.research import open_research as o  # noqa: E402
from agents.research import researcher  # noqa: E402

ENV = ("AGENTS_RESEARCH_ENGINE", "AGENTS_RESEARCH_MODEL", "AGENTS_RESEARCH_SECOND", "AGENTS_RESEARCH_REFEREE",
       "AGENTS_ANALYSIS_MODEL", "AGENTS_RESEARCH_SEARCHES", "AGENTS_RESEARCH_PAGES")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(o._dead, "serper", False)
    monkeypatch.setitem(o._dead, "firecrawl", False)


class FakeRun:
    def __init__(self, root: Path):
        self.dir = root
        for sub in ("research", "analysis"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    def p(self, *parts):
        return self.dir.joinpath(*parts)

    def load(self, name, default=None):
        f = self.p(name)
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else default

    def save(self, name, obj):
        self.p(name).write_text(json.dumps(obj), encoding="utf-8")


def make_run(tmp_path, owner=("Marla", "Quillfeather")):
    run = FakeRun(tmp_path)
    run.save("records.json", [{"slug": "12-test-ln", "record": {"owner": {"first_name": owner[0],
                                                                         "last_name": owner[1]}}}])
    run.save("people.json", [{"slug": "12-test-ln", "people": [
        {"key": "p1", "first": owner[0], "last": owner[1], "is_subject": True, "phones": [{"number": "8655550101"}]}]}])
    return run


def result(model, status, decedents=(), signers=(), memo_pad="x" * 500, usd=0.01):
    fact = {"owner_status": status, "decedents": [{"name": n, "dod": d} for n, d in decedents],
            "must_sign": list(signers)}
    return {"model": model, "memo": "memo from %s %s\nFACT_SHEET_JSON: %s" % (model, memo_pad, json.dumps(fact)),
            "fact": fact, "error": "", "seconds": 1, "searches": 3, "pages": 2, "usd": usd,
            "prompt_tokens": 10, "completion_tokens": 5, "steps": 2, "hosts": {"HostA": 2}}


# ------------------------------------------------------------------ engine --

def test_the_claude_researcher_stays_the_default():
    e = o.engine()
    assert e.name == "claude"
    assert e.research is researcher.research and e.analyze is researcher.analyze
    assert e.cap == 3.0 and e.label == "research (Claude + web search)"


def test_the_open_engine_is_an_explicit_switch(monkeypatch):
    monkeypatch.setenv("AGENTS_RESEARCH_ENGINE", "open")
    e = o.engine()
    assert e.name == "open" and e.research is o.research and e.analyze is o.analyze
    assert e.cap < 1.0
    s = o.settings()
    assert s["primary"] == o.DEFAULT_PRIMARY and s["second"] == o.DEFAULT_SECOND and s["analysis"] == ""


def test_off_switches_the_second_opinion_and_the_referee_off(monkeypatch):
    monkeypatch.setenv("AGENTS_RESEARCH_SECOND", "off")
    monkeypatch.setenv("AGENTS_RESEARCH_REFEREE", "none")
    s = o.settings()
    assert s["second"] == "" and s["referee"] == ""


# ------------------------------------------------------------------- names --

def test_same_person_survives_order_initials_and_suffixes():
    assert o.same_person("Quillfeather, Marla J", "Marla Quillfeather")
    assert o.same_person("Marla Jean Quillfeather", "Marla Quillfeather Sr.")
    assert not o.same_person("Marla Quillfeather", "Dorn Quillfeather")
    assert not o.same_person("", "Marla Quillfeather")


# ------------------------------------------------------------- cross-check --

def test_living_against_deceased_is_a_hard_dispute():
    d = o.compare({"owner_status": "Living"}, {"owner_status": "Deceased"})
    assert d["hard"] and d["hard"][0].startswith("Owner status")


def test_any_split_on_the_owner_status_is_settled_not_averaged():
    for a, b in (("Unknown", "Deceased"), ("Living", "Unknown")):
        d = o.compare({"owner_status": a}, {"owner_status": b})
        assert d["hard"] and d["hard"][0].startswith("Owner status")


def test_living_against_unconfirmed_is_a_note_only_where_the_index_vouches_for_no_death():
    d = o.compare({"owner_status": "Living"}, {"owner_status": "Unknown"}, absence_vouched=True)
    assert not d["hard"] and d["soft"]
    # the index never excuses a death one researcher actually found
    assert o.compare({"owner_status": "Deceased"}, {"owner_status": "Unknown"}, absence_vouched=True)["hard"]


def test_the_index_vouches_only_for_a_knox_record(tmp_path):
    run = FakeRun(tmp_path)
    run.save("records.json", [{"slug": "s", "record": {"address": {"county": "Blount"}}}])
    assert not o.index_covers(run)
    run.save("records.json", [{"slug": "s", "record": {"address": {"county": "Knox County"}}}])
    assert o.index_covers(run)


def test_a_person_dead_in_one_memo_and_signing_in_the_other_is_hard():
    a = {"owner_status": "Deceased", "decedents": [{"name": "Dorn Quillfeather", "dod": "2021-03-04"}]}
    b = {"owner_status": "Deceased", "must_sign": ["Dorn A. Quillfeather"]}
    assert any("names them as a signer" in x for x in o.compare(a, b)["hard"])


def test_a_dead_father_and_his_living_son_are_two_people():
    # found live: "Sr." dead in one memo and "Jr." signing in the other sent a settled record to the referee
    a = {"owner_status": "Living", "decedents": [{"name": "Dorn Lee Quillfeather Sr.", "dod": "2009-12-09"}]}
    b = {"owner_status": "Living", "must_sign": ["Dorn Lee Quillfeather Jr."]}
    assert o.compare(a, b)["hard"] == []
    assert not o.same_person("Dorn Quillfeather Sr", "Dorn Quillfeather Jr")
    assert o.same_person("Dorn Quillfeather", "Dorn Quillfeather Jr")              # agreement is lenient
    assert not o.same_person("Dorn Quillfeather", "Dorn Quillfeather Jr", strict=True)   # an accusation is not


def test_a_shared_middle_name_and_surname_is_a_parent_and_child_not_one_person():
    # found live: the dead father and his son share a middle name; the referee was paid to settle nothing
    assert not o.same_person("Dorn Maurice Quillfeather", "Pell Maurice Quillfeather")
    a = {"decedents": [{"name": "Dorn Maurice Quillfeather", "dod": "2026-02-19"}]}
    b = {"must_sign": ["Pell Maurice Quillfeather", "Ines Ann Quillfeather"]}
    assert o.compare(a, b)["hard"] == []
    # and a son who carries his father's first name as a middle name is not his father either
    assert o.compare(a, {"must_sign": ["Pell Dorn Quillfeather"]})["hard"] == []
    assert o.same_person("Ines Nicole Quillfeather Brackwater", "Ines Quillfeather")     # married name
    assert o.same_person("Susan Theresa Brackwater", "Theresa Brackwater")              # goes by her middle name
    assert o.same_person("QUILLFEATHER DORN M", "Dorn Quillfeather")                    # county order


def test_a_line_about_the_estate_names_nobody_to_sign():
    # found live: "the executor of the estate of X" read as naming the dead X as a signer
    a = {"decedents": [{"name": "Dorn Quillfeather", "dod": "2024-06-05"}], "must_sign": ["Marla Quillfeather"]}
    b = {"must_sign": ["Marla Quillfeather (surviving spouse, 12 Test Ln)",
                       "OR the Personal Representative/Executor of the Estate of Dorn Quillfeather if probate is opened"]}
    assert o.compare(a, b) == {"hard": [], "soft": []}


def test_a_stranger_listed_only_to_be_ruled_out_is_not_a_dispute():
    # found live on the hardest trap: the owner died in 1999, a same-name stranger in 2025
    a = {"decedents": [{"name": "Dorn W. Quillfeather", "dod": "1999-08-03"},
                       {"name": "Dorn Clifford Quillfeather, Jr. (RULED OUT - not this owner)", "dod": "2025-03-23"}]}
    b = {"decedents": [{"name": "Dorn W Quillfeather", "dod": "August 3, 1999"}]}
    assert o.compare(a, b) == {"hard": [], "soft": []}
    # and when one researcher holds both deaths, the matching one is the one that counts
    a["decedents"][1]["name"] = "Dorn Quillfeather"
    assert o.compare(a, b)["hard"] == []


def test_a_vague_date_and_a_date_about_a_relative_do_not_call_the_referee():
    # found live: "1992 (obituary date ...)" against "before 2007-08-09" for a grandparent
    owners = [{"name": "Pell Quillfeather", "status": "Living"}]
    a = {"owners": owners, "decedents": [{"name": "Ines Quillfeather", "dod": "1992 (obituary Dec 30, 1992)"}]}
    assert o.compare(a, {"owners": owners, "decedents": [{"name": "Ines Quillfeather", "dod": "before 2007-08-09"}]}) \
        == {"hard": [], "soft": []}
    d = o.compare(a, {"owners": owners, "decedents": [{"name": "Ines Quillfeather", "dod": "1994-01-02"}]})
    assert not d["hard"] and d["soft"]                      # a relative: a note
    owners[0]["name"] = "Ines Quillfeather"
    assert o.compare(a, {"owners": owners, "decedents": [{"name": "Ines Quillfeather", "dod": "1994-01-02"}]})["hard"]


def test_a_person_nobody_verified_does_not_read_as_dead():
    shaped = {"decedents": [{"name": "Dorn Quillfeather"}], "red_flags": [], "unresolved": [], "people": [
        {"name": "Marla Quillfeather", "living": False, "relationship_to_owner": "Co-owner of record"},
        {"name": "Dorn Quillfeather", "living": False, "relationship_to_owner": "Owner"},
        {"name": "Pell Quillfeather", "living": True, "relationship_to_owner": "Son"}]}
    out = o.apply_cross_check(shaped, {"hard": [], "soft": []}, disputed=False, refereed=False)
    rel = [p["relationship_to_owner"] for p in out["people"]]
    assert rel[0].startswith("STATUS UNVERIFIED") and rel[1] == "Owner" and rel[2] == "Son"


def test_one_date_written_two_ways_is_one_date():
    a = {"decedents": [{"name": "Marla Quillfeather", "dod": "2025-06-30"}]}
    b = {"decedents": [{"name": "Marla Quillfeather", "dod": "June 30, 2025"}]}
    assert o.compare(a, b) == {"hard": [], "soft": []}
    assert o.date_key("6/30/2025") == o.date_key("30 Jun 2025") == (2025, 6, 30)


def test_different_years_of_death_is_hard_and_a_different_day_is_a_note():
    a = {"decedents": [{"name": "Dorn Quillfeather", "dod": "March 4, 2021"}]}
    assert o.compare(a, {"decedents": [{"name": "Dorn Quillfeather", "dod": "2019-03-04"}]})["hard"]
    d = o.compare(a, {"decedents": [{"name": "Dorn Quillfeather", "dod": "2021-03-09"}]})
    assert not d["hard"] and d["soft"]


def test_signers_one_extra_name_is_a_note_no_overlap_is_hard():
    a = {"must_sign": ["Marla Quillfeather", "Pell Quillfeather"]}
    d = o.compare(a, {"must_sign": ["Marla Quillfeather"]})
    assert not d["hard"] and "Pell Quillfeather" in d["soft"][0]
    assert o.compare(a, {"must_sign": ["Ines Brackwater"]})["hard"]


def test_two_matching_fact_sheets_raise_nothing():
    f = {"owner_status": "Deceased", "decedents": [{"name": "Dorn Quillfeather", "dod": "2021-03-04"}],
         "must_sign": ["Marla Quillfeather"]}
    assert o.compare(f, dict(f)) == {"hard": [], "soft": []}


def test_the_lead_is_the_first_researcher_unless_it_could_not_settle_the_status():
    a, b = result("m/a", "Unknown"), result("m/b", "Deceased")
    assert o.pick_lead(a, b) is b
    assert o.pick_lead(result("m/a", "Living"), b)["model"] == "m/a"
    assert o.pick_lead(a, {"memo": "", "fact": {}}) is a


def test_an_unsettled_owner_status_dispute_ships_as_unknown():
    shaped = {"owner_status": "Living", "red_flags": [], "unresolved": []}
    diff = {"hard": ["Owner status: one researcher says Living, the other says Deceased."], "soft": ["note"]}
    out = o.apply_cross_check(shaped, diff, disputed=True, refereed=False)
    assert out["owner_status"] == "Unknown"
    assert out["red_flags"][0].startswith("DISPUTED")
    assert any(x.startswith("Cross-check note") for x in out["unresolved"])


def test_a_refereed_dispute_keeps_the_answer_and_records_the_dispute():
    shaped = {"owner_status": "Deceased", "red_flags": [], "unresolved": []}
    out = o.apply_cross_check(shaped, {"hard": ["Owner status: x"], "soft": []}, disputed=False, refereed=True)
    assert out["owner_status"] == "Deceased" and not out["red_flags"]
    assert "referee settled it" in out["unresolved"][0]


# ----------------------------------------------------------- signer guard --

def analysis(people, must_sign):
    return {"slug": "12-test-ln", "people": people, "must_sign": must_sign, "signing_flags": [], "open_items": []}


def test_a_dead_person_leaves_the_signer_list():
    a = o.guard_signers(analysis(
        [{"name": "Dorn Quillfeather", "living": "Deceased", "role": "signer"},
         {"name": "Marla Quillfeather", "living": "Living", "role": "decision maker"}],
        [{"who": "Dorn Quillfeather", "status": "Required"}, {"who": "Marla Quillfeather", "status": "Required"}]))
    assert [m["who"] for m in a["must_sign"]] == ["Marla Quillfeather"]
    assert "recorded as deceased" in a["open_items"][0]
    assert a["people"][0]["role"] == "none"


def test_an_unverified_person_stays_but_is_labelled():
    a = o.guard_signers(analysis([{"name": "Pell Quillfeather", "living": "Unknown", "role": "signer"}],
                                 [{"who": "Pell Quillfeather", "status": "Required"}]))
    assert a["must_sign"][0]["status"].startswith("UNVERIFIED")
    assert "not verified as living" in a["signing_flags"][0]


def test_role_lines_and_a_living_namesake_are_left_alone():
    a = o.guard_signers(analysis(
        [{"name": "Dorn Quillfeather", "living": "Deceased", "role": "none"},
         {"name": "Dorn Quillfeather Jr", "living": "Living", "role": "signer"}],
        [{"who": "Successor trustee of the Dorn Quillfeather Trust", "status": "Required"},
         {"who": "Dorn's heirs", "status": "Verify"}, {"who": "A spouse, if any", "status": "Verify"},
         {"who": "Dorn Quillfeather Jr.", "status": "Required"}]))
    assert len(a["must_sign"]) == 4
    assert not any(m["status"].startswith("UNVERIFIED") for m in a["must_sign"])
    assert not a["open_items"]


# ------------------------------------------------------------------- index --

def test_the_obituary_index_rows_are_read_with_age_and_date():
    html = ("<table><tr><td>Quillfeather, Marla Jean</td><td>(age: 84)</td><td>B4</td><td>3/9/2021</td></tr>"
            "<tr><td>Quillfeather, Marla Sue</td><td>(age: 41)</td><td>A2</td><td>7/1/1998</td></tr></table>")
    out = o.parse_index(html, "quillfeather", "Marla")
    assert "Quillfeather, Marla Jean | age 84" in out and "3/9/2021" in out
    assert out.count("\n") == 1


def test_no_row_is_never_reported_as_proof_of_life():
    assert "not proof the person is alive" in o.parse_index("<html></html>", "Quillfeather", "Marla")


def test_companies_and_trusts_are_not_looked_up(tmp_path):
    run = FakeRun(tmp_path)
    run.save("records.json", [{"slug": "s", "record": {"owner": {"company": "Quillfeather Holdings LLC"}}}])
    run.save("people.json", [{"slug": "s", "people": [{"first": "Marla", "last": "Quillfeather", "is_subject": True},
                                                      {"first": "Pell", "last": "Quillfeather"}]}])
    assert o.owner_names(run) == [("Marla", "Quillfeather")]


# --------------------------------------------------------------------- web --

class Resp:
    def __init__(self, status=200, text="", payload=None, ctype="text/html"):
        self.status_code, self.text, self._payload = status, text, payload
        self.headers = {"content-type": ctype}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_a_page_a_plain_request_serves_never_reaches_a_paid_opener(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(o.requests, "get", lambda url, **kw: calls.append(("get", url)) or Resp(text="<p>%s</p>" % ("obituary text " * 40)))
    monkeypatch.setattr(o.requests, "post", lambda url, **kw: calls.append(("post", url)) or Resp(payload={}))
    web = o.Web(tmp_path / "cache.json", env={"FIRECRAWL_API_KEY": "k", "SCRAPFLY_KEY": "k"})
    assert "obituary text" in web.open("https://example.org/obit")
    assert [c[0] for c in calls] == ["get"]
    assert web.stats["page_direct"] == 1 and web.usd == 0
    web.open("https://example.org/obit")
    assert web.stats["page_cached"] == 1 and len(calls) == 1


def test_a_blocked_page_falls_through_to_firecrawl_and_is_costed(monkeypatch, tmp_path):
    monkeypatch.setattr(o.requests, "get", lambda url, **kw: Resp(status=403, text="Just a moment..."))
    monkeypatch.setattr(o.requests, "post", lambda url, **kw: Resp(payload={"data": {"markdown": "survived by " * 60}}))
    web = o.Web(tmp_path / "cache.json", env={"FIRECRAWL_API_KEY": "k"})
    assert "survived by" in web.open("https://funeral.example/obit/1")
    assert web.stats["page_firecrawl"] == 1 and web.usd == o.PAGE_USD["firecrawl"]
    assert "https://funeral.example/obit/1" in web.seen


def test_a_page_nobody_can_open_is_reported_and_not_marked_seen(monkeypatch, tmp_path):
    monkeypatch.setattr(o.requests, "get", lambda url, **kw: Resp(status=403, text="Access denied"))
    web = o.Web(tmp_path / "cache.json", env={})
    assert web.open("https://blocked.example/x").startswith("Could not open")
    assert web.stats["page_failed"] == 1 and not web.seen


def test_a_repeated_search_is_served_from_the_record_cache_across_runs(monkeypatch, tmp_path):
    n = []
    payload = {"organic_results": [{"title": "Obituary", "link": "https://example.org/o", "snippet": "s"}]}
    monkeypatch.setattr(o.requests, "get", lambda url, **kw: n.append(1) or Resp(payload=payload))
    web = o.Web(tmp_path / "cache.json", env={"SERPAPI_KEY": "k"})
    assert "https://example.org/o" in web.search("Marla Quillfeather obituary")
    web.search("marla  quillfeather OBITUARY")
    assert len(n) == 1 and web.stats["search_serpapi"] == 1 and web.stats["search_cached"] == 1
    web.save()
    again = o.Web(tmp_path / "cache.json", env={"SERPAPI_KEY": "k"})
    again.search("Marla Quillfeather obituary")
    assert len(n) == 1 and "https://example.org/o" in again.seen


def test_serper_out_of_credits_switches_to_the_serpapi_plan_once(monkeypatch, tmp_path):
    posts = []
    monkeypatch.setattr(o.requests, "post", lambda url, **kw: posts.append(1) or Resp(status=400, payload={"message": "Not enough credits"}))
    monkeypatch.setattr(o.requests, "get", lambda url, **kw: Resp(payload={"organic_results": [{"title": "t", "link": "https://e.org/1"}]}))
    web = o.Web(tmp_path / "c.json", env={"SERPER_API_KEY": "k", "SERPAPI_KEY": "k"})
    web.search("one")
    web.search("two")
    assert len(posts) == 1 and web.stats["search_serpapi"] == 2


# -------------------------------------------------------------- model loop --

def reply(content="", tool_calls=None, cost=0.002, provider="HostA"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": "c%d" % i, "function": {"name": n, "arguments": json.dumps(a)}}
                             for i, (n, a) in enumerate(tool_calls)]
    return {"choices": [{"message": msg}], "provider": provider,
            "usage": {"cost": cost, "prompt_tokens": 100, "completion_tokens": 20}}


def test_the_loop_enforces_each_budget_and_reads_the_fact_sheet(monkeypatch, tmp_path):
    script = [reply(tool_calls=[("web_search", {"query": "a"}), ("web_search", {"query": "b"}),
                                ("web_fetch", {"url": "https://e.org/1"}), ("obituary_index", {"last_name": "Quillfeather"})]),
              reply("Memo. " + "finding " * 80 + '\nFACT_SHEET_JSON: {"owner_status": "Deceased", "must_sign": ["Pell Quillfeather"]}')]
    monkeypatch.setattr(o, "chat", lambda model, messages, tools=None, max_tokens=20000: script.pop(0))
    web = o.Web(tmp_path / "c.json", env={})
    seen = []
    monkeypatch.setattr(web, "search", lambda q: seen.append(q) or "results")
    monkeypatch.setattr(web, "open", lambda u: "page text")
    monkeypatch.setattr(web, "obituary_index", lambda last, first="": "row")
    r = o.investigate("m/a", "system", "ask", web, searches=1, pages=5)
    assert seen == ["a"] and r["searches"] == 1 and r["pages"] == 1
    assert r["fact"]["owner_status"] == "Deceased" and r["hosts"] == {"HostA": 2}
    assert abs(r["usd"] - 0.004) < 1e-9 and not r["error"]


def test_a_model_that_dies_returns_an_error_not_an_exception(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("openrouter error: gone")
    monkeypatch.setattr(o, "chat", boom)
    r = o.investigate("m/a", "s", "a", o.Web(tmp_path / "c.json", env={}), 2, 2)
    assert "gone" in r["error"] and not o.usable(r)


# ---------------------------------------------------------------- research --

RESEARCH_JSON = {"owner_of_record": "Marla Quillfeather", "owner_status": "Deceased",
                 "owner_status_evidence": [{"claim": "died", "url": "https://seen.example/obit", "quote": "q"},
                                           {"claim": "made up", "url": "https://never.example/x", "quote": "q"}],
                 "red_flags": [], "unresolved": []}


def wire(monkeypatch, results, shaped=RESEARCH_JSON):
    """Stub the model loop and the shaping call; record which models ran."""
    ran = []

    def fake_investigate(model, system, ask, web, searches, pages):
        ran.append(model)
        web.seen.add("https://seen.example/obit")
        return results[model]

    monkeypatch.setattr(o, "investigate", fake_investigate)
    monkeypatch.setattr(o, "lookup_obituary_index", lambda last, first="": "Quillfeather, Marla | age 84 | B4 | obituary date 3/9/2021")
    monkeypatch.setattr(o, "chat", lambda model, messages, tools=None, max_tokens=20000: reply(json.dumps(shaped), cost=0.003))
    return ran


def test_two_researchers_who_agree_ship_the_first_memo_and_no_referee_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_RESEARCH_ENGINE", "open")
    run = make_run(tmp_path)
    a = result(o.DEFAULT_PRIMARY, "Deceased", [("Marla Quillfeather", "2021-03-04")], ["Pell Quillfeather"])
    b = result(o.DEFAULT_SECOND, "Deceased", [("Marla Quillfeather", "2021-03-04")], ["Pell Quillfeather"])
    ran = wire(monkeypatch, {o.DEFAULT_PRIMARY: a, o.DEFAULT_SECOND: b})
    shaped, usd = o.research(run, {"street": "12 Test Ln"})
    assert sorted(ran) == sorted([o.DEFAULT_PRIMARY, o.DEFAULT_SECOND])
    assert shaped["slug"] == "12-test-ln" and shaped["owner_status"] == "Deceased"
    # the source guard still runs: a claim whose page was never opened is moved to unresolved
    assert [e["claim"] for e in shaped["owner_status_evidence"]] == ["died"]
    assert any("made up" in u for u in shaped["unresolved"])
    side = run.load("research/12-test-ln_open.json")
    assert side["lead"] == o.DEFAULT_PRIMARY and side["referee"] is None and not side["disputed"]
    assert abs(usd - (0.01 + 0.01 + 0.003)) < 1e-9
    assert "https://seen.example/obit" in run.load("research/12-test-ln_urls.json")
    assert run.p("research", "12-test-ln_memo_a.md").exists() and run.p("research", "12-test-ln_memo_b.md").exists()


def test_a_hard_dispute_goes_to_the_referee_and_the_referee_memo_ships(monkeypatch, tmp_path):
    run = make_run(tmp_path)
    a, b = result(o.DEFAULT_PRIMARY, "Living"), result(o.DEFAULT_SECOND, "Deceased", [("Marla Quillfeather", "2021")])
    ref = result(o.DEFAULT_REFEREE, "Deceased", [("Marla Quillfeather", "2021")], usd=0.30)
    ran = wire(monkeypatch, {o.DEFAULT_PRIMARY: a, o.DEFAULT_SECOND: b, o.DEFAULT_REFEREE: ref})
    shaped, usd = o.research(run, {})
    assert ran[-1] == o.DEFAULT_REFEREE
    side = run.load("research/12-test-ln_open.json")
    assert side["lead"] == o.DEFAULT_REFEREE and not side["disputed"] and side["cross_check"]["hard"]
    assert any("referee settled it" in u for u in shaped["unresolved"])
    assert usd > 0.30


def test_with_no_referee_a_hard_dispute_ships_as_unknown_and_flagged(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_RESEARCH_REFEREE", "off")
    run = make_run(tmp_path)
    a, b = result(o.DEFAULT_PRIMARY, "Living"), result(o.DEFAULT_SECOND, "Deceased", [("Marla Quillfeather", "2021")])
    ran = wire(monkeypatch, {o.DEFAULT_PRIMARY: a, o.DEFAULT_SECOND: b}, shaped=dict(RESEARCH_JSON, owner_status="Living"))
    shaped, _ = o.research(run, {})
    assert len(ran) == 2
    assert shaped["owner_status"] == "Unknown" and shaped["red_flags"][0].startswith("DISPUTED")
    assert run.load("research/12-test-ln_open.json")["disputed"] is True


def test_a_memo_written_without_looking_is_thrown_out_and_the_researcher_retried(monkeypatch, tmp_path):
    # found live: a host ignored the tools and the model wrote a confident memo, death included, from nothing
    run = make_run(tmp_path)
    blind = dict(result(o.DEFAULT_PRIMARY, "Deceased", [("Marla Quillfeather", "2024")]), searches=0, pages=0)
    good = result(o.DEFAULT_SECOND, "Living", signers=["Marla Quillfeather"])
    assert not o.usable(blind) and o.usable(good)
    ran = wire(monkeypatch, {o.DEFAULT_PRIMARY: blind, o.DEFAULT_SECOND: good}, shaped=dict(RESEARCH_JSON, owner_status="Living"))
    shaped, _ = o.research(run, {})
    assert ran.count(o.DEFAULT_PRIMARY) == 2 and o.DEFAULT_REFEREE not in ran      # retried once, never refereed
    side = run.load("research/12-test-ln_open.json")
    assert side["lead"] == o.DEFAULT_SECOND and not side["cross_check"]["hard"]
    assert any("no cross-check" in u for u in shaped["unresolved"])
    assert shaped["owner_status"] == "Living"                                      # the invented death went nowhere


def test_tool_calls_are_only_routed_to_hosts_that_support_tools(monkeypatch):
    sent = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(o.requests, "post", lambda url, **kw: sent.append(kw["json"]) or Resp(payload={"choices": [{"message": {}}]}))
    o.chat("m/a", [], tools=o.TOOLS)
    o.chat("m/a", [])
    assert sent[0]["provider"] == {"data_collection": "deny", "require_parameters": True}
    assert sent[1]["provider"] == {"data_collection": "deny"}


def test_no_memo_from_anyone_is_a_failure_not_an_empty_success(monkeypatch, tmp_path):
    run = make_run(tmp_path)
    dead = {"model": "x", "memo": "", "fact": {}, "error": "boom", "usd": 0.0}
    wire(monkeypatch, {o.DEFAULT_PRIMARY: dead, o.DEFAULT_SECOND: dead})
    with pytest.raises(RuntimeError, match="no researcher produced a memo"):
        o.research(run, {})


def test_the_owner_is_looked_up_in_the_index_before_any_model_runs(monkeypatch, tmp_path):
    run = make_run(tmp_path)
    asks = []

    def fake_investigate(model, system, ask, web, searches, pages):
        asks.append(ask)
        return result(model, "Deceased")

    wire(monkeypatch, {})
    monkeypatch.setattr(o, "investigate", fake_investigate)
    o.research(run, {})
    assert "obituary_index_for_owner" in asks[0] and "age 84" in asks[0]


# ---------------------------------------------------------------- analysis --

def test_the_analysis_runs_on_the_named_model_with_the_production_prompt_and_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_ANALYSIS_MODEL", "m/cheap")
    run = make_run(tmp_path)
    run.save("research/12-test-ln.json", {"owner_of_record": "Marla Quillfeather"})
    run.save("research/12-test-ln_urls.json", [])
    out = {"people": [{"name": "Dorn Quillfeather", "living": "Deceased", "role": "signer", "ss_key": "nope"},
                      {"name": "Pell Quillfeather", "living": "Unknown", "role": "signer", "ss_key": "p1"}],
           "must_sign": [{"who": "Dorn Quillfeather", "status": "Required"},
                         {"who": "Pell Quillfeather", "status": "Required"}],
           "death": {"decedent": "Someone Else", "owner_deceased": True, "confirmed": True, "obituary_url": ""},
           "crm_fields": {"Owner Deceased": "Yes"}}
    used = []

    def fake_chat(model, messages, tools=None, max_tokens=20000):
        used.append((model, messages[-1]["content"]))
        return reply(json.dumps(out), cost=0.004)

    monkeypatch.setattr(o, "chat", fake_chat)
    before = researcher._structured
    shaped, usd = o.analyze(run, {"street": "12 Test Ln"}, final=False)
    assert researcher._structured is before                      # the Claude path is put back
    assert used[0][0] == "m/cheap" and "FIRST pass" in used[0][1]
    assert abs(usd - 0.004) < 1e-9
    # researcher.enforce_analysis still ran: an unknown key is cleared, an unbacked death is refused
    assert shaped["people"][0]["ss_key"] == "" and shaped["death"]["owner_deceased"] is False
    # and the signer guard ran on top
    assert [m["who"] for m in shaped["must_sign"]] == ["Pell Quillfeather"]
    assert shaped["must_sign"][0]["status"].startswith("UNVERIFIED")
    assert run.load("analysis/12-test-ln.json")["must_sign"][0]["status"].startswith("UNVERIFIED")


def test_the_module_holds_no_long_dash_or_backspace_byte():
    text = (SRC / "agents" / "research" / "open_research.py").read_text(encoding="utf-8")
    assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text
