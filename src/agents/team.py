"""The team: who works here, what each one owns, and how a message finds the right one.

team/roster.yaml is the single source of truth (Tyler's bootcamp folder contract, centralized):
four employees with an id that is their role (analysis, research, sales, ops) and a persona that
can be renamed per install (Atlas, Diggs, Roma, Quinn). From it this module derives:

  * TEAM.md, the org chart and shared rules every employee reads on every run,
  * each employee's runtime workspace (persona + TEAM.md + the skills it carries), built OUTSIDE
    the repo so no unrelated CLAUDE.md or personal setting leaks into an employee's context,
  * Slack routing (a greeting, a name, or the channel decides who answers),
  * the DataSift webhook routes (a workflow's datasift:<slug> trigger),
  * the Slack app manifest, so creating the app is one click.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

from . import config

SPEND = ("free", "paid", "records", "outreach")
STATUS = ("live", "planned")
TRIGGER_KINDS = ("slack", "datasift", "schedule", "chain")
EXTRA_BUILTINS = {"WebSearch", "WebFetch"}      # the only built-ins an employee may be granted
SHARED_TOOLS = {"record_lookup"}
SPEND_LABEL = {"free": "free", "paid": "paid, inside the caps", "records": "asks first (record allowance)",
               "outreach": "staged, a human releases"}


class RosterError(ValueError):
    pass


@dataclass
class Workflow:
    name: str
    owner: str
    summary: str
    status: str
    spend: str
    triggers: list[str]
    inputs: dict = field(default_factory=dict)
    manual: str = ""
    sources: list[str] = field(default_factory=list)
    model: str = ""
    covers: list[str] = field(default_factory=list)   # org-chart agent ids (docs/agents.json) this runs
    autopilot: bool = False

    @property
    def live(self) -> bool:
        return self.status == "live"

    def trigger_values(self, kind: str) -> list[str]:
        return [t.split(":", 1)[1].strip() for t in self.triggers if t.split(":", 1)[0] == kind and ":" in t]

    def runs_on(self) -> str:
        out = []
        for t in self.triggers:
            kind, _, val = t.partition(":")
            out.append({"slack": "ask in Slack", "datasift": "DataSift sequence (%s)" % val,
                        "schedule": "schedule (%s)" % val, "chain": "after %s" % val}.get(kind, t))
        return ", ".join(out)


@dataclass
class Employee:
    id: str
    display_name: str
    role: str
    icon_emoji: str
    slack_channel: str
    model: str
    job: str
    introduction: str
    skills: list[str] = field(default_factory=list)
    builtins: list[str] = field(default_factory=list)
    effort: str = ""

    @property
    def name(self) -> str:
        return self.display_name


@dataclass
class Roster:
    team_channel: str
    employees: dict[str, Employee]
    workflows: dict[str, Workflow]
    shared_tools: list[str]
    warnings: list[str] = field(default_factory=list)

    def order(self) -> list[str]:
        return list(self.employees)

    def next_after(self, emp_id: str) -> str | None:
        ids = self.order()
        i = ids.index(emp_id)
        return ids[i + 1] if i + 1 < len(ids) else None

    def owned(self, emp_id: str) -> list[Workflow]:
        return [w for w in self.workflows.values() if w.owner == emp_id]

    def find(self, word: str) -> Employee | None:
        w = (word or "").strip().lower()
        for e in self.employees.values():
            if w in (e.id.lower(), e.display_name.lower()):
                return e
        return None

    def by_channel(self, channel_name: str) -> Employee | None:
        c = (channel_name or "").lstrip("#").lower()
        return next((e for e in self.employees.values() if e.slack_channel.lstrip("#").lower() == c), None) if c else None

    def datasift_routes(self) -> dict[str, tuple[str, str]]:
        return {slug: (w.owner, w.name) for w in self.workflows.values() for slug in w.trigger_values("datasift")}

    def which_text(self) -> str:
        names = ", ".join("%s (%s)" % (e.display_name, e.role.lower()) for e in self.employees.values())
        first = self.employees[self.order()[0]].display_name if self.employees else "Atlas"
        sales = self.employees.get("sales")
        example = "%s, quick screen 123 Main St" % (sales.display_name if sales else first)
        return "Which of us should take this? %s. Start with a name, for example: %s" % (names, example)


def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def parse(data: dict, skills_dir: Path | None = None) -> Roster:
    """Build and validate a roster. Every problem is collected and raised together, so one run of
    `cli.py team` shows the author everything to fix rather than one error per attempt."""
    problems: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        raise RosterError("roster.yaml must be a mapping")
    emps: dict[str, Employee] = {}
    for eid, e in (data.get("employees") or {}).items():
        if not isinstance(e, dict):
            problems.append("employee %s is not a mapping" % eid)
            continue
        missing = [k for k in ("display_name", "role", "slack_channel", "model", "job", "introduction") if not e.get(k)]
        if missing:
            problems.append("employee %s is missing %s" % (eid, ", ".join(missing)))
        extra = sorted(set(_list(e.get("builtins"))) - EXTRA_BUILTINS)
        if extra:
            problems.append("employee %s asks for built-ins %s; only %s may be granted" % (
                eid, extra, sorted(EXTRA_BUILTINS)))
        emps[eid] = Employee(
            id=eid, display_name=str(e.get("display_name") or eid), role=str(e.get("role") or ""),
            icon_emoji=str(e.get("icon_emoji") or ""), slack_channel=str(e.get("slack_channel") or "").lstrip("#"),
            model=str(e.get("model") or config.MODEL), job=" ".join(str(e.get("job") or "").split()),
            introduction=" ".join(str(e.get("introduction") or "").split()), skills=[str(s) for s in _list(e.get("skills"))],
            builtins=[str(b) for b in _list(e.get("builtins"))], effort=str(e.get("effort") or ""))
    if not emps:
        problems.append("the roster has no employees")
    for label, attr in (("display name", "display_name"), ("slack channel", "slack_channel")):
        seen: dict[str, str] = {}
        for e in emps.values():
            k = getattr(e, attr).lower()
            if k in seen:
                problems.append("%s %r is used by both %s and %s" % (label, k, seen[k], e.id))
            seen[k] = e.id
    if skills_dir is not None:
        for e in emps.values():
            for s in e.skills:
                if not (skills_dir / s / "SKILL.md").is_file():
                    warnings.append("%s carries skill %r, which has no SKILL.md under %s" % (e.id, s, skills_dir))

    wfs: dict[str, Workflow] = {}
    for name, w in (data.get("workflows") or {}).items():
        if not isinstance(w, dict):
            problems.append("workflow %s is not a mapping" % name)
            continue
        wf = Workflow(name=name, owner=str(w.get("owner") or ""), summary=" ".join(str(w.get("summary") or "").split()),
                      status=str(w.get("status") or "planned"), spend=str(w.get("spend") or "free"),
                      triggers=[str(t) for t in _list(w.get("triggers"))], inputs=dict(w.get("inputs") or {}),
                      manual=str(w.get("manual") or ""), sources=[str(s) for s in _list(w.get("sources"))],
                      model=str(w.get("model") or ""), covers=[str(c) for c in _list(w.get("covers"))],
                      autopilot=bool(w.get("autopilot")))
        dup = sorted({c for c in wf.covers if wf.covers.count(c) > 1})
        if dup:
            problems.append("workflow %s lists %s in covers more than once" % (name, dup))
        if wf.owner not in emps:
            problems.append("workflow %s is owned by %r, who is not on the roster" % (name, wf.owner))
        if wf.status not in STATUS:
            problems.append("workflow %s has status %r (use %s)" % (name, wf.status, "/".join(STATUS)))
        if wf.spend not in SPEND:
            problems.append("workflow %s has spend %r (use %s)" % (name, wf.spend, "/".join(SPEND)))
        if not wf.summary:
            problems.append("workflow %s has no summary" % name)
        for t in wf.triggers:
            kind = t.split(":", 1)[0]
            if kind not in TRIGGER_KINDS:
                problems.append("workflow %s has trigger %r (kinds: %s)" % (name, t, ", ".join(TRIGGER_KINDS)))
        wfs[name] = wf
    for wf in wfs.values():
        for target in wf.trigger_values("chain"):
            if target not in wfs:
                problems.append("workflow %s chains after %r, which is not in the catalog" % (wf.name, target))
    slugs: dict[str, str] = {}
    for wf in wfs.values():
        for slug in wf.trigger_values("datasift"):
            if slug in slugs:
                problems.append("DataSift route %r is claimed by both %s and %s" % (slug, slugs[slug], wf.name))
            slugs[slug] = wf.name
    shared = [str(s) for s in _list(data.get("shared_tools"))]
    unknown = sorted(set(shared) - SHARED_TOOLS)
    if unknown:
        problems.append("shared_tools %s are not tools the team has (known: %s)" % (unknown, sorted(SHARED_TOOLS)))
    if problems:
        raise RosterError("roster.yaml has %d problem(s):\n  - %s" % (len(problems), "\n  - ".join(problems)))
    return Roster(team_channel=str(data.get("team_channel") or "ai-team").lstrip("#"), employees=emps,
                  workflows=wfs, shared_tools=shared, warnings=warnings)


_CACHE: dict = {}

# ------------------------------------------------------------------------------- the org chart
# docs/agents.json is the public org chart (learn.datasift.ai/agent-org-chart). Every agent on it
# that is not retired must be run by at least one workflow here, so nothing in SiftStack sits
# outside the team. The Fly image does not ship docs/, so the check runs where the file exists
# (the repo, CI, the selftest) and the roster never fails to load because of it.
ORG_CHART = config.ROOT / "docs" / "agents.json"
PLATFORM = {"operator": "the person running the business", "orchestrator": "Claude itself, directing the team",
            "core": "the shared spine every workflow imports"}


def org_chart_agents(path: Path | None = None) -> dict[str, dict]:
    """id -> {name, dept, status, phase, source} for every agent on the org chart; {} without the file."""
    p = Path(path or ORG_CHART)
    if not p.is_file():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}

    def add(a, dept: str) -> None:
        if isinstance(a, dict) and a.get("id"):
            out[str(a["id"])] = {"name": str(a.get("name") or a["id"]), "dept": dept, "phase": a.get("phase"),
                                 "status": str(a.get("status") or ""), "source": str(a.get("source") or "")}

    for a in _list(d.get("exec")):
        add(a, "Executive")
    add(d.get("core") or {}, "Core")
    for dep in d.get("departments") or []:
        for a in dep.get("agents") or []:
            add(a, str(dep.get("name") or dep.get("id") or ""))
    return out


def coverage(roster: Roster, agents: dict | None = None) -> dict:
    """Which workflow runs each org-chart agent, and what is left over. `problems` is what the
    team check fails on: an id that is not on the chart, a live agent no workflow runs."""
    agents = org_chart_agents() if agents is None else agents
    by_agent: dict[str, list[str]] = {}
    for w in roster.workflows.values():
        for aid in w.covers:
            by_agent.setdefault(aid, []).append(w.name)
    unknown = sorted("%s -> %s" % (w.name, aid) for w in roster.workflows.values() for aid in w.covers
                     if agents and aid not in agents)
    retired = sorted(a for a, v in agents.items() if v["status"] == "retired")
    uncovered = sorted(a for a, v in agents.items()
                       if v["status"] != "retired" and a not in PLATFORM and a not in by_agent)
    revived = sorted(a for a in retired if a in by_agent)
    problems = (["workflow %s covers an id the org chart does not have" % u for u in unknown]
                + ["org-chart agent %s (%s) is run by no workflow" % (a, agents[a]["name"]) for a in uncovered]
                + ["retired agent %s is still listed in covers" % a for a in revived])
    return {"agents": len(agents), "covered": sum(1 for a in agents if a in by_agent), "by_agent": by_agent,
            "platform": sorted(a for a in agents if a in PLATFORM), "retired": retired, "uncovered": uncovered,
            "unknown": unknown, "off_chart": sorted(w.name for w in roster.workflows.values() if not w.covers),
            "problems": problems}


def render_coverage_md(roster: Roster, agents: dict) -> str:
    """team/COVERAGE.md: every org-chart agent, the workflow that runs it and the employee who owns
    that workflow. Generated with TEAM.md; deterministic, so `cli.py team --check` can gate it."""
    cov = coverage(roster, agents)
    E = roster.employees
    L = ["# Org chart coverage", "",
         "Every agent on docs/agents.json (the public org chart), the team workflow that runs it, and the"
         " employee who owns that workflow. %d agents: %d run by a workflow, %d platform, %d retired, %d"
         " not yet assigned." % (cov["agents"], cov["covered"], len(cov["platform"]), len(cov["retired"]),
                                 len(cov["uncovered"])), ""]
    for e in E.values():
        L += ["## %s (%s)" % (e.display_name, e.role), "", "| Workflow | Status | Org-chart agents it runs |",
              "|---|---|---|"]
        for w in roster.owned(e.id):
            names = ", ".join("%s (%s)" % (agents[a]["name"], a) if a in agents else a for a in w.covers)
            status = ("autopilot stage" if w.autopilot else "live") if w.live else "by hand today"
            L.append("| %s | %s | %s |" % (w.name, status, names or "none: not on the org chart yet"))
        L.append("")
    L += ["## Not workflows", "", "| Agent | Why |", "|---|---|"]
    L += ["| %s (%s) | %s |" % (agents[a]["name"], a, PLATFORM[a]) for a in cov["platform"]]
    L += ["| %s (%s) | retired |" % (agents[a]["name"], a) for a in cov["retired"]]
    if cov["uncovered"]:
        L += ["", "## Not yet assigned", ""] + ["- %s (%s), %s" % (agents[a]["name"], a, agents[a]["dept"])
                                               for a in cov["uncovered"]]
    L.append("")
    return "\n".join(L)


def with_autopilot(data: dict, playbook: Path) -> tuple[dict, list[str]]:
    """The roster plus every autopilot stage (team/autopilot.yaml) as a live workflow, so the team's
    org chart, Slack routing and the clock all see the playbook. A playbook that fails to read is a
    warning here, never a broken roster: the autopilot's own check reports it in full."""
    notes: list[str] = []
    if not playbook.is_file():
        return data, notes
    try:
        from .autopilot import policy as ap
        stages = ap.raw_stages(playbook)
    except Exception as exc:  # noqa: BLE001
        return data, ["team/autopilot.yaml did not load (%s); its stages are not on the roster" % exc]
    data = dict(data)
    wfs = dict(data.get("workflows") or {})
    for sid, s in stages.items():
        if sid in wfs:
            notes.append("autopilot stage %s has the same name as a roster workflow; the roster entry wins" % sid)
            continue
        if isinstance(s, dict):
            wfs[sid] = ap.as_workflow(sid, s)
    data["workflows"] = wfs
    return data, notes


