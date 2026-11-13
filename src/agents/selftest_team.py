"""Team selftest: the roster, routing, the bus, Slack in and out, the tools, the runner and the
hello chain. Zero network: Slack, DataSift and the Claude Agent SDK are all fakes, the database is
a fresh file, tasks run by id (never a blind drain), and the deep prospect handler is swapped for a
canned one so nothing can spend.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path

from . import config

U3 = "33333333-2222-3333-4444-555555555555"
U4 = "44444444-2222-3333-4444-555555555555"
REC = {"uuid": U3, "address": {"street": "123 Main St", "city": "Knoxville", "state": "TN", "postal_code": "37914",
                               "county": "Knox"},
       "owner": {"first_name": "Pat", "last_name": "Example", "phones": [{"number": "8655550101"}]},
       "status": {"title": "Warm Lead"}, "lists": [{"title": "Probate"}], "tags": ["Courthouse Data"],
       "estimate_value": 180000, "bedrooms": 3}
REC2 = dict(REC, uuid=U4, address={"street": "12 Oak St", "city": "Maryville", "state": "TN", "postal_code": "37801"})


class FakeCRM:
    def get_record(self, uuid):
        return {U3: REC, U4: REC2}.get(uuid, {})

    def search_by_address(self, q, limit=10, offset=0):
        q = q.lower()
        if "123 main" in q:
            return [REC]
        if "oak" in q:
            return [REC2, dict(REC2, uuid=U3)]
        return []


class _Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


class FakeSlack:
    names = {"CROMA": "roma-sales", "CTEAM": "ai-team", "CDIGGS": "diggs-research"}

    def __init__(self):
        self.calls, self.n = [], 0
        self.outside, self.private, self.down = set(), set(), False   # not a member yet / refuses a join / all fail

    def post(self, url, json=None, timeout=None, headers=None):
        method, ch = url.rsplit("/", 1)[-1], (json or {}).get("channel", "")
        self.n += 1
        self.calls.append((method, dict(json or {})))
        if self.down:
            return _Resp({"ok": False, "error": "invalid_auth"})
        if method == "apps.connections.open":
            self.opened = getattr(self, "opened", 0) + 1
            return _Resp({"ok": True, "url": "wss://socket.example.test/link/%d" % self.opened})
        if method == "conversations.join":
            if ch in self.private:
                return _Resp({"ok": False, "error": "method_not_supported_for_channel_type"})
            self.outside.discard(ch)
            return _Resp({"ok": True})
        if method == "chat.postMessage" and ch in self.outside:
            return _Resp({"ok": False, "error": "not_in_channel"})
        return _Resp({"ok": True, "ts": "1700000000.%06d" % self.n})

    def get(self, url, params=None, timeout=None, headers=None):
        cid = (params or {}).get("channel", "")
        return _Resp({"ok": cid in self.names, "channel": {"name": self.names.get(cid, "")}})

    def posts(self):
        return [b for m, b in self.calls if m == "chat.postMessage"]


class FakeSDK:
    """Stands in for claude_agent_sdk: records the options, runs the scripted tool calls through the
    real guard hook and the real tool handlers, and returns a ResultMessage."""
    script: list = []
    options: list = []
    prompts: list = []
    denied: list = []
    outputs: list = []

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.kw = kw

    class ResultMessage:
        def __init__(self, **kw):
            self.__dict__.update({"result": "", "session_id": "", "total_cost_usd": 0.0, "num_turns": 0,
                                  "is_error": False, "subtype": "success", "errors": None})
            self.__dict__.update(kw)

    class HookMatcher:
        def __init__(self, matcher=None, hooks=None, timeout=None):
            self.matcher, self.hooks = matcher, hooks or []

    @staticmethod
    def tool(name, description, schema):
        def deco(fn):
            return {"name": name, "description": description, "schema": schema, "handler": fn}
        return deco

    @staticmethod
    def create_sdk_mcp_server(name, version="1.0.0", tools=None):
        return {"name": name, "tools": {t["name"]: t for t in tools or []}}

    @staticmethod
    async def query(prompt, options):
        FakeSDK.options.append(options)
        FakeSDK.prompts.append(prompt)
        step = FakeSDK.script.pop(0)
        server = options.mcp_servers["team"]
        guard = options.hooks["PreToolUse"][0].hooks[0]
        for name, args in step.get("calls", []):
            full = name if name[0].isupper() else "mcp__team__" + name
            if await guard({"tool_name": full, "tool_input": args}, None, {}):
                FakeSDK.denied.append(name)
                continue
            FakeSDK.outputs.append((name, await server["tools"][name]["handler"](args)))
        yield FakeSDK.ResultMessage(**({"result": step.get("text", ""), "session_id": "sess-%d" % len(FakeSDK.options),
                                        "total_cost_usd": 0.11, "num_turns": 2} | step.get("result", {})))


def _text_of(out: dict) -> str:
    return " ".join(c.get("text", "") for c in out.get("content", []))


def _sign(secret: str, body: bytes, ts: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": "application/json"}


def run(check, tmp: Path) -> None:
    default_ws = config.WORKSPACES_DIR
    config.DB_PATH, config.WORKSPACES_DIR = tmp / "team.db", tmp / "workspaces"
    config.SLACK_BOT_TOKEN, config.SLACK_SIGNING_SECRET, config.USE_LLM = "", "", False
    config.SLACK_CHANNEL = config.SLACK_APP_TOKEN = ""   # this machine's own env may carry both; the checks set their own
    config.DAILY_CAP_USD, config.AGENT_BUDGET_USD, config.MAX_HOPS = 25.0, 2.0, 6
    from . import bus, hello, intake, router, runner, slack_app, slack_socket, store, team, tools, worker
    store.init()
    tools.CRM = FakeCRM()
    fake_slack = FakeSlack()
    door_shut = slack_app._http     # whatever stood there on the way in goes back on the way out
    slack_app._http = slack_socket._http = fake_slack
    slack_app._CHANNEL_NAMES.clear()
    real_dp = worker.HANDLERS["deep_prospect"]
    worker.HANDLERS["deep_prospect"] = (lambda t: {"street": "123 Main St", "headline": "daughter signs"},
                                        lambda t, r: "Deep prospect done for %s: %s" % (r["street"], r["headline"]))

    def run_id(tid):
        store.mark(tid, "running")
        return worker.run_task(store.task(tid))

    try:
        print("team: roster")
        roster = team.load()
        check(roster.order() == ["analysis", "research", "sales", "ops"], "four employees, in hello order")
        check([e.display_name for e in roster.employees.values()] == ["Atlas", "Diggs", "Roma", "Quinn"],
              "Tyler's persona names are the display names")
        check(len(roster.workflows) >= 25 and not roster.warnings, "the catalog loads with no missing skills")
        check(all(worker.handler_for(w.name) for w in roster.workflows.values() if w.live),
              "every live workflow has a worker handler (fixed handlers plus the autopilot playbook)")
        try:
            team.parse({"employees": {"x": {"display_name": "X", "role": "r", "slack_channel": "c", "model": "m",
                                            "job": "j", "introduction": "i", "builtins": ["Bash"]}},
                        "workflows": {"w": {"owner": "nobody", "summary": "s", "status": "maybe"}}})
            check(False, "a broken roster is refused")
        except team.RosterError as exc:
            msg = str(exc)
            check("Bash" in msg and "nobody" in msg and "maybe" in msg, "every roster problem is reported at once")
        check(router.routes(roster).get("deep-prospect") == ("research", "deep_prospect"), "deep-prospect routes to Diggs")
        check("quick-screen" not in router.routes(roster), "a planned workflow's webhook is not routed")
        md = team.render_team_md(roster)
        check(md == team.render_team_md(roster), "TEAM.md renders the same every time (a stable prompt prefix)")
        check(all(w in md for w in roster.workflows) and all(e.display_name in md for e in roster.employees.values()),
              "TEAM.md names every employee and every workflow")
        check("—" not in md and "–" not in md, "TEAM.md has no em or en dash")
        committed = (config.TEAM_DIR / "TEAM.md").read_text(encoding="utf-8")
        check(committed.endswith(md), "team/TEAM.md matches the roster (run cli.py team after editing it)")
        for e in roster.employees.values():
            p = team.persona(e.id)
            check(p.startswith("# %s" % e.display_name) and "—" not in p and "–" not in p,
                  "%s's brief exists, is titled, and has no dash" % e.display_name)

        print("team: org chart coverage")
        agents = team.org_chart_agents()
        if agents:
            cov = team.coverage(roster, agents)
            check(not cov["uncovered"], "every live org-chart agent is run by a workflow %s" % cov["uncovered"][:4])
            check(not cov["unknown"], "every covers id is on the org chart %s" % cov["unknown"][:3])
            check(cov["covered"] + len(cov["platform"]) + len(cov["retired"]) == cov["agents"],
                  "the org chart adds up: run by a workflow + platform + retired = all %d" % cov["agents"])
            fake = dict(agents, zzlive={"name": "New Agent", "dept": "x", "phase": 1, "status": "live", "source": ""})
            check(any("zzlive" in p for p in team.coverage(roster, fake)["problems"]),
                  "a new org-chart agent no workflow runs is a problem, not a silent gap")
            r2 = team.parse({"employees": {"x": {"display_name": "X", "role": "r", "slack_channel": "c", "model": "m",
                                                 "job": "j", "introduction": "i"}},
                             "workflows": {"w": {"owner": "x", "summary": "s", "covers": ["nosuch", "shot"]}}})
            probs = " ".join(team.coverage(r2, agents)["problems"])
            check("nosuch" in probs and "retired agent shot" in probs,
                  "an id off the chart, or a retired agent, in covers is a problem")
            cmd = team.render_coverage_md(roster, agents)
            check("—" not in cmd and "–" not in cmd and cmd == team.render_coverage_md(roster, agents),
                  "COVERAGE.md is deterministic and dash-free")
            cov_file = config.TEAM_DIR / "COVERAGE.md"
            check(cov_file.is_file() and cov_file.read_text(encoding="utf-8").endswith(cmd),
                  "team/COVERAGE.md matches the roster and org chart (run cli.py team after editing either)")
        else:
            print("  (docs/agents.json not present here; coverage is checked in the repo)")

        print("team: routing")
        R = lambda ch, t: team.route_message(roster, ch, t)  # noqa: E731
        check(R("ai-team", "Roma, quick screen 123 Main St") == {"kind": "ask", "agent": "sales",
                                                              "text": "quick screen 123 Main St"}, "a name at the start picks the employee")
        check(R("ai-team", "good morning Diggs: deep prospect 123 Main St")["agent"] == "research",
              "a greeting before the name still routes to the name")
        check(R("ai-team", "<@U0BOT> atlas pull Knox notices")["agent"] == "analysis", "a bot mention is stripped")
        for g in ("good morning", "Hello World!", "meet the team", "Good morning team!"):
            check(R("ai-team", g) == {"kind": "hello"}, "%r starts meet-the-team" % g)
        check(R("ai-team", "good morning, can you look at 12 Oak?") == {"kind": "which"},
              "a greeting with a request in it is not the hello chain")
        check(R("roma-sales", "what about 12 Oak St?")["agent"] == "sales", "an employee's channel picks the employee")
        check(R("ai-team", "this reminds me of roma") == {"kind": "which"}, "a name mid-sentence does not steal the message")

        print("team: store migration")
        old = tmp / "old.db"
        c = sqlite3.connect(old)
        c.executescript("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, agent TEXT, "
                        "workflow TEXT, record_uuid TEXT, subject TEXT, status TEXT, attempts INTEGER DEFAULT 0, "
                        "started TEXT, finished TEXT, result TEXT, error TEXT, event_id INTEGER);"
                        "INSERT INTO tasks (created, agent, workflow, record_uuid, status) "
                        "VALUES ('2026-11-03', 'research', 'deep_prospect', 'x', 'done');")
        c.commit()
        c.close()
        live_db, config.DB_PATH = config.DB_PATH, old
        store.init()
        have = {r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(tasks)")}
        check({n for n, _ in store.TEAM_COLUMNS} <= have, "an old queue gains the team columns in place")
        row = store.task(1)
        check(row and row["status"] == "done" and row["mode"] == "direct", "old rows survive with sane defaults")
        config.DB_PATH = live_db

        print("team: bus")
        a, _ = store.enqueue("sales", "ask", "", payload={"text": "x"}, mode="agent", slack_channel="CROMA",
                             slack_thread="1700.1", source="slack")
        ta = store.task(a)
        h = bus.handoff(ta, roster, "Diggs", "Who signs for 123 Main St?", "Owner Pat Example")
        th = store.task(h)
        check(th["agent"] == "research" and th["thread_id"] == ta["thread_id"] and th["hop"] == 1 and th["parent_id"] == a
              and th["slack_thread"] == "1700.1" and th["source"] == "agent:sales",
              "a handoff keeps the thread and the Slack thread and counts a hop")
        check(store.payload_of(th)["known"] == "Owner Pat Example", "what the sender knows travels with the work")
        for args, why in ((("Roma", "x"), "an employee cannot hand work to itself"),
                          (("Nobody", "x"), "an unknown teammate is refused"), (("Diggs", "  "), "an empty ask is refused")):
            try:
                bus.handoff(ta, roster, *args)
                check(False, why)
            except bus.HandoffRefused:
                check(True, why)
        try:
            bus.handoff(dict(ta, hop=config.MAX_HOPS), roster, "Diggs", "x")
            check(False, "a chain past the hop limit is refused")
        except bus.HandoffRefused:
            check(True, "a chain past the hop limit is refused")

        print("team: slack transport")
        config.SLACK_SIGNING_SECRET = "sign-secret"
        body = b'{"type":"event_callback"}'
        check(slack_app.verify(_sign("sign-secret", body), body), "a correctly signed request verifies")
        check(not slack_app.verify(_sign("other", body), body), "a wrong signing secret fails")
        check(not slack_app.verify(_sign("sign-secret", body, str(int(time.time()) - 900)), body), "a replay fails")
        check(not slack_app.verify({}, body), "no signature fails")
        check(slack_app.clean_text("ARV — $180,000, rehab 4–6 weeks") == "ARV, $180,000, rehab 4-6 weeks",
              "dashes are cleaned out of anything the team says")
        check(slack_app.clean_text("**REVIEW** on 123 Main") == "*REVIEW* on 123 Main",
              "Markdown bold becomes Slack bold instead of showing its asterisks")
        check(slack_app.post("CROMA", "hi") == "" and not fake_slack.calls, "no bot token: falls back, never threads")
        config.SLACK_BOT_TOKEN = "xoxb-test"
        ts = slack_app.post("CROMA", "hi", thread_ts="1700.1", employee=roster.employees["sales"])
        p = fake_slack.posts()[-1]
        check(ts and p["thread_ts"] == "1700.1" and p["username"] == "Roma" and p["icon_emoji"],
              "a bot post goes in the thread under the employee's own name and icon")
        check(slack_app.channel_name("CROMA") == "roma-sales" and slack_app.channel_name("D123") == "",
              "channel ids resolve to names; a DM has none")

        print("team: hello chain")
        n0 = len(fake_slack.posts())
        first = hello.start(roster, "CTEAM", "1700.9")
        tid, seen = first, []
        while tid:
            run_id(tid)
            t = store.task(tid)
            seen.append((t["agent"], t["hop"], t["thread_id"]))
            nxt = [x for x in store.thread(t["thread_id"]) if x["status"] == "queued"]
            tid = nxt[0]["id"] if nxt else 0
        posts = fake_slack.posts()[n0:]
        check([s[0] for s in seen] == roster.order() and [s[1] for s in seen] == [0, 1, 2, 3],
              "everyone introduces themselves in order, one hop each")
        check(len({s[2] for s in seen}) == 1 and all(x.get("thread_ts") == "1700.9" for x in posts),
              "the whole chain is one job thread and one Slack thread")
        check([x.get("username") for x in posts[1:]] == ["Atlas", "Diggs", "Roma", "Quinn"], "each speaks as itself")
        check("Meet the team: complete" in posts[-1]["text"], "the last employee closes the chain")

        print("team: tools")
        ctx_s = tools.RunContext(task=ta, roster=roster, employee=roster.employees["sales"])
        names = [t.name for t in tools.build(ctx_s)]
        check(names[:4] == ["handoff", "ask_operator", "thread_history", "record_lookup"], "team and shared tools first")
        check("quick_screen" in names and "deep_prospect" not in names, "an employee gets only the workflows it owns")
        by = {t.name: t for t in tools.build(ctx_s)}
        out = asyncio.run(by["record_lookup"].handler({"record": "123 Main St"}))
        txt = _text_of(out)
        check('"owner": "Pat Example"' in txt and '"phones_on_owner": 1' in txt and "8655550101" not in txt,
              "record lookup reads the record and keeps phone numbers out of the model")
        amb = asyncio.run(by["record_lookup"].handler({"record": "12 Oak St"}))
        check(amb.get("is_error") and U3 in _text_of(amb) and U4 in _text_of(amb), "an ambiguous address lists the matches")
        pl = asyncio.run(by["quick_screen"].handler({"details": "x"}))
        check(pl.get("is_error") and "not wired" in _text_of(pl) and "comp_package" in _text_of(pl),
              "a planned workflow says so and how it runs by hand")
        ctx_r = tools.RunContext(task=th, roster=roster, employee=roster.employees["research"])
        dp = {t.name: t for t in tools.build(ctx_r)}["deep_prospect"]
        q1 = _text_of(asyncio.run(dp.handler({"record": "123 Main St"})))
        q2 = _text_of(asyncio.run(dp.handler({"record": "123 Main St"})))
        job = store.task(ctx_r.tickets[0])
        check("Queued deep_prospect" in q1 and job["mode"] == "direct" and job["record_uuid"] == U3 and
              job["thread_id"] == th["thread_id"], "a live workflow queues a direct job on the same thread")
        check("Already" in q2 and len(set(ctx_r.tickets)) == 1, "the same record is not queued twice")
        gated = team.Workflow(name="pull_test", owner="analysis", summary="s", status="live", spend="records", triggers=[])
        g = tools.workflow_tool(tools.RunContext(task=ta, roster=roster, employee=roster.employees["analysis"]), gated)
        check(asyncio.run(g.handler({})).get("is_error"), "a record-allowance job needs the operator's yes first")
        check("Queued" in _text_of(asyncio.run(g.handler({"approved": True}))), "with the yes it queues")
        run_id(job["id"])
        done = fake_slack.posts()[-1]
        check(store.task(job["id"])["status"] == "done" and "Deep prospect done" in done["text"] and
              done["username"] == "Diggs" and done["thread_ts"] == "1700.1", "the direct job posts in the thread as its owner")

        print("team: runner")
        ws = team.materialize(roster, "sales")
        check((ws / "EMPLOYEE.md").read_text(encoding="utf-8").startswith("# Roma") and not (ws / "CLAUDE.md").exists(),
              "the workspace carries the brief as EMPLOYEE.md, never a second CLAUDE.md")
        check((ws / ".claude" / "skills" / "real-estate-comping" / "SKILL.md").is_file(), "the employee's skills are in its workspace")
        check(not default_ws.resolve().is_relative_to(config.ROOT.resolve()), "workspaces live outside the repo by default")
        m1 = (ws / ".signature").stat().st_mtime
        team.materialize(roster, "sales")
        check((ws / ".signature").stat().st_mtime == m1, "an unchanged workspace is not rebuilt")

        runner._SDK, config.USE_LLM = FakeSDK, True
        FakeSDK.script = [{"calls": [("record_lookup", {"record": "123 Main St"}), ("deep_prospect", {"record": "x"}),
                                     ("handoff", {"to": "Diggs", "need": "Who signs for 123 Main St?",
                                                  "known": "Owner of record Pat Example"})],
                           "text": "Pulled 123 Main St. Diggs is checking who signs."}]
        s1, _ = store.enqueue("sales", "ask", "", subject="quick screen 123 Main St",
                              payload={"text": "quick screen 123 Main St", "channel_name": "roma-sales"}, mode="agent",
                              source="slack", slack_channel="CROMA", slack_thread="1700.20")
        res = run_id(s1)
        o = FakeSDK.options[-1]
        check(o.tools == ["Read", "Glob", "Grep", "Skill"] and set(runner.NEVER) <= set(o.disallowed_tools),
              "Roma has no shell, no file writes, no edits")
        check(o.permission_mode == "dontAsk" and o.strict_mcp_config and o.setting_sources == ["project"],
              "anything not pre-approved is denied and only the workspace's own settings load")
        check("mcp__team__quick_screen" in o.allowed_tools and "mcp__team__deep_prospect" not in o.allowed_tools,
              "Roma's approvals cover her tools, not Diggs'")
        check(o.cwd == str(ws) and o.model == "claude-opus-5-5" and 0 < o.max_budget_usd <= config.AGENT_BUDGET_USD,
              "runs in her workspace, on her model, under the turn budget")
        check(FakeSDK.denied == ["deep_prospect"], "the guard blocks a teammate's workflow even if the model asks for it")
        check(res and res["handoffs"] and res["handoffs"][0]["to"] == "Diggs", "Roma's handoff to Diggs is recorded")
        kid = res["handoffs"][0]["task"]
        check(store.task(kid)["agent"] == "research" and store.task(kid)["hop"] == 1, "Diggs has the work on the same thread")
        check(fake_slack.posts()[-1]["thread_ts"] == "1700.20" and fake_slack.posts()[-1]["username"] == "Roma",
              "Roma answers in the operator's thread")
        check(abs(store.spent(task_id=s1) - 0.11) < 1e-9, "the turn's Claude cost lands in the spend ledger")
        check("The operator wrote in #roma-sales" in FakeSDK.prompts[-1] and "Mode: dry" in FakeSDK.prompts[-1],
              "the prompt carries the ask, the thread and the live mode")
        guard = o.hooks["PreToolUse"][0].hooks[0]
        deny = lambda n, a: asyncio.run(guard({"tool_name": n, "tool_input": a}, None, {}))  # noqa: E731
        check(deny("Bash", {"command": "env"}), "the guard denies Bash")
        check(deny("Read", {"file_path": str(config.ROOT / "CLAUDE.md")}), "the guard denies reading outside the workspace")
        check(not deny("Read", {"file_path": str(ws / "EMPLOYEE.md")}), "the guard allows reading the workspace")
        check(deny("Glob", {"pattern": "../../**/*.env"}), "a glob cannot climb out of the workspace")

        FakeSDK.script = [{"calls": [("thread_history", {})], "text": "On it."}]
        d1, _ = store.enqueue("research", "ask", "", payload={"text": "deep prospect 123 Main St"}, mode="agent",
                              source="slack", slack_channel="CDIGGS", slack_thread="1700.30")
        run_id(d1)
        check({"WebSearch", "WebFetch"} <= set(FakeSDK.options[-1].tools), "Diggs is granted web research")

        print("team: ask-back")
        FakeSDK.script = [{"calls": [("ask_operator", {"question": "Which county is 12 Oak St in?"})], "text": ""}]
        w1, _ = store.enqueue("sales", "ask", "", payload={"text": "screen 12 Oak St"}, mode="agent", source="slack",
                              slack_channel="CROMA", slack_thread="1700.40")
        run_id(w1)
        wt = store.task(w1)
        check(wt["status"] == "waiting" and wt["question"].startswith("Which county") and wt["session_id"],
              "asking the operator parks the task with its session")
        check(fake_slack.posts()[-1]["text"].startswith("Which county") and fake_slack.posts()[-1]["thread_ts"] == "1700.40",
              "the question is posted in the thread")
        r = intake.slack_event({"type": "message", "channel": "CROMA", "ts": "1700.41", "thread_ts": "1700.40",
                                "text": "Blount", "user": "U1"}, roster)
        check(r.get("resumed") == w1 and store.task(w1)["status"] == "answered", "the reply resumes that employee")
        FakeSDK.script = [{"text": "Blount it is. Comps pulled."}]
        run_id(r["task"])
        check(FakeSDK.options[-1].resume == wt["session_id"] and "They said: Blount" in FakeSDK.prompts[-1],
              "the resume continues the same Claude session with the answer")
        again = intake.slack_event({"type": "message", "channel": "CROMA", "ts": "1700.42", "thread_ts": "1700.40",
                                    "text": "thanks", "user": "U1"}, roster)
        check("resumed" not in again and store.task(again["queued"])["agent"] == "sales",
              "a later reply in the thread goes back to whoever spoke there, not to a closed question")

        print("team: failure and caps")
        FakeSDK.script = [{"text": "", "result": {"is_error": True, "subtype": "error_max_budget_usd"}}]
        f1, _ = store.enqueue("ops", "ask", "", payload={"text": "x"}, mode="agent", source="slack",
                              slack_channel="CTEAM", slack_thread="1700.50")
        run_id(f1)
        check(store.task(f1)["status"] == "failed" and ":x:" in fake_slack.posts()[-1]["text"],
              "a turn that ends in an error with nothing done fails loudly in the thread")
        store.add_spend(f1, config.DAILY_CAP_USD, "test: cap reached")
        calls_before = len(FakeSDK.options)
        f2, _ = store.enqueue("ops", "ask", "", payload={"text": "x"}, mode="agent", source="slack",
                              slack_channel="CTEAM", slack_thread="1700.51")
        run_id(f2)
        check(store.task(f2)["status"] == "failed" and len(FakeSDK.options) == calls_before,
              "with the day's cap spent no turn runs at all")
        config.USE_LLM = False
        f3, _ = store.enqueue("ops", "ask", "", payload={"text": "x"}, mode="agent", source="cli")
        run_id(f3)
        check(store.task(f3)["status"] == "done" and "switched off" in store.result_of(store.task(f3))["text"],
              "with Claude off an employee answers honestly instead of pretending")

        print("team: slack events (brain)")
        from fastapi.testclient import TestClient
        from . import brain
        c = TestClient(brain.app)

        def send(payload: dict, secret="sign-secret", headers=None):
            raw = json.dumps(payload).encode()
            return c.post("/slack/events", content=raw, headers={**_sign(secret, raw), **(headers or {})})

        check(send({"type": "event_callback"}, secret="wrong").status_code == 401, "an unsigned event is refused")
        check(send({"type": "url_verification", "challenge": "abc"}).json() == {"challenge": "abc"},
              "Slack's URL check is answered")
        ev = {"type": "message", "channel": "CTEAM", "ts": "1700.60", "text": "Roma, quick screen 123 Main St",
              "user": "U1", "channel_type": "channel"}
        r1 = send({"type": "event_callback", "event_id": "Ev1", "event": ev}).json()
        r2 = send({"type": "event_callback", "event_id": "Ev1", "event": ev}).json()
        r3 = send({"type": "event_callback", "event_id": "Ev2", "event": dict(ev, type="app_mention")}).json()
        qt = store.task(r1["queued"])
        check(qt["agent"] == "sales" and qt["slack_thread"] == "1700.60", "a Slack ask is queued for the named employee")
        check(r2.get("duplicate") and r3.get("duplicate"), "a redelivery and the mention's twin event are dropped")
        check(any(m == "reactions.add" for m, _ in fake_slack.calls), "the operator's message gets an :eyes: reaction")
        check(send({"type": "event_callback", "event_id": "Ev3", "event": ev},
                   headers={"x-slack-retry-num": "1"}).json().get("retry"), "a Slack retry is acknowledged, not re-run")
        check(send({"type": "event_callback", "event_id": "Ev4", "event": dict(ev, ts="1700.61", bot_id="B1")}).json()
              .get("ignored"), "the team's own posts are ignored")
        n_posts = len(fake_slack.posts())
        chat = send({"type": "event_callback", "event_id": "Ev5",
                     "event": dict(ev, ts="1700.62", text="anyone want lunch?")}).json()
        check(chat.get("which") is False and len(fake_slack.posts()) == n_posts, "people chatting in the team channel are left alone")
        men = send({"type": "event_callback", "event_id": "Ev6",
                    "event": dict(ev, type="app_mention", ts="1700.63", text="<@U0BOT> can someone help")}).json()
        check(men.get("which") and "Which of us" in fake_slack.posts()[-1]["text"], "a bare mention gets the roster")
        hi = send({"type": "event_callback", "event_id": "Ev7", "event": dict(ev, ts="1700.64", text="good morning")}).json()
        check(store.task(hi["hello"])["workflow"] == "introduce_self", "good morning in Slack starts the hello chain")
        check(c.post("/team/hello/wrong").status_code == 404 and c.post("/team/hello/test-secret").json()["ok"],
              "the deploy's hello needs the brain secret")
        hl = c.get("/health").json()
        check(hl["slack"] == "bot" and hl["team"]["sales"] == "Roma" and "deep_prospect" in hl["live_workflows"],
              "health reports Slack mode, the team and what is live")
        config.SLACK_SIGNING_SECRET = ""
        check(send({"type": "event_callback"}).status_code == 404, "with no signing secret the Slack door is closed")

        print("team: one channel for the whole team")
        roma, diggs = roster.employees["sales"], roster.employees["research"]
        check(slack_app.home_channel(roma, roster) == roma.slack_channel
              and slack_app.home_channel(None, roster) == roster.team_channel,
              "without the setting each employee keeps its own channel and the team shares one")
        config.SLACK_CHANNEL = "CONE"
        check(slack_app.home_channel(roma, roster) == "CONE" and slack_app.home_channel(None, roster) == "CONE",
              "with one channel set, every post that belongs to no thread goes there")
        t_one, _ = store.enqueue("research", "deep_prospect", "00000000-0000-4000-8000-0000000000aa", source="datasift")
        n0 = len(fake_slack.posts())
        worker._post(store.task(t_one), "file ready", diggs, roster)
        p = fake_slack.posts()[-1]
        check(len(fake_slack.posts()) == n0 + 1 and p["channel"] == "CONE" and p["username"] == "Diggs",
              "a webhook-born task posts in the one channel, still under the employee's own name")
        check(store.task(t_one)["slack_channel"] == "CONE" and store.task(t_one)["slack_thread"],
              "and adopts that message as its thread")
        store.finish(t_one, "done", {})
        fake_slack.outside.add("CONE")
        n0 = len(fake_slack.calls)
        ts = slack_app.post("CONE", "first words")
        check(ts and [m for m, _ in fake_slack.calls[n0:]] == ["chat.postMessage", "conversations.join", "chat.postMessage"],
              "an app that is not in the channel yet joins it and sends once more")
        fake_slack.outside.add("CPRIV")
        fake_slack.private.add("CPRIV")
        check(slack_app.post("CPRIV", "x") == "", "a private channel that refuses the join is a failed post, never a crash")
        check("channels:join" in team.slack_manifest("https://brain.example")["oauth_config"]["scopes"]["bot"],
              "the app manifest asks for the right to join a public channel")
        n0 = len(fake_slack.posts())
        slack_app.notice("a webhook arrived with no record id", roster)
        check(fake_slack.posts()[-1]["channel"] == "CONE" and len(fake_slack.posts()) == n0 + 1,
              "a notice that belongs to no task goes to the one channel too")
        check(c.get("/health").json()["slack_one_channel"] is True, "health says one channel is set, never which")

        print("team: first hello")
        store.kv_set(brain.FIRST_HELLO_KEY, "")
        config.SLACK_BOT_TOKEN = ""
        check(brain.first_hello() == 0, "no Slack app: nothing is said")
        config.SLACK_BOT_TOKEN = "xoxb-test"
        fake_slack.down = True
        check(brain.first_hello() == 0 and not store.kv_get(brain.FIRST_HELLO_KEY),
              "an opening message that did not land is not marked done, so the next boot tries again")
        fake_slack.down = False
        n0 = len(fake_slack.posts())
        tid = brain.first_hello()
        opening = fake_slack.posts()[n0]
        check(tid and store.kv_get(brain.FIRST_HELLO_KEY) and opening["channel"] == "CONE" and not opening.get("thread_ts"),
              "the first boot with the app has the team introduce itself in the one channel")
        check(store.task(tid)["slack_thread"].startswith("1700000000.") and store.task(tid)["source"] == "first-boot",
              "and the introductions thread under that opening message")
        store.finish(tid, "done", {})
        n0 = len(fake_slack.posts())
        check(brain.first_hello() == 0 and len(fake_slack.posts()) == n0, "it is said once, never again")

        class _Stop:                      # a stop flag that is never set, and a wait that costs no time
            waits = 0

            def wait(self, seconds):
                _Stop.waits += 1
                if _Stop.waits == 2:      # the app is let into the channel before the third try
                    fake_slack.down = False
                return False

        store.kv_set(brain.FIRST_HELLO_KEY, "")
        fake_slack.down = True
        used = brain.hello_until_landed(_Stop(), every_s=0, tries=10)
        check(used == 2 and store.kv_get(brain.FIRST_HELLO_KEY) and not brain.hello_pending(),
              "a hello that could not land at boot keeps trying and stops the moment it lands")
        check(brain.hello_until_landed(_Stop(), every_s=0, tries=10) == 0, "once it has landed nothing more is tried")

        print("team: socket mode")
        import json as _json
        heard = []

        def on_body(body, retry):
            heard.append((body.get("event_id"), retry))
            return intake.handle_body(body, retry, lambda: roster)

        acks = []
        order = []
        ask = {"type": "event_callback", "event_id": "EvS1",
               "event": {"type": "message", "channel": "CONE", "ts": "1800.10", "text": "Roma, quick screen 9 Elm St",
                         "user": "U1", "channel_type": "group"}}
        frame = _json.dumps({"envelope_id": "env-1", "type": "events_api", "payload": ask, "retry_attempt": 0})
        what = slack_socket.handle_frame(frame, lambda t: (acks.append(t), order.append("ack")),
                                         lambda b, r: (order.append("handle"), on_body(b, r)))
        check(what == "event" and order == ["ack", "handle"] and _json.loads(acks[0]) == {"envelope_id": "env-1"},
              "an event over the socket is acknowledged first, then handled")
        qt = store.on_slack_thread("CONE", "1800.10")
        check(len(qt) == 1 and qt[0]["agent"] == "sales" and store.kv_get(intake.HEARD_KEY),
              "it is queued for the employee it names, exactly as an event over the Request URL is")
        slack_socket.handle_frame(frame, acks.append, on_body)
        check(len(store.on_slack_thread("CONE", "1800.10")) == 1 and len(acks) == 2,
              "the same event delivered again is acknowledged and not queued twice")
        check(slack_socket.handle_frame('{"type":"hello","num_connections":1}', acks.append, on_body) == "hello"
              and slack_socket.handle_frame('{"type":"disconnect","reason":"refresh_requested"}', acks.append, on_body)
              == "disconnect:refresh_requested", "hello and disconnect frames are read for what they are")
        n_acks, n_heard = len(acks), len(heard)
        check(slack_socket.handle_frame('{"envelope_id":"env-9","type":"slash_commands","payload":{}}', acks.append, on_body)
              == "other" and len(acks) == n_acks + 1 and len(heard) == n_heard,
              "an envelope that is not an event is acknowledged and nothing is run")
        check(slack_socket.handle_frame("not json", acks.append, on_body) == "bad" and
              slack_socket.handle_frame("[1]", acks.append, on_body) == "bad", "a frame that is not an envelope is dropped")

        class FakeConn:
            def __init__(self, frames):
                self.frames, self.sent, self.closed = list(frames), [], False

            def recv(self, timeout=None):
                if not self.frames:
                    raise ConnectionError("closed by the far end")
                f = self.frames.pop(0)
                if f is TimeoutError:
                    raise TimeoutError()
                return f

            def send(self, text):
                self.sent.append(text)

            def close(self):
                self.closed = True

        class Stop:
            def __init__(self):
                self.flag, self.pauses = False, []

            def is_set(self):
                return self.flag

            def wait(self, seconds):
                self.pauses.append(seconds)
                self.flag = True          # the first pause is where the brain is told to stop
                return True

        ask2 = dict(ask, event_id="EvS2", event=dict(ask["event"], ts="1800.20", text="Diggs, who owns 9 Elm St"))
        conns = [FakeConn(['{"type":"hello"}', TimeoutError,
                           _json.dumps({"envelope_id": "env-2", "type": "events_api", "payload": ask2}),
                           '{"type":"disconnect","reason":"refresh_requested"}']),
                 FakeConn(['{"type":"hello"}'])]
        urls = []
        config.SLACK_APP_TOKEN = "xapp-test"
        slack_socket.STATE.update(state="off", since="", error="", events=0, connects=0)
        stop = Stop()
        slack_socket.run(stop, on_body, connect=lambda url: (urls.append(url), conns[len(urls) - 1])[1], sleep=stop.wait)
        check(len(urls) == 2 and urls[0] != urls[1] and all(c.closed for c in conns),
              "when Slack asks for a fresh connection one is opened at once, on a new address, and the old one is closed")
        check(_json.loads(conns[0].sent[0]) == {"envelope_id": "env-2"} and store.on_slack_thread("CONE", "1800.20")[0]["agent"] == "research",
              "an event read off a live connection is acknowledged and queued")
        check(stop.pauses == [slack_socket.PAUSE_MIN_S] and slack_socket.status()["state"] == "off",
              "a connection that drops is retried after a pause, and a stopped brain leaves the loop")
        fake_slack.down = True
        stop2 = Stop()
        slack_socket.run(stop2, on_body, connect=lambda url: check(False, "no connection without an address"), sleep=stop2.wait)
        fake_slack.down = False
        check(stop2.pauses == [slack_socket.PAUSE_MIN_S], "Slack refusing the connection is paused on, never spun on")
        config.SLACK_APP_TOKEN = ""
        check(slack_socket.open_url() == ("", "no app-level token"), "without an app-level token nothing is opened")

        print("team: can the app hear")
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        t_hello = _dt(2026, 1, 5, 15, 0, 0, tzinfo=_tz.utc)
        store.kv_set(brain.DEAF_KEY, "")
        store.kv_set(brain.FIRST_HELLO_KEY, "")
        check(brain.hearing_notice(t_hello) == "", "before the team has said hello there is nothing to check")
        store.kv_set(brain.FIRST_HELLO_KEY, t_hello.isoformat(timespec="seconds"))
        store.kv_set(intake.HEARD_KEY, (t_hello - _td(seconds=30)).isoformat(timespec="seconds"))
        check(brain.hearing_notice(t_hello + _td(seconds=60)) == "", "one minute after the hello is too early to tell")
        config.SLACK_APP_TOKEN = "xapp-test"
        n0 = len(fake_slack.posts())
        said = brain.hearing_notice(t_hello + _td(seconds=200))
        check("Socket Mode" in said and len(fake_slack.posts()) == n0 + 1 and fake_slack.posts()[-1]["channel"] == "CONE",
              "nothing heard three minutes after the hello: the channel is told once, with the setting that fixes it")
        check(brain.hearing_notice(t_hello + _td(seconds=400)) == "" and len(fake_slack.posts()) == n0 + 1,
              "it is said once")
        store.kv_set(intake.HEARD_KEY, (t_hello + _td(seconds=500)).isoformat(timespec="seconds"))
        check(brain.hearing_notice(t_hello + _td(seconds=510)) == "heard" and len(fake_slack.posts()) == n0 + 2
              and "I can hear this channel now" in fake_slack.posts()[-1]["text"],
              "once the setting is fixed and the first event arrives, the channel is told it can be heard")
        check(brain.hearing_notice(t_hello + _td(seconds=600)) == "" and len(fake_slack.posts()) == n0 + 2,
              "and that is said once as well")
        store.kv_set(intake.HEARD_KEY, (t_hello - _td(seconds=30)).isoformat(timespec="seconds"))
        store.kv_set(brain.DEAF_KEY, "")
        config.SLACK_APP_TOKEN, config.SLACK_SIGNING_SECRET = "", "sign-secret"
        check("Request URL" in brain.hearing_notice(t_hello + _td(seconds=200)),
              "with the Request URL as the way in, the fix named is the Request URL")
        store.kv_set(brain.DEAF_KEY, "")
        store.kv_set(intake.HEARD_KEY, (t_hello + _td(seconds=4)).isoformat(timespec="seconds"))
        n0 = len(fake_slack.posts())
        check(brain.hearing_notice(t_hello + _td(seconds=200)) == "heard" and len(fake_slack.posts()) == n0
              and store.kv_get(brain.DEAF_KEY).startswith("heard"),
              "an event after the hello means the app can hear, and nothing is posted")

        class Tick:                       # a stop flag never set; the third look is when Slack starts sending
            n = 0

            def wait(self, seconds):
                Tick.n += 1
                if Tick.n == 3:
                    store.kv_set(intake.HEARD_KEY, store.now())
                return False

        store.kv_set(brain.DEAF_KEY, "notice earlier")
        store.kv_set(brain.FIRST_HELLO_KEY, (_dt.now(_tz.utc) - _td(hours=1)).isoformat(timespec="seconds"))
        store.kv_set(intake.HEARD_KEY, "")
        n0 = len(fake_slack.posts())
        check(brain.slack_watch(Tick(), every_s=0, checks=10) == "heard" and Tick.n == 3 and len(fake_slack.posts()) == n0 + 1,
              "the watch waits through a deaf spell and confirms in the channel the moment an event arrives")
        store.kv_set(brain.DEAF_KEY, "")
        store.kv_set(brain.FIRST_HELLO_KEY, "")
        config.SLACK_SIGNING_SECRET = ""
    finally:
        config.SLACK_CHANNEL = config.SLACK_APP_TOKEN = ""
        slack_socket._http = door_shut
        worker.HANDLERS["deep_prospect"] = real_dp
        runner._SDK = None
        tools.CRM = None
        slack_app._http = door_shut
        config.SLACK_BOT_TOKEN = config.SLACK_SIGNING_SECRET = ""
