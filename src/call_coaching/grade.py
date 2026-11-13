"""grade.py: grade one transcribed call against the team's rubric, with Claude.

The three coach skills (skills/cold-call-coach, lead-manager-coach, closer-coach) were run inside a
person's Claude session: someone opened a transcript and graded it by the rubric. The brain has no
person in that seat, so this module is the seat. The rubric file goes in as the system prompt
(cached: it is the same for every call of a pipeline), the transcript goes in as the message, and
what comes back is checked before it counts:

  * NO SCORES JSON, NO GRADE. A report the code cannot read is never recorded as a score.
  * THE TOTAL IS RECOMPUTED from the category scores and the rubric's own weights, spreading an N/A
    category's weight over the rest as the rubric says. The skill's measured reliability is about
    3 points; a sum that does not foot is arithmetic, not judgment, so it is replaced and said.
  * EVERY QUOTE MUST BE IN THE TRANSCRIPT (the skill's verify pass). A quote that is not is counted
    and flagged for the owner, who reads the notes first, never silently kept as evidence.
  * THE RECORDING LINK IS NEVER ASKED FOR AND NEVER KEPT. It opens without a login.

The owner reads the notes before any caller does (build plan 6.7), so the model also returns one
short note per call: the one thing to fix on a worst call, the one thing to keep on a best one.

Used by the brain (src/agents/workflows/call_coaching.py). It runs on a workstation as well, with
ANTHROPIC_API_KEY set.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINES = {"cold_call": "cold-call-coach", "lead_management": "lead-manager-coach", "closing": "closer-coach"}
PIPELINE_WORDS = {"cold_call": "cold call", "lead_management": "lead management call", "closing": "closing call"}
# Server-side refusal fallback, the same beta the research agent runs on (src/agents/research).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Dollars per million tokens: input, output, cache read, cache write (five-minute). Read 2026-11-13.
# A model not listed is priced at the dearest rate, so a cap can only be met early, never overrun.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0), "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125), "claude-fable-5-2": (8.0, 40.0, 0.20, 10.0)}
DEAREST = (10.0, 50.0, 1.0, 12.5)
BANDS = ((90.0, "Elite"), (75.0, "Strong"), (60.0, "Developing"), (40.0, "Needs Work"), (0.0, "Retrain"))
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EM, EN = chr(0x2014), chr(0x2013)       # the two dashes the house rules ban from anything a person reads

PROTOCOL = """You grade one recorded sales call for the owner of a real estate investing team. The rubric
below is the only standard. The owner reads your notes before any caller sees them.

How to grade. Every rule is mandatory.
1. Read the whole transcript first. The AGENT and SELLER labels come from a transcription pass and
   can be swapped: our caller is whoever asks about buying the property, whatever the label says.
   If they are swapped, grade with the corrected roles and say so in one line.
2. Run the rubric's applicability gate. A call the gate does not score (a voicemail, a wrong
   number, no decision maker reached, dead air) gets no grade: write one line saying why and, in
   the SCORES JSON, set "call_type" to "not_scored" with every number null.
3. Route full or short exactly as the rubric says. When in doubt, grade it as a full call.
4. Every criterion score cites a verbatim quote from the transcript or names an explicit absence.
   Every quote you attribute to the call must appear word for word in the transcript.
5. Use N/A only where the rubric allows it, and list every N/A with the weight it moved.
6. Recompute the math. The tables must foot exactly, and the SCORES JSON must match them number
   for number.
7. Grade only what is audible on the call. Records, dispositions and anything after the call are
   out of scope.
8. Never write a recording link; set "recording_url" to "". Never use the long dash or the en
   dash: use a comma, a colon or parentheses.

What to write, in this order:
- the rubric's report template (full or short), filled in;
- the SCORES JSON block the rubric requires, fenced as ```json;
- one more fenced ```json block, the note for the owner, exactly these keys:
  {"focus": "fix" or "reinforce", "criterion": "<criterion number, for example 2.1>",
   "said": "<our caller's words, verbatim from the transcript>",
   "instead": "<the playbook line to use instead; empty when reinforcing>",
   "why": "<one plain sentence>"}
  On a call picked as one of the caller's worst, focus is fix: the one change that would have
  helped most. On a call picked as one of the best, focus is reinforce: the one thing to keep."""


# ------------------------------------------------------------------ the rubric
def skills_dir() -> Path:
    return Path(os.environ.get("AGENTS_SKILLS_DIR") or ROOT / "skills")


def rubric_path(pipeline: str, base: Path | None = None) -> Path:
    skill = PIPELINES.get(pipeline)
    if not skill:
        raise KeyError("no rubric for pipeline %r (rubrics: %s)" % (pipeline, ", ".join(PIPELINES)))
    return Path(base or skills_dir()) / skill / "references" / "rubric.md"


def rubric(pipeline: str, base: Path | None = None) -> str:
    return rubric_path(pipeline, base).read_text(encoding="utf-8")


_CAT = re.compile(r"^#{2,4}\s*Category\s+(\d+)\s*:\s*(.+?)\s*\((\d+(?:\.\d+)?)%\)\s*$", re.M)
_CRIT = re.compile(r"^\|\s*(\d+\.\d+)\s+([^|]+?)\s*\|", re.M)


def weights(text: str) -> dict[str, float]:
    """category number -> its weight in percent, read from the rubric's own headings."""
    return {m.group(1): float(m.group(3)) for m in _CAT.finditer(text or "")}