def load(path: Path | None = None) -> Roster:
    """The roster, re-read only when it or the playbook changes (the worker asks on every task)."""
    p = Path(path or config.TEAM_DIR / "roster.yaml")
    pb = p.parent / "autopilot.yaml"
    mtime = (p.stat().st_mtime, pb.stat().st_mtime if pb.is_file() else 0)
    hit = _CACHE.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    data, notes = with_autopilot(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, pb)
    r = parse(data, skills_dir=config.SKILLS_DIR)
    r.warnings.extend(notes)
    _CACHE[str(p)] = (mtime, r)
    return r


# ------------------------------------------------------------------------------------ routing

_MENTION = re.compile(r"<[@#!][^>]*>")
_WORD = re.compile(r"[A-Za-z0-9']+")
GREETINGS = ("hello world", "good morning", "good afternoon", "good evening", "morning team", "meet the team",
             "introduce yourselves", "introduce yourself", "hello team", "hi team", "hey team")
_FILLER = {"hey", "hi", "hello", "yo", "ok", "okay", "good", "morning", "afternoon", "evening", "team", "world",
           "please", "so", "and"}


def clean(text: str) -> str:
    return " ".join(_MENTION.sub(" ", text or "").split())


def _split_address(roster: Roster, text: str) -> tuple[Employee | None, str]:
    """Who the message is addressed to, read from its first words: "Roma, quick screen 123 Main",
    "good morning Diggs: deep prospect ...", "hey atlas pull knox". Only the opening counts, so an
    employee's name inside a sentence never steals the message."""
    pos = 0
    for m in _WORD.finditer(text):
        word = m.group(0).lower()
        if word in _FILLER:
            pos = m.end()
            continue
        emp = roster.find(word)
        if emp:
            return emp, text[m.end():].lstrip(" ,:;-.!").strip()
        break
    return None, text[pos:].strip() if pos else text


