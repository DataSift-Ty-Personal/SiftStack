"""Tools that let an employee work a skill in Slack the way Claude Code works it.

Ty, 2026-11-17: "it's critical to include the exact skills workflows through all of this. We want the
slack workflows to mirror what someone would be doing in Claude code." In Claude Code a skill runs as:
load SKILL.md, follow its steps in order, gather its data, build the JSON it describes, run
`python scripts/<generator>.py out.xlsx data.json`, present its summary. An employee does the same:

  * the Skill tool loads the skill (its whole folder, scripts and data included, is in the workspace
    under .claude/skills/<name>/, copied by team.materialize);
  * comps_data is the data step of the comping and rehab skills: SiftMap's deed comps inside the area
    and the engine's own value beside them (quick_screen.dataset), written where run_python reads;
  * run_python runs the skill's own generator, passed from .claude/skills/<skill>/scripts/, on the JSON
    the employee's code writes, exactly as Claude Code runs it in a terminal;
  * publish_sheet turns the workbook into a Google Sheet in the operator's Drive (a "SiftStack Deals"
    folder) and gives back its link; until Google is connected it says so and the workbook's own link
    from run_python stands in.

Google: the KPI tracker's sign-in (kpisinks.credential: a service account or the operator's OAuth
refresh token, scopes spreadsheets + drive.file). An .xlsx uploaded with the Google Sheets mime type is
converted by Drive on the way in; drive.file only sees what this app made, which is all it needs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from pathlib import Path

from . import config, store

log = logging.getLogger(__name__)

COMP_SKILLS = ("real-estate-comping", "rehab-estimator", "comp-package")
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_KEY = "gdrive:deals_folder"
SOURCE_MIME = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".csv": "text/csv"}

# Tests swap this for a fake Drive. (method, url, headers, body bytes) -> (status, parsed JSON)
HTTP = None


def _http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict]:
    if HTTP is not None:
        return HTTP(method, url, headers, body)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            data = {}
        return exc.code, data


def _err(status: int, data: dict) -> str:
    e = (data or {}).get("error")
    msg = e.get("message") if isinstance(e, dict) else (data or {}).get("error_description") or e
    return "HTTP %s: %s" % (status, str(msg or "no message")[:200])


def folder_name() -> str:
    return (os.environ.get("AGENTS_DEALS_SHEET_FOLDER") or "SiftStack Deals").strip()


def deals_folder(token: str) -> str:
    """The id of the Drive folder deal sheets go in, made once by this app and remembered."""
    have = store.kv_get(FOLDER_KEY) or ""
    name = folder_name()
    if have.startswith(name + "|"):
        return have.split("|", 1)[1]
    auth = {"Authorization": "Bearer %s" % token}
    q = "name = '%s' and mimeType = '%s' and trashed = false" % (name.replace("'", "\\'"), FOLDER_MIME)
    status, data = _http("GET", FILES_URL + "?" + urllib.parse.urlencode({"q": q, "fields": "files(id,name)"}), auth, None)
    if status == 200 and (data.get("files") or []):
        fid = data["files"][0]["id"]
    else:
        status, data = _http("POST", FILES_URL + "?fields=id", dict(auth, **{"Content-Type": "application/json"}),
                             json.dumps({"name": name, "mimeType": FOLDER_MIME}).encode())
        if status not in (200, 201) or not data.get("id"):
            raise RuntimeError("the Drive folder %r could not be made (%s)" % (name, _err(status, data)))
        fid = data["id"]
    store.kv_set(FOLDER_KEY, "%s|%s" % (name, fid))
    return fid


DEALS_OAUTH = ("AGENTS_DEALS_GOOGLE_CLIENT_ID", "AGENTS_DEALS_GOOGLE_CLIENT_SECRET", "AGENTS_DEALS_GOOGLE_REFRESH_TOKEN")


def credential() -> tuple[dict | None, str]:
    """The Google sign-in deal sheets use. Its own three settings first, so connecting deal sheets does
    not also switch on the KPI tracker's Sheet (which writes whenever the shared AGENTS_GOOGLE_* sign-in
    exists); the shared sign-in when those are unset. A distinct `who`, so the token cache never hands
    one sign-in's token to the other."""
    vals = {k: (os.environ.get(k) or "").strip() for k in DEALS_OAUTH}
    if all(vals.values()):
        return {"mode": "oauth", "client_id": vals[DEALS_OAUTH[0]], "client_secret": vals[DEALS_OAUTH[1]],
                "refresh_token": vals[DEALS_OAUTH[2]], "who": "deals:" + vals[DEALS_OAUTH[0]]}, ""
    if any(vals.values()):
        missing = [k for k, v in vals.items() if not v]
        return None, "the deal-sheet Google sign-in is incomplete: %s not set" % ", ".join(missing)
    from .autopilot import kpisinks
    return kpisinks.credential()


