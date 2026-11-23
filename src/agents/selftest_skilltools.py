"""Selftest: a skill worked in Slack the way Claude Code works it (skilltools.py, 2026-11-17).

Zero network: Google is a fake (skilltools.HTTP) behind a fake sign-in. The comp data step itself is
tested in selftest_wf_deals beside the comps screen, on that section's fake DataSift and SiftMap."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace


def run(check, tmp: Path) -> None:
    from . import config, sandbox, skilltools, store, team
    from .autopilot import kpisinks

    roster = team.load()
    md = team.render_team_md(roster)
    check("## Skills: work them the way Claude Code does" in md and "for reference" not in md
          and "in/generate_excel_report.py" in md and "publish_sheet" in md,
          "every employee is told to run a matching skill end to end, its own generator included, and to publish it")
    check("comps_data" in (config.TEAM_DIR / "sales" / "CLAUDE.md").read_text(encoding="utf-8")
          and "side by side" in (config.TEAM_DIR / "sales" / "CLAUDE.md").read_text(encoding="utf-8"),
          "Roma's brief sends comps and rehab asks through the skills, with both ARVs side by side")

    def names(emp_id):
        emp = roster.employees[emp_id]
        return [t.name for t in skilltools.skill_tools(SimpleNamespace(employee=emp, task={"id": 1}))]
    check(names("sales") == ["comps_data", "skill_report", "publish_sheet"] and names("ops") == ["publish_sheet"],
          "the comps data step goes to whoever carries the comping skills; publishing goes to everyone")

    tid, _ = store.enqueue("sales", "ask", "", subject="comps", source="slack", force=True)
    task = store.task(tid)
    files = sandbox.run_dir_for(task) / "files"
    files.mkdir(parents=True, exist_ok=True)
    (files / "Comps.xlsx").write_bytes(b"PK\x03\x04 a workbook")
    (files / "notes.txt").write_text("x", encoding="utf-8")

    keep_env = {k: os.environ.pop(k, None) for k in (kpisinks.SA_ENV, *kpisinks.OAUTH_ENV, *skilltools.DEALS_OAUTH,
                                                     "AGENTS_DEALS_SHEET_SHARE")}
    keep = (kpisinks.access_token, skilltools.HTTP)
    try:
        r = skilltools.publish(task, "sales", "Comps.xlsx", "123 Example St comps")
        check(not r["published"] and "not connected" in r["why"],
              "with Google not connected the workbook stays a file, and the tool says to share its link")
        r = skilltools.publish(task, "sales", "notes.txt")
        check(not r["published"] and "neither" in r["why"], "only a workbook or a CSV becomes a Sheet")

        os.environ.update({kpisinks.OAUTH_ENV[0]: "cid", kpisinks.OAUTH_ENV[1]: "secret", kpisinks.OAUTH_ENV[2]: "rt",
                           "AGENTS_DEALS_SHEET_SHARE": "teammate@example.com"})
        kpisinks.access_token = lambda cred: "tok"
        calls = []

        def drive(method, url, headers, body):
            calls.append((method, url, headers, body))
            if method == "GET":
                return 200, {"files": []}
            if "/upload/" in url:
                return 200, {"id": "S1", "name": "x", "webViewLink": "https://docs.google.com/spreadsheets/d/S1/edit"}
            if "/permissions" in url:
                return 200, {"id": "p1"}
            return 200, {"id": "F1"}
        skilltools.HTTP = drive
        store.kv_set(skilltools.FOLDER_KEY, "")
        r = skilltools.publish(task, "sales", "Comps.xlsx", "123 Example St comps")
        up = [c for c in calls if "/upload/" in c[1]]
        check(r["published"] and r["link"].endswith("/S1/edit") and r["shared_with"] == ["teammate@example.com"],
              "connected, the workbook becomes a Google Sheet, its link comes back, and it is shared as set")
        check(len(up) == 1 and skilltools.SHEET_MIME.encode() in up[0][3] and b'"F1"' in up[0][3]
              and b"a workbook" in up[0][3] and up[0][2]["Authorization"] == "Bearer tok",
              "the upload asks Drive to convert it to a Sheet, in the deals folder, with the workbook's own bytes")
        folders = [c for c in calls if c[0] == "POST" and c[1].startswith(skilltools.FILES_URL + "?")]
        calls.clear()
        skilltools.publish(task, "sales", "Comps.xlsx", "again")
        check(len(folders) == 1 and not [c for c in calls if c[0] == "GET" or c[1].startswith(skilltools.FILES_URL + "?")],
              "the deals folder is made once and remembered")
        skilltools.HTTP = lambda m, u, h, b: (403, {"error": {"message": "insufficient scope"}})
        store.kv_set(skilltools.FOLDER_KEY, "")
        r = skilltools.publish(task, "sales", "Comps.xlsx")
        check(not r["published"] and "insufficient scope" in r["why"], "a refusal from Google is said, never raised")
        os.environ.update({skilltools.DEALS_OAUTH[0]: "dcid", skilltools.DEALS_OAUTH[1]: "dsecret",
                           skilltools.DEALS_OAUTH[2]: "drt"})
        cred, _ = skilltools.credential()
        check(cred["refresh_token"] == "drt" and cred["who"] == "deals:dcid",
              "deal sheets use their own sign-in first, so connecting them leaves the KPI tracker's Sheet off")
        os.environ.pop(skilltools.DEALS_OAUTH[2])
        cred, why = skilltools.credential()
        check(cred is None and "incomplete" in why, "a half-set deal-sheet sign-in is said, not half used")
    finally:
        kpisinks.access_token, skilltools.HTTP = keep
        for k, v in keep_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