def route_message(roster: Roster, channel_name: str, text: str) -> dict:
    """{"kind": "hello"} | {"kind": "ask", "agent", "text"} | {"kind": "which"}."""
    t = clean(text)
    low = t.lower()
    emp, rest = _split_address(roster, t)
    if emp:
        return {"kind": "ask", "agent": emp.id, "text": rest or t}
    if any(g in low for g in GREETINGS):
        leftover = [w for w in _WORD.findall(low) if w not in _FILLER and w not in ("meet", "the", "introduce",
                                                                                     "yourselves", "yourself")]
        if not leftover:
            return {"kind": "hello"}
    ch = roster.by_channel(channel_name)
    if ch:
        return {"kind": "ask", "agent": ch.id, "text": t}
    return {"kind": "which"}


# ------------------------------------------------------------------------------------ TEAM.md

def render_team_md(roster: Roster) -> str:
    """The org chart and the rules every employee shares. Deterministic (no dates, no live caps),
    so it is a stable, cacheable prefix of every employee's prompt; the live mode and caps are
    appended per run."""
    E = roster.employees
    L = ["# The team", "",
         "You are one employee on a %d-person AI team that works for a real estate operator. Each of you"
         " owns specific work. When you need something you do not own, hand it to the owner." % len(E), "",
         "## Employees", "", "| Name | Role | Slack | What they do for the operator |", "|---|---|---|---|"]
    for e in E.values():
        L.append("| %s | %s | #%s | %s |" % (e.display_name, e.role, e.slack_channel, e.job))
    L += ["", "The whole team also listens in #%s." % roster.team_channel, "", "## Who owns what", ""]
    for e in E.values():
        L += ["### %s (%s)" % (e.display_name, e.role), "", "| Workflow | What it does | Runs on | Spend | Status |",
              "|---|---|---|---|---|"]
        for w in roster.owned(e.id):
            status = "live" if w.live else "by hand today"
            L.append("| %s | %s | %s | %s | %s |" % (w.name, w.summary, w.runs_on(), SPEND_LABEL[w.spend], status))
        L.append("")
    shared = ", ".join(roster.shared_tools) or "none"
    L += ["Every employee can also use: %s." % shared, "",
          "## Handoffs", "",
          "1. Never pretend to own a workflow you do not. Hand it to the owner with the handoff tool.",
          "2. A handoff says what you already know and exactly what you need back, so the owner never"
          " re-asks the operator something you already learned.",
          "3. Handoffs stay on the same thread. The platform counts hops and stops a chain that runs long.",
          "4. Never hand work to yourself. One clear handoff beats a chain of vague ones.",
          "5. Before you act, read the thread. When a teammate finishes, build on their result.", "",
          "## Asking the operator", "",
          "- A required input is missing: ask one short question with ask_operator, then end your turn."
          " The operator's reply in the same thread wakes you up where you left off.",
          "- Anything irreversible, or anything that spends the record allowance: ask first, with the number.",
          "- Never invent a critical fact to avoid asking.", "",
          "## Money and outreach", "",
          "- free: run it. paid: run it inside the per-run and per-day caps; a step that would cross a cap"
          " is skipped and named. records: ask the operator first. outreach: stage it only; a human releases it.",
          "- In dry mode nothing is paid and nothing is written to DataSift. Say so when it matters.",
          "- A workflow marked 'by hand today' is not wired into the team yet. Say so plainly and offer what"
          " you can do; never act as if it ran.", "",
          "## Writing", "",
          "- Your final reply is posted to Slack exactly as you write it. Keep it to three short lines at"
          " most: plain sentences, no headings, no tables, no bullet lists, no labels like 'Slack line:'.",
          "- The DataSift record is the system of record, so the detail goes there and the reply says where"
          " (in dry mode, say the detail was not written).",
          "- A workflow that is not wired cannot be run by hand by you either. Say who can run it, not that"
          " you will.",
          "- Never use em dashes or en dashes. Use commas, periods, colons or parentheses; number ranges"
          " use a plain hyphen.",
          "- Keep private details (phone numbers, ages, family members) out of channels unless the operator"
          " needs them to act right now.",
          "- You cannot run shell commands or edit files. Your tools do the work, and the skills in your"
          " workspace describe methods for reference.", ""]
    return "\n".join(L)


