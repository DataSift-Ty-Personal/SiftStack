"""The researcher a person used to be: writes research/<slug>.json and analysis/<slug>.json.

dream100_dp renders and writes whatever those two files say and never invents a relative, a death
or a relationship. On the first single-record run and the Dream 100 top 5 a person (or Claude in a session)
wrote them by hand. Here Claude writes them on its own, in two passes:

  research  Claude with web search and web fetch, given the CRM record, the county title and the
            SmartSkip relative graph, returns sourced facts. A second call shapes them into the
            research schema. Every claim must carry a URL Claude actually opened or searched in
            this run; a claim whose URL it never visited is moved to `unresolved`, because an
            invented source is worse than no source.
  analysis  No tools. The judgment: who owns, who died, who must sign, who to call first, what
            to say. Run once before the heir trace (it names who to trace) and again after the
            numbers are scored and verified (call order, whose number is whose).

Guards in code, not in the prompt alone: the owner is marked deceased only when an obituary URL
from this run backs it AND the decedent's name matches the owner of record (the spouse-obituary
trap); an ss_key must exist in people.json; a call-order number must exist on someone.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

import anthropic

from .. import config

if str(config.ROOT / "src") not in sys.path:
    sys.path.insert(0, str(config.ROOT / "src"))

log = logging.getLogger(__name__)

SKILL = Path(config.ROOT) / "skills" / "deep-prospecting-v5" / "SKILL.md"
WEB_TOOLS = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 30},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 30}]
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Funeral homes and Legacy.com sit behind bot protection that web_fetch cannot pass: on the first
# single-record run the obituary that proves the death opened for nobody, so the death stayed unconfirmed.
# Scrapfly's ASP fetch (src/scrapfly_browser.py) clears those pages; the agent calls it itself.
PROTECTED_TOOL = {
    "name": "fetch_protected_page",
    "description": ("Open a page that web_fetch could not open because of Cloudflare or bot protection: "
                    "a funeral home obituary, Legacy.com, a newspaper obituary, a county or court record. "
                    "Costs money per page, so use it only after web_fetch failed on that exact URL."),
    "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
}
PROTECTED_MAX = 8
PROTECTED_USD = 0.02
# Claude Opus 5.5 list prices per token, plus web search per request.
PRICE = {"in": 4e-6, "out": 20e-6, "cache_read": 0.2e-6, "cache_write": 5e-6, "search": 0.01}

# ----------------------------------------------------------------- schemas --
S, B, I = {"type": "string"}, {"type": "boolean"}, {"type": "integer"}


def _obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _arr(item) -> dict:
    return {"type": "array", "items": item}


EVID = _obj(claim=S, url=S, quote=S)
LIVING = {"type": "string", "enum": ["Living", "Deceased", "Unknown"]}

RESEARCH_SCHEMA = _obj(
    slug=S, property=S, owner_of_record=S, owner_status=LIVING, owner_status_confidence=S,
    owner_status_evidence=_arr(EVID),
    decedents=_arr(_obj(name=S, relationship_to_owner=S, dob=S, dod=S, age_at_death=I, obituary_url=S,
                        funeral_home=S, survivors_verbatim=S)),
    people=_arr(_obj(name=S, relationship_to_owner=S, living=B, city_state=S, source_url=S, quote=S)),
    identity_proof=_arr(EVID), title_and_sales=_arr(EVID), other_properties=_arr(EVID),
    tax_and_liens=_arr(EVID), life_stage_signals=_arr(EVID), property_history=_arr(EVID),
    personalization_hooks=_arr(_obj(hook=S, url=S, quote=S)),
    red_flags=_arr(S), unresolved=_arr(S), searches_run=_arr(S))

NUMBER_NOTE = _obj(number=S, note=S)
NUMBER_NAMES = _obj(number=S, names=_arr(S))
ANALYSIS_SCHEMA = _obj(
    slug=S, assignee=S, headline=S, headline_points=_arr(S), owner_status=LIVING, owner_status_note=S,
    decision_maker=S, first_call=S, next_step=S, condition=S, taxes=S, tenure=S, debt=S, touches=S,
    level=S, sources_checked=_arr(S),
    title=_obj(vesting=S, chain=_arr(_arr(S)), flags=_arr(S)),
    identity=_arr(S), evidence=_arr(_obj(finding=S, source=S)), quote_survivors_of=_arr(S),
    family_tree=_arr(S), note_tree=_arr(S),
    people=_arr(_obj(ss_key=S, name=S, relationship=S, verified_by=S, age=I, location=S, living=LIVING,
                     role={"type": "string", "enum": ["owner", "signer", "decision maker", "channel", "none"]},
                     tag=S, rc_name=S, trace=B, trace_first=S, trace_middle=S, trace_last=S,
                     trace_address=_obj(street=S, city=S, state=S, zip=S))),
    must_sign=_arr(_obj(who=S, status=S, why=S, short=S)), signing_flags=_arr(S),
    call_order=_arr(_obj(who=S, number=S, why=S)),
    crm_number_label=S, crm_candidates=_arr(S),
    number_notes=_arr(NUMBER_NOTE), wrong_party=_arr(NUMBER_NOTE),
    verify_extra=_arr(NUMBER_NAMES), also_reaches=_arr(NUMBER_NAMES),
    outreach=_obj(sender=S, who_first=S, angle=S, opener=S, talking_points=_arr(S), texts=_arr(S),
                  letter=S, avoid=_arr(S)),
    corrections=_arr(S), open_items=_arr(S), sources=_arr(_obj(what=S, url=S)),
    death=_obj(decedent=S, dod=S, obituary_url=S, owner_deceased=B, confirmed=B),
    crm_fields={"type": "object", "additionalProperties": False,
                "required": ["Owner Deceased", "Date of Death", "Decedent Name", "Obituary URL",
                             "Decision Maker", "DM Relationship"],
                "properties": {k: S for k in ("Owner Deceased", "Date of Death", "Decedent Name",
                                              "Obituary URL", "Decision Maker", "DM Relationship")}})

RULES = """
You are the Research agent for a real estate investing team. You do the research a skilled deep
prospector does by hand, for ONE property, and you never invent anything.