def category_names(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in _CAT.finditer(text or "")}


def criterion_names(text: str) -> dict[str, str]:
    """criterion number -> its short name, from the rubric's tables ("2.1" -> "Motivation asked soft...")."""
    out: dict[str, str] = {}
    for m in _CRIT.finditer(text or ""):
        out.setdefault(m.group(1), m.group(2).strip())
    return out


def band(total) -> str:
    if total is None:
        return ""
    for floor, name in BANDS:
        if float(total) >= floor:
            return name
    return "Retrain"


def _number(v):
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def recompute(categories: dict, w: dict) -> float | None:
    """The call score from its category scores: sum of (score / 5 x weight), with the weight of an
    N/A category (null) spread over the scored ones, scaled to 100. None when nothing was scored."""
    usable = {}
    for k, v in (categories or {}).items():
        n = _number(v)
        if n is not None and str(k) in w:
            usable[str(k)] = n
    weight = sum(w[k] for k in usable)
    if not usable or weight <= 0:
        return None
    return round(sum(usable[k] / 5.0 * w[k] for k in usable) * 100.0 / weight, 1)


# ------------------------------------------------------------------ reading what came back
def clean(text: str) -> str:
    """No long dash and no en dash in anything a person reads: a range keeps a plain hyphen."""
    t = (text or "").replace(" %s " % EM, ", ").replace(EM, ", ").replace(EN, "-")
    return t


_REC_FIELD = re.compile(r'("recording_url"\s*:\s*)"[^"]*"')


def scrub_links(text: str) -> str:
    """The rubric's JSON carries a recording_url field. The grader is never given the link, but a
    model can still fill the field, and a recording opens without a login: it is blanked here."""
    return _REC_FIELD.sub(r'\1""', text or "")


_FENCE = re.compile(r"```(?:json)?[ \t]*\n?(.*?)```", re.S)


def json_blocks(text: str) -> list[dict]:
    """Every JSON object in the reply, fenced ones first. A fence that does not parse is skipped."""
    out = []
    for m in _FENCE.finditer(text or ""):
        body = m.group(1).strip()
        if not body.startswith("{"):
            continue
        try:
            d = json.loads(body)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    if out:
        return out
    dec = json.JSONDecoder()
    i = (text or "").find("{")
    while i != -1:
        try:
            d, end = dec.raw_decode(text, i)
            if isinstance(d, dict):
                out.append(d)
            i = text.find("{", end)
        except ValueError:
            i = text.find("{", i + 1)
    return out


def _norm(s: str) -> str:
    """Words only, lower case: speaker labels, delivery notes like [long pause 4s] and punctuation
    are not part of what was said."""
    s = (s or "").lower().replace(chr(0x2019), "'").replace(chr(0x2018), "'")
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"(?:^|\s)(?:agent_vm|agent|seller|voicemail)\s*:", " ", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return " ".join(s.split())


_QUOTE = re.compile(r'"([^"\n]{12,})"')
_INSTEAD = re.compile(r"(use instead|say instead|instead\W|playbook line)", re.I)


def _said(q: str, hay: str) -> bool:
    """Is this quote in the call? An ellipsis joins pieces of the call, and each piece must be there."""
    pieces = [p for p in re.split(r"\.\.\.|" + chr(0x2026), q or "") if len(_norm(p).split()) >= 3]
    return not pieces or all(_norm(p) in hay for p in pieces)


def unverified_quotes(report: str, transcript: str, note: dict | None = None) -> list[str]:
    """Quotes the report attributes to the call that are not in the transcript. A prescribed
    playbook line ("Use instead: ...") is not a quote from the call and is not checked, and the JSON
    blocks are machine fields (only the note's `said` is a quote, and it is checked on its own)."""
    hay = _norm(transcript)
    missing = []
    said = str((note or {}).get("said") or "")
    if said and not _said(said, hay):
        missing.append(said[:90])
    for line in _FENCE.sub(" ", report or "").splitlines():
        line = line.replace(chr(0x201C), '"').replace(chr(0x201D), '"')
        m = _INSTEAD.search(line)
        part = line[:m.start()] if m else line
        for q in _QUOTE.findall(part):
            if not _said(q, hay) and q[:90] not in missing:
                missing.append(q[:90])
    return missing