def persona(emp_id: str) -> str:
    p = config.TEAM_DIR / emp_id / "CLAUDE.md"
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def system_prompt(roster: Roster, emp_id: str) -> str:
    return persona(emp_id) + "\n\n" + render_team_md(roster)


def _tree_signature(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for root in paths:
        if root.is_file():
            st = root.stat()
            h.update(("%s|%d|%d" % (root.name, st.st_size, int(st.st_mtime))).encode())
            continue
        for f in sorted(root.rglob("*")) if root.is_dir() else []:
            if f.is_file() and "__pycache__" not in f.parts:
                st = f.stat()
                h.update(("%s|%d|%d" % (f.relative_to(root).as_posix(), st.st_size, int(st.st_mtime))).encode())
    return h.hexdigest()


def materialize(roster: Roster, emp_id: str, root: Path | None = None) -> Path:
    """Build (or reuse) the employee's runtime workspace:

        <workspaces>/<id>/EMPLOYEE.md            persona + TEAM.md (also passed as the system prompt)
        <workspaces>/<id>/brain/                 the employee's reference docs, if team/<id>/brain exists
        <workspaces>/<id>/.claude/skills/<name>  each skill the employee carries

    Rebuilt only when a source changes. EMPLOYEE.md is deliberately not named CLAUDE.md: the
    prompt is passed explicitly, and a CLAUDE.md would load it a second time."""
    emp = roster.employees[emp_id]
    ws = Path(root or config.WORKSPACES_DIR) / emp_id
    text = system_prompt(roster, emp_id)
    brain = config.TEAM_DIR / emp_id / "brain"
    skill_dirs = [config.SKILLS_DIR / s for s in emp.skills if (config.SKILLS_DIR / s / "SKILL.md").is_file()]
    sig = hashlib.sha256((text + _tree_signature([brain] + skill_dirs)).encode()).hexdigest()
    marker = ws / ".signature"
    if marker.is_file() and marker.read_text() == sig:
        return ws
    ws.mkdir(parents=True, exist_ok=True)
    for sub in (ws / ".claude" / "skills", ws / "brain"):
        if sub.exists():
            shutil.rmtree(sub)     # our own generated workspace, never the repo
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "dist", "node_modules", ".git")
    for d in skill_dirs:
        shutil.copytree(d, ws / ".claude" / "skills" / d.name, ignore=ignore)
    if brain.is_dir():
        shutil.copytree(brain, ws / "brain", ignore=ignore)
    (ws / "EMPLOYEE.md").write_text(text, encoding="utf-8")
    marker.write_text(sig)
    return ws