Hard rules, each one a real failure seen on live records:
1. A death is a fact only with a published obituary, funeral home page or court record you
   opened in this run. SmartSkip's deceased flag is advisory and often wrong in both directions.
2. THE SPOUSE-OBITUARY TRAP: an obituary attached to a record does not mean the OWNER died. Match
   the decedent's name to the owner of record before treating the record as an heir case. A
   widow who still owns the house is a living owner to call gently, not an heir case.
3. Quote survivors verbatim from the obituary. Never infer a child or spouse the source does not
   name. Married daughters carry a different surname; reconcile them against the relative graph.
4. Common names: prove identity with at least two matching facts (age, address history, a
   named relative) before you attach anything to this owner.
5. Every claim needs a URL you actually visited or searched and a short verbatim quote. If you
   cannot source it, put it in `unresolved` instead.
6. Tennessee: estates are probated in county Chancery or Probate Court; title may sit in a
   revocable trust, in which case the successor trustee signs.
7. Never put a person's name or relationship on a phone number that the owner also holds.
8. When web_fetch cannot open an obituary, Legacy.com, newspaper, county or court page, open it
   with fetch_protected_page. A death is confirmed by an obituary you opened with either tool.
""".strip()


def _system() -> list[dict]:
    skill = SKILL.read_text(encoding="utf-8") if SKILL.exists() else ""
    text = RULES + ("\n\nThe team's deep prospecting method (v5):\n\n" + skill if skill else "")
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ------------------------------------------------------------------ inputs --

def _trim(v, n=400):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "..."


def record_brief(run, row) -> dict:
    x = (run.load("records.json") or [{}])[0]
    rec = x.get("record") or {}
    ow = rec.get("owner") or {}
    tax = x.get("tax") or {}
    sm = x.get("siftmap") or {}
    return {
        "property": {"street": row.get("street"), "city": row.get("city"), "zip": row.get("zip"),
                     "beds": row.get("beds"), "baths": row.get("baths"), "sqft": row.get("sqft"),
                     "year_built": row.get("year"), "estimated_value": row.get("value"),
                     "equity_pct": row.get("equity_pct")},
        "owner_of_record_crm": ow.get("company") or " ".join(
            p for p in (ow.get("first_name"), ow.get("last_name")) if p),
        "owner_mailing": {"street": row.get("mail_street"), "city": row.get("mail_city"),
                          "state": row.get("mail_state"), "zip": row.get("mail_zip")},
        "title_owner_county": x.get("title_owner"),
        "county_parcels": [{k: p.get(k) for k in ("owner", "address", "parcel", "amount_due", "year")}
                           for p in (tax.get("parcels") or [])[:5]],
        "owner_other_parcels": len((x.get("portfolio") or {}).get("parcels") or []),
        "siftmap": {k: _trim(sm.get(k), 600) for k in ("obituary_history", "sale_history", "mortgage_history",
                                                       "owner_info", "tax_delinquent", "foreclosure")
                    if sm.get(k)},
        "crm_lists": row.get("lists"),
        "crm_custom_fields": x.get("custom_fields"),
        "crm_activity": x.get("activity_summary"),
        "crm_board_notes": [_trim(m.get("message"), 500) for m in (x.get("messages") or [])[:10]],
        "context": row.get("context"),
    }


def people_brief(run, *, with_phones: bool) -> list[dict]:
    groups = run.load("people.json") or []
    out = []
    for p in (groups[0]["people"] if groups else []):
        item = {k: p.get(k) for k in ("key", "first", "middle", "last", "age", "ss_label", "canon", "street",
                                      "city", "state", "zip", "is_subject", "ss_deceased", "at_owner_address",
                                      "in_state")}
        if with_phones:
            item["phones"] = [{"number": q.get("number"), "type": q.get("type"), "status": q.get("status")}
                              for q in p.get("phones") or []]
        out.append(item)
    return out


# ------------------------------------------------------------------ claude --

class Usage:
    def __init__(self):
        self.usd = 0.0

    def add(self, u) -> float:
        if u is None:
            return 0.0
        g = lambda k: getattr(u, k, 0) or 0  # noqa: E731
        st = getattr(u, "server_tool_use", None)
        searches = (getattr(st, "web_search_requests", 0) or 0) if st else 0
        cost = (g("input_tokens") * PRICE["in"] + g("output_tokens") * PRICE["out"]
                + g("cache_read_input_tokens") * PRICE["cache_read"]
                + g("cache_creation_input_tokens") * PRICE["cache_write"] + searches * PRICE["search"])
        self.usd += cost
        return cost


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=4)


def _stream(client, usage: Usage, **kw):
    """One streamed turn with the server-side refusal fallback on. The installed SDK may not
    know the `fallbacks` keyword yet, so it rides in the body."""
    kw.setdefault("betas", [FALLBACK_BETA])
    kw.setdefault("extra_body", {"fallbacks": "default"})
    with client.beta.messages.stream(**kw) as s:
        msg = s.get_final_message()
    usage.add(getattr(msg, "usage", None))
    if msg.stop_reason == "refusal":
        raise RuntimeError("the model declined this research request")
    return msg


def _urls_seen(blocks) -> set[str]:
    seen = set()
    for b in blocks:
        t = getattr(b, "type", "")
        if t == "web_search_tool_result":
            content = getattr(b, "content", None)
            for r in content if isinstance(content, list) else []:
                if getattr(r, "url", None):
                    seen.add(r.url)
        elif t == "web_fetch_tool_result":
            c = getattr(b, "content", None)
            url = getattr(c, "url", None) if c is not None else None
            if url:
                seen.add(url)
        elif t == "text":
            for c in getattr(b, "citations", None) or []:
                if getattr(c, "url", None):
                    seen.add(c.url)
    return seen


def _norm(u: str) -> str:
    return re.sub(r"[#?].*$", "", (u or "").strip().lower()).rstrip("/").replace("http://", "https://")


def _text(msg) -> str:
    return "\n".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def conform(v, s: dict):
    """Force a value into the schema's shape: fill what is missing with empties, drop what the
    schema does not name, coerce types. Strict constrained decoding rejects schemas this size
    ("compiled grammar is too large"), so the shape is guaranteed here instead, and every
    downstream reader can index a field without guarding it."""
    t = s.get("type")
    if t == "object":
        v = v if isinstance(v, dict) else {}
        return {k: conform(v.get(k), sub) for k, sub in s.get("properties", {}).items()}
    if t == "array":
        return [conform(x, s["items"]) for x in v] if isinstance(v, list) else []
    if t == "boolean":
        return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")
    if t == "integer":
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0
    out = "" if v is None else (v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v))
    enum = s.get("enum")
    if enum and out not in enum:
        low = {e.lower(): e for e in enum}
        out = low.get(out.lower()) or ("Unknown" if "Unknown" in enum else "none" if "none" in enum else enum[0])
    return out


def _first_json(text: str):
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _structured(client, usage: Usage, schema: dict, prompt: str) -> dict:
    ask = (prompt + "\n\nReply with ONE JSON object and nothing else. It must follow this JSON "
           "Schema exactly (every key present; empty string, empty list or false when unknown):\n"
           + json.dumps(schema, separators=(",", ":")))
    messages = [{"role": "user", "content": ask}]
    for attempt in range(2):
        msg = _stream(client, usage, model=config.MODEL, max_tokens=32000, system=_system(),
                      output_config={"effort": "medium"}, messages=messages)
        text = _text(msg)
        try:
            return conform(_first_json(text), schema)
        except ValueError as exc:
            if attempt:
                raise RuntimeError("the model did not return valid JSON twice: %s" % exc) from None
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "That was not one valid JSON object (%s). Reply with only "
                                                     "the JSON object." % exc}]
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- research --

def research(run, row) -> tuple[dict, float]:
    """Web research for one record. Writes research/<slug>.json; returns (research, usd)."""
    client, usage = _client(), Usage()
    slug = run.load("records.json")[0]["slug"]
    brief = {"record": record_brief(run, row), "smartskip_relatives": people_brief(run, with_phones=False)}
    ask = (
        "Research this property's owner of record and, if the owner has died, the heirs and who "
        "must sign. Use web search and web fetch: obituaries (legacy.com, funeral homes, local "
        "papers, findagrave), probate and Chancery filings, county records, and anything that "
        "proves identity. Finish with a plain research memo listing every finding with its URL and "
        "a verbatim quote, the decedents with their verbatim survivors lists, the living people and "
        "their relationship to the owner, red flags, and what you could not resolve.\n\n"
        "RECORD AND RELATIVE GRAPH:\n" + json.dumps(brief, indent=1, default=str))
    messages = [{"role": "user", "content": ask}]
    seen: set[str] = set()
    fetcher = _protected_fetcher()
    tools = WEB_TOOLS + ([PROTECTED_TOOL] if fetcher else [])
    used = 0
    msg = None
    for _ in range(20):
        msg = _stream(client, usage, model=config.MODEL, max_tokens=64000, system=_system(),
                      tools=tools, output_config={"effort": "high"}, messages=messages)
        seen |= _urls_seen(msg.content)
        if msg.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": msg.content})
            continue
        if msg.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": msg.content})
        results = []
        for b in msg.content:
            if getattr(b, "type", "") != "tool_use":
                continue
            url = str((b.input or {}).get("url") or "")
            if b.name != PROTECTED_TOOL["name"] or not fetcher or used >= PROTECTED_MAX:
                results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                "content": "Not available (limit of %d protected pages reached)." % PROTECTED_MAX})
                continue
            used += 1
            usage.usd += PROTECTED_USD
            text, ok = fetcher(url)
            if ok:
                seen.add(url)
            results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": not ok, "content": text})
        messages.append({"role": "user", "content": results})
    memo = _text(msg) if msg else ""
    shaped = _structured(client, usage, RESEARCH_SCHEMA, (
        "Shape this research memo into the research JSON for slug %r. Copy URLs and quotes exactly; "
        "add nothing the memo does not say. `searches_run` lists the searches and pages used.\n\n"
        "URLS VISITED IN THIS RUN:\n%s\n\nMEMO:\n%s" % (slug, "\n".join(sorted(seen)), memo)))
    shaped["slug"] = slug
    shaped = enforce_sources(shaped, seen)
    run.save("research/%s.json" % slug, shaped)
    run.save("research/%s_urls.json" % slug, sorted(seen))
    return shaped, usage.usd


def _protected_fetcher():
    """A url -> (text, ok) function over Scrapfly ASP, or None when SCRAPFLY_KEY is not set."""
    if not os.environ.get("SCRAPFLY_KEY"):
        return None
    try:
        import scrapfly_browser as sb  # noqa: E402
        client = sb.ScrapflyBrowserClient()
    except Exception as exc:  # noqa: BLE001
        log.warning("protected fetch unavailable: %s", exc)
        return None

    def fetch(url: str) -> tuple[str, bool]:
        try:
            res = client.fetch(url)
        except Exception as exc:  # noqa: BLE001
            return "Fetch failed: %s" % str(exc)[:200], False
        if not res.ok or res.blocked:
            return "Could not open (%s)." % (res.blocked_reason or res.error or res.upstream_status), False
        text = res.text or sb.visible_text(res.content)
        return text[:20000] or "The page opened but carried no readable text.", bool(text)
    return fetch


def enforce_sources(r: dict, seen: set[str]) -> dict:
    """Drop every claim whose URL was not visited in this run into `unresolved`."""
    ok = {_norm(u) for u in seen}
    unresolved = list(r.get("unresolved") or [])

    def visited(u: str) -> bool:
        return bool(u) and _norm(u) in ok

    for key in ("owner_status_evidence", "identity_proof", "title_and_sales", "other_properties",
                "tax_and_liens", "life_stage_signals", "property_history"):
        keep = []
        for e in r.get(key) or []:
            if visited(e.get("url")):
                keep.append(e)
            else:
                unresolved.append("dropped, source not visited this run: %s" % e.get("claim"))
        r[key] = keep
    r["personalization_hooks"] = [h for h in r.get("personalization_hooks") or [] if visited(h.get("url"))]
    for d in r.get("decedents") or []:
        if d.get("obituary_url") and not visited(d["obituary_url"]):
            unresolved.append("obituary for %s cited but never opened: %s" % (d.get("name"), d["obituary_url"]))
            d["obituary_url"] = ""
    for p in r.get("people") or []:
        if p.get("source_url") and not visited(p["source_url"]):
            p["source_url"] = ""
    r["unresolved"] = unresolved
    return r


# ---------------------------------------------------------------- analysis --

def analyze(run, row, *, final: bool) -> tuple[dict, float]:
    """The judgment layer. Writes analysis/<slug>.json; returns (analysis, usd)."""
    client, usage = _client(), Usage()
    slug = run.load("records.json")[0]["slug"]
    research_json = run.load("research/%s.json" % slug) or {}
    people = people_brief(run, with_phones=final)
    payload = {"record": record_brief(run, row), "research": research_json, "people_index": people}
    if final:
        payload["phone_scores"] = run.load("scores.json", {})
        payload["real_contact_checks"] = run.load("verify.json", {})
    step = ("FINAL pass: the numbers are traced, scored (Trestle tier) and checked against names "
            "(Real Contact). Fill call_order with real numbers from people_index phones, best first; "
            "number_notes for every number you call out; wrong_party for numbers Real Contact ties "
            "to a stranger; also_reaches when one number reaches two people."
            if final else
            "FIRST pass, before the heir trace: name every living decision maker in `people` with "
            "role 'decision maker' (or 'signer'), living 'Living', trace true, and the ss_key of their "
            "people_index entry. Leave call_order, number_notes, wrong_party, also_reaches and "
            "verify_extra empty for now.")
    shaped = _structured(client, usage, ANALYSIS_SCHEMA, (
        "Write the analysis JSON for slug %r from the research and the people index. %s\n\n"
        "Rules: ss_key must be a `key` from people_index or empty; owner_status and death come only "
        "from the research's sourced evidence; crm_fields values stay empty unless the research "
        "proves them; `assignee` is the CRM assignee if known, else empty. Plain words, no em or en "
        "dashes.\n\nINPUT:\n%s" % (slug, step, json.dumps(payload, indent=1, default=str))))
    shaped["slug"] = slug
    shaped = enforce_analysis(shaped, run, research_json)
    run.save("analysis/%s.json" % slug, shaped)
    return shaped, usage.usd


def _tokens(name: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z ]", " ", (name or "").lower()).split() if len(t) > 1}


def enforce_analysis(a: dict, run, research_json: dict) -> dict:
    groups = run.load("people.json") or []
    keys = {p["key"] for p in (groups[0]["people"] if groups else [])}
    phones = {q.get("number") for p in (groups[0]["people"] if groups else []) for q in p.get("phones") or []}
    for p in a.get("people") or []:
        if p.get("ss_key") not in keys:
            p["ss_key"] = ""
        addr = p.get("trace_address") or {}
        if not (addr.get("street") or "").strip():
            p.pop("trace_address", None)          # an empty street would override the real one
        for k in ("trace_first", "trace_middle", "trace_last", "rc_name", "tag"):
            if not (p.get(k) or "").strip():
                p.pop(k, None)
    a["call_order"] = [c for c in a.get("call_order") or [] if re.sub(r"\D", "", c.get("number") or "")[-10:] in
                       {re.sub(r"\D", "", n or "")[-10:] for n in phones} or not c.get("number")]
    for key in ("number_notes", "wrong_party"):
        a[key] = {re.sub(r"\D", "", e["number"])[-10:]: e["note"] for e in a.get(key) or [] if e.get("number")}
    for key in ("verify_extra", "also_reaches"):
        a[key] = {re.sub(r"\D", "", e["number"])[-10:]: e["names"] for e in a.get(key) or [] if e.get("number")}
    a["crm_fields"] = {k: v for k, v in (a.get("crm_fields") or {}).items() if (v or "").strip()}
    death = a.get("death") or {}
    owner = research_json.get("owner_of_record") or ""
    seen = {_norm(u) for u in run.load("research/%s_urls.json" % a["slug"], []) or []}
    backed = bool(death.get("obituary_url")) and _norm(death["obituary_url"]) in seen
    same_person = len(_tokens(death.get("decedent")) & _tokens(owner)) >= 2
    if death.get("owner_deceased") and not (death.get("confirmed") and backed and same_person):
        death["owner_deceased"] = False
        a.setdefault("open_items", []).append(
            "Owner NOT marked deceased: the death needs an obituary opened this run whose decedent "
            "matches the owner of record (%s)." % owner)
        a["crm_fields"].pop("Owner Deceased", None)
    a["death"] = death
    return a