def usage_usd(usage, model: str) -> float:
    if usage is None:
        return 0.0
    p_in, p_out, p_cr, p_cw = PRICES.get(model) or DEAREST

    def g(k: str) -> float:
        v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, 0)
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    return (g("input_tokens") * p_in + g("output_tokens") * p_out + g("cache_read_input_tokens") * p_cr
            + g("cache_creation_input_tokens") * p_cw) / 1_000_000.0


def _text(msg) -> str:
    return "".join(getattr(b, "text", "") or "" for b in (getattr(msg, "content", None) or [])
                   if getattr(b, "type", "") == "text")


# ------------------------------------------------------------------ the call
def system_blocks(rubric_text: str) -> list[dict]:
    return [{"type": "text", "text": PROTOCOL + "\n\nTHE RUBRIC\n\n" + rubric_text, "cache_control": {"type": "ephemeral"}}]


def user_text(transcript: str, meta: dict) -> str:
    kind = str(meta.get("kind") or "")
    lines = ["The call to grade.",
             "Our caller: %s" % (meta.get("caller") or "not named in the log"),
             "When: %s" % (meta.get("when") or "unknown"),
             "Length: %s" % (meta.get("length") or "unknown"),
             "Rubric: the %s rubric above" % PIPELINE_WORDS.get(meta.get("pipeline") or "", "call"),
             "Why it was picked: one of the caller's %s calls, %s" % (kind or "picked", meta.get("why") or "no reason given"),
             "Note focus: %s" % ("reinforce" if kind == "best" else "fix")]
    if meta.get("labels_swapped"):
        lines.append("The triage thinks the AGENT and SELLER labels are swapped on this transcript: check by content.")
    return "\n".join(lines) + "\n\nTRANSCRIPT\n\n" + (transcript or "")


def _client():
    import anthropic
    return anthropic.Anthropic(max_retries=4)


def model_of() -> str:
    return (os.environ.get("AGENTS_COACH_MODEL") or os.environ.get("AGENTS_MODEL") or "claude-opus-5-5").strip()


def effort_of() -> str:
    e = (os.environ.get("AGENTS_COACH_EFFORT") or "high").strip().lower()
    return e if e in EFFORTS else "high"


def grade(transcript: str, meta: dict, pipeline: str, *, client=None, model: str = "", effort: str = "",
          base: Path | None = None) -> dict:
    """Grade one call. Returns {"usd", "model", "report", "scores", "note", "unverified", "problems"};
    `scores` is None whenever there is no grade to record."""
    text_r = rubric(pipeline, base)
    w = weights(text_r)
    model = model or model_of()
    effort = effort if effort in EFFORTS else effort_of()
    client = client or _client()
    kw = dict(model=model, max_tokens=32000, system=system_blocks(text_r),
              messages=[{"role": "user", "content": user_text(transcript, dict(meta, pipeline=pipeline))}],
              output_config={"effort": effort}, betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"})
    with client.beta.messages.stream(**kw) as s:
        msg = s.get_final_message()
    out = {"usd": round(usage_usd(getattr(msg, "usage", None), model), 4), "model": model, "report": "",
           "scores": None, "note": None, "unverified": [], "problems": []}
    if getattr(msg, "stop_reason", "") == "refusal":
        out["problems"].append("the model declined to grade this call")
        return out
    report = scrub_links(clean(_text(msg)))
    out["report"] = report
    if getattr(msg, "stop_reason", "") == "max_tokens":
        out["problems"].append("the report was cut off before its end")
    blocks = json_blocks(report)
    scores = next((b for b in blocks if "call_type" in b), None)
    note = next((b for b in blocks if "focus" in b), None)
    if scores is None:
        out["problems"].append("the report came back without its SCORES JSON, so no grade is recorded")
        return out
    scores = dict(scores)
    scores.pop("recording_url", None)
    kind = str(scores.get("call_type") or "").strip().lower()
    scores["call_type"] = kind
    if kind == "full":
        again = recompute(scores.get("categories") or {}, w)
        said = _number(scores.get("total"))
        if again is None:
            out["problems"].append("the full-call grade carries no category scores, so its total cannot be checked")
            total = said
        else:
            if said is None or abs(said - again) > 0.5:
                out["problems"].append("the total did not foot (it said %s, its categories give %.1f); %.1f is kept" % (
                    "nothing" if said is None else "%.1f" % said, again, again))
            total = again
        scores["total"] = total
        failed = str(scores.get("auto_fail") or "PASS").strip().upper().startswith("FAIL")
        scores["band"] = "FAIL" if failed else band(total)
    elif kind == "short":
        scores["total"], scores["band"] = None, None
    else:
        scores["call_type"] = "not_scored"
        scores["total"], scores["band"] = None, None
    out["scores"] = scores
    if isinstance(note, dict):
        out["note"] = {k: clean(str(note.get(k) or "")) for k in ("focus", "criterion", "said", "instead", "why")}
    missing = unverified_quotes(report, transcript, out["note"])
    out["unverified"] = missing
    if missing:
        out["problems"].append("%d quote(s) in the report are not in the transcript; check them before a caller sees the notes"
                               % len(missing))
    return out