def share_list() -> list[tuple[str, str]]:
    """AGENTS_DEALS_SHEET_SHARE: comma-separated emails, each read-only unless written email=writer."""
    out = []
    for part in re.split(r"[,;\s]+", os.environ.get("AGENTS_DEALS_SHEET_SHARE") or ""):
        email, _, role = part.partition("=")
        role = role.strip().lower() or "reader"
        if "@" in email:
            out.append((email.strip().lower(), role if role in ("reader", "commenter", "writer") else "reader"))
    return out


def publish(task: dict, employee_id: str, given: str, title: str = "") -> dict:
    """One workbook (or CSV) from this thread, uploaded to the deals folder as a Google Sheet.
    {"published": True, "link", "title"} or {"published": False, "why"}. Never raises."""
    from . import sandbox
    from .autopilot import kpisinks
    try:
        p = sandbox.resolve_input(str(given or ""), task, employee_id)
    except sandbox.Refused as exc:
        return {"published": False, "why": str(exc)}
    kind = p.suffix.lower()
    if kind not in SOURCE_MIME:
        return {"published": False, "why": "only an .xlsx or a .csv becomes a Google Sheet; %s is neither" % p.name}
    cred, problem = credential()
    if not cred:
        return {"published": False, "why": problem or (
            "Google is not connected on this box yet, so the workbook stays a file: share run_python's link for it "
            "(the operator connects Google once, then every workbook becomes a Sheet)")}
    name = (title or p.stem).strip()[:120] or p.stem
    try:
        token = kpisinks.access_token(cred)
        parent = deals_folder(token)
        boundary = "skill-%s" % uuidlib.uuid4().hex
        meta = json.dumps({"name": name, "mimeType": SHEET_MIME, "parents": [parent]}).encode()
        body = (b"--" + boundary.encode() + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + meta
                + b"\r\n--" + boundary.encode() + b"\r\nContent-Type: " + SOURCE_MIME[kind].encode() + b"\r\n\r\n"
                + p.read_bytes() + b"\r\n--" + boundary.encode() + b"--\r\n")
        status, data = _http("POST", UPLOAD_URL, {"Authorization": "Bearer %s" % token,
                                                  "Content-Type": "multipart/related; boundary=%s" % boundary}, body)
        if status not in (200, 201) or not data.get("id"):
            return {"published": False, "why": "Google refused the upload (%s)" % _err(status, data)}
        shared = []
        for email, role in share_list():
            s2, d2 = _http("POST", "%s/%s/permissions?sendNotificationEmail=false" % (FILES_URL, data["id"]),
                           {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
                           json.dumps({"type": "user", "role": role, "emailAddress": email}).encode())
            shared.append(email if s2 in (200, 201) else "%s (not shared: %s)" % (email, _err(s2, d2)))
        link = data.get("webViewLink") or "https://docs.google.com/spreadsheets/d/%s/edit" % data["id"]
        return {"published": True, "link": link, "title": name, "folder": folder_name(), "shared_with": shared}
    except Exception as exc:  # noqa: BLE001  (Google down or refusing: said, never raised into the turn)
        log.warning("publish_sheet failed: %s", exc)
        return {"published": False, "why": "the Google upload failed: %s" % str(exc)[:200]}


# ------------------------------------------------------------------------------------- the tools
def comps_data_tool(ctx):
    from .tools import Tool, _obj, _text
    from .workflows import quick_screen as qs

    async def handler(args: dict) -> dict:
        try:
            res = await asyncio.to_thread(qs.dataset, ctx.task, dict(args or {}))
        except Exception as exc:  # noqa: BLE001  (a data step fault must never crash the turn)
            log.exception("comps_data failed")
            return _text("The comp data step failed: %s: %s" % (type(exc).__name__, str(exc)[:300]), error=True)
        if not res.get("ok"):
            return _text(res.get("why") or "No comp data came back.", error=True)
        return _text(json.dumps(res, default=str, separators=(",", ":")))

    return Tool("comps_data",
                "The data step of the real-estate-comping and rehab-estimator skills, run inside your turn: the "
                "subject house off its DataSift record (plus card facts from the notes), every SiftMap deed sale "
                "inside the boundary or radius (price, date, size, beds, baths, year, $/sf, miles, the engine's "
                "condition bucket, the deed's buyer and cash flag), and the engine's own value (tight ARV and its "
                "basis) for the side-by-side. Free; deed reads are capped. It also writes comps_<street>.csv and "
                "subject_<street>.json for run_python (the paths are in its answer under files).",
                _obj({"address": {"type": "string", "description": "The house: its street line or record uuid"},
                      "boundary": {"type": "string", "description": "Optional: a box, corners, lat,lon points, KML or "
                                                                    "GeoJSON text, or 'radius 0.75'"},
                      "streets": {"type": "string", "description": "Optional: streets to keep, separated by |"},
                      "notes": {"type": "string", "description": "Optional: card facts and context as the operator "
                                                                 "wrote them (3/1, 1,400 sqft, built 1950)"},
                      "deed_reads": {"type": "number", "description": "Optional: how many deeds to read (default 30, "
                                                                      "at most 60)"}}, ["address"]),
                handler, read_only=True)


def publish_sheet_tool(ctx):
    from .tools import Tool, _obj, _text

    async def handler(args: dict) -> dict:
        a = dict(args or {})
        res = await asyncio.to_thread(publish, ctx.task, ctx.employee.id, a.get("file"), a.get("title") or "")
        return _text(json.dumps(res, separators=(",", ":")), error=not res.get("published") and not res.get("why"))

    return Tool("publish_sheet",
                "Turn a workbook this thread made (a run_python output, e.g. task:123/Comps.xlsx or its file name) "
                "into a Google Sheet in the operator's Drive and get its link. Use it as a skill's last step, then "
                "put the link in your reply. If Google is not connected it says so: share run_python's link instead.",
                _obj({"file": {"type": "string", "description": "The workbook: a file name from run_python's files, "
                                                                "or task:<id>/<name>.xlsx"},
                      "title": {"type": "string", "description": "The Sheet's title, e.g. '123 Example St comps'"}},
                     ["file"]),
                handler)


def skill_report_tool(ctx):
    from .tools import Tool, _obj, _text
    from . import skillrun

    async def handler(args: dict) -> dict:
        try:
            res = await asyncio.to_thread(skillrun.run, ctx.task, ctx.employee.id, dict(args or {}))
        except Exception as exc:  # noqa: BLE001  (a fault here must never crash the turn)
            log.exception("skill_report failed")
            return _text("skill_report failed: %s: %s. Work the skill by hand." % (type(exc).__name__, str(exc)[:300]),
                         error=True)
        return _text(json.dumps(res, default=str, separators=(",", ":")), error=not res.get("ok"))

    return Tool("skill_report",
                "The thinking step of the real-estate-comping or rehab-estimator skill, run on the team's file-skill "
                "model (src/model_registry.py), then the skill's OWN generator builds its workbook. Comps: run "
                "comps_data first; this reads its comps CSV and subject JSON from this turn. Rehab: it reads the "
                "walkthrough photos saved for this thread plus your notes (give it the ARV and purchase price when "
                "known). Every answer is checked (comps exist with the same price and size, bucket medians, ARV "
                "math; rehab lines and totals add up) and retried, then sent once to a second model, before it "
                "comes back. Takes one to four minutes. Returns the summary lines, the figures, the workbook and "
                "its link; publish it with publish_sheet. If it answers ok=false, work the skill by hand.",
                _obj({"skill": {"type": "string", "enum": sorted(skillrun.SKILLS),
                                "description": "real-estate-comping or rehab-estimator"},
                      "address": {"type": "string", "description": "The house's street line (names the workbook)"},
                      "notes": {"type": "string", "description": "The operator's card facts and walkthrough notes, "
                                                                 "as written"},
                      "files": {"type": "array", "items": {"type": "string"},
                                "description": "Optional: the comps CSV and subject JSON (exports/...) if not this "
                                               "turn's newest, or other files to read"},
                      "photos": {"type": "string", "description": "Rehab: 'all' (default) uses every photo saved for "
                                                                  "this thread, 'none' uses none"},
                      "arv": {"type": "number", "description": "Rehab: the ARV for the deal analysis"},
                      "purchase_price": {"type": "number", "description": "Rehab: the price we would pay"}},
                     ["skill"]),
                handler)


def skill_tools(ctx) -> list:
    tools = [publish_sheet_tool(ctx)]
    if any(s in COMP_SKILLS for s in (getattr(ctx.employee, "skills", None) or [])):
        tools[0:0] = [comps_data_tool(ctx), skill_report_tool(ctx)]
    return tools