# ------------------------------------------------------------------------------------ Slack app

# channels:join lets the app join a PUBLIC channel by id on its own (slack_app.join), so nobody has
# to invite it before it can post or hear there. A private channel still needs the invite.
BOT_SCOPES = ["app_mentions:read", "channels:history", "channels:join", "channels:read", "chat:write",
              "chat:write.customize", "groups:history", "groups:read", "im:history", "reactions:write"]
BOT_EVENTS = ["app_mention", "message.channels", "message.groups", "message.im"]


def slack_manifest(public_url: str, app_name: str = "SiftStack Team") -> dict:
    """The Slack app, as a manifest. chat:write.customize lets each employee post under its own name
    and icon; the request URL is this brain's /slack/events, so the brain must be deployed first."""
    return {
        "display_information": {"name": app_name, "description": "Your AI team: Analysis, Research, Sales, Ops.",
                                "background_color": "#0a1130"},
        "features": {"bot_user": {"display_name": app_name, "always_online": True},
                     "app_home": {"messages_tab_enabled": True, "messages_tab_read_only_enabled": False}},
        "oauth_config": {"scopes": {"bot": BOT_SCOPES}},
        "settings": {"event_subscriptions": {"request_url": public_url.rstrip("/") + "/slack/events",
                                             "bot_events": BOT_EVENTS},
                     "org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
    }


def manifest_url(manifest: dict) -> str:
    """One click: opens Slack's create-app page with every scope and event already filled in."""
    return "https://api.slack.com/apps?new_app=1&manifest_json=" + quote(json.dumps(manifest, separators=(",", ":")))
