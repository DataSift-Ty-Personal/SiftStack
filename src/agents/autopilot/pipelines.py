"""Three pipelines for the account (the owner's ask, 2026-11-14): the change list that splits the
Priority track back into its two halves and makes the First to Market board.

    County     every county record (tag FTM, the Obituary list) on one SiftLine board, First to
               Market, worked through five full attempts in a row (ftmpipeline.py)
    Top 1,000  the Priority presets, narrowed to the tag Top 1000, with the flow they always had
    Agency     unchanged

The two-track restructure of 11-13 (restructure.py) anchored every Priority preset on "FTM or Top
1000" and archived the first-to-market folders. This tool takes the county half back out:

  (a) every "Priority - ..." preset is narrowed to the Top 1,000. Its anchor any_tags [FTM, Top 1000]
      becomes [Top 1000] and every other key stays exactly as it was. By default the narrowed preset
      also skips the FTM tag and the Obituary list (TOP_SKIPS_COUNTY): the Top 1,000 is ranked
      without county records, but a Top 1,000 house that later gets a county notice would sit in
      both pipelines until the monthly refresh. Their folders are renamed to say so; the preset
      titles stay, because every stage reads its presets by title.
  (b) the archived "05. FTM - CALL" and "06. FTM - MAIL" folders get their names back. The archive
      only renamed the folders, so their presets are the county presets as the 11-13 change list
      proved them, anchored on FTM alone. Restoring keeps those titles and filters; new presets
      would need new titles (a title is unique in an account) and would start unproven. Obituary
      records are not in them, because a preset ANDs its keys and "tag FTM or list Obituary" cannot
      be said: they reach the board through the preset Obituary - All, and the board is where the
      two meet.
  (c) the board First to Market and its seven columns (Research, Day 1 to Day 5, Exhausted). The
      board is made on the key (a documented route). The published API has no column route, so the
      columns are made with this workstation's own login (siftline_columns.py), the way the account
      blueprint made columns on our own accounts. Every column is read back, and the run prints the
      column ids for the stage's block (ftm_pipeline.column_ids): no documented route lists them.

`plan` only reads. `apply` needs --commit AND PIPELINES_ALLOW=1. It writes a backup of every folder,
preset and board first, checks every preset still reads as the plan saw it, reads back each change,
stops at the first difference, and logs each write with what it replaced. A second run changes
nothing. `undo` replays the log backwards: a preset gets its old filter back and a folder its old
name. Deleting a SiftLine board is app-only, so undo leaves the board and its columns and says so.

    python src/agents/autopilot/pipelines.py plan
    python src/agents/autopilot/pipelines.py apply --plan output/autopilot/pipelines/plan_<day>.json [--commit]
    python src/agents/autopilot/pipelines.py undo --log output/autopilot/pipelines/applied_<day>.jsonl [--commit]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
ROOT = SRC.parent
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would be first on the path and its modules (mail, policy,
    # windows) would shadow anything else of the same name. Only src belongs there.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.autopilot import preset_patch as pp        # noqa: E402
from agents.autopilot import restructure as rs         # noqa: E402
from agents.autopilot.crm import rows_of               # noqa: E402
from agents.autopilot.ftmpipeline import BOARD, COLUMNS  # noqa: E402

OUT_DIR = ROOT / "output" / "autopilot" / "pipelines"
BACKUP_DIR = ROOT / "output" / "backups"
ALLOW_ENV = "PIPELINES_ALLOW"
Refused = pp.Refused

# ==================================================================================================
# THE TABLE. Every name this tool relies on is in this block. Edit it, run `plan` again, read it.
# ==================================================================================================

FTM_TAG = rs.FTM_TAG
TOP_TAG = rs.TOP_TAG
OBITUARY_LIST = "Obituary"
PRIORITY_PREFIX = rs.TARGET_PREFIX                 # "Priority - "
# The narrowed Top 1,000 presets also skip the county markers, so no county record sits in both
# pipelines. False keeps the narrowing to the anchor alone, every other key exactly as it is.
TOP_SKIPS_COUNTY = True
# The Priority folders, renamed to say they hold the Top 1,000 only.
RENAME = {rs.TEXT_FOLDER: "01. TOP 1000 - TEXT", rs.CALL_FOLDER: "02. TOP 1000 - CALL",
          rs.MAIL_FOLDER: "03. TOP 1000 - MAIL"}
# The county folders, back from "99. Archive - <name>".
RESTORE = [rs.FTM_CALL_FOLDER, rs.FTM_MAIL_FOLDER]
ARCHIVE_PREFIX = rs.ARCHIVE_PREFIX
# The board's second door: obituary records reach it through this preset (checked to exist).
OBITUARY_PRESETS = ["Obituary - All"]

# ==================================================================================================
# End of the table.
# ==================================================================================================

R_BOARDS = "/api/internal/siftline/board/?limit=100&offset=0"
R_BOARD_NEW = "/api/internal/siftline/board/"
R_BOARD_ONE = "/api/internal/siftline/board/%s/"


# ------------------------------------------------------------------------------------------ names
def _must(p: dict) -> dict:
    return ((p or {}).get("filters") or {}).get("must") or {}


def board_row(boards, title: str = BOARD):
    """The board titled `title`, matched the way the app shows titles (case and outer spaces do
    not matter), or None."""
    want = str(title or "").strip().lower()
    return next((b for b in boards or [] if str(b.get("title") or "").strip().lower() == want and b.get("uuid")), None)


def columns_of_detail(detail) -> list[dict]:
    """The columns a board detail carries, when it carries any (the reference does not promise it)."""
    cols = (detail or {}).get("columns") if isinstance(detail, dict) else None
    if isinstance(cols, dict):
        cols = [dict(v, uuid=k) for k, v in cols.items() if isinstance(v, dict)]
    return [{"uuid": str(c.get("uuid")), "title": str(c.get("title") or ""), "order": c.get("order")}
            for c in cols or [] if isinstance(c, dict) and c.get("uuid")]


def narrow_must(must: dict, ftm: str, top: str, obit: str) -> tuple[dict | None, str, list]:
    """(the filter read for the Top 1,000 alone, why it cannot be when it cannot, what changed). The
    FTM tag leaves the anchor and nothing else moves; with TOP_SKIPS_COUNTY the FTM tag and the
    Obituary list are added to the filter's exclusions, after what is already there."""
    m = copy.deepcopy(must or {})
    tags = m.get("any_tags")
    if not isinstance(tags, list) or top not in tags:
        if isinstance(tags, list) and ftm in tags:
            return None, "it reads FTM and not Top 1000, so it is not one of the Priority clones", []
        return None, "it does not read the Top 1000 tag", []
    says = []
    if ftm in tags:
        m["any_tags"] = [t for t in tags if t != ftm]
        says.append("The anchor FTM or Top 1000 becomes Top 1000")
    if TOP_SKIPS_COUNTY:
        mn = m.get("must_not") if isinstance(m.get("must_not"), dict) else {}
        if ftm and ftm not in (mn.get("any_tags") or []):
            rs._skip(m, "any_tags", [ftm])
            says.append("Skips the FTM tag, so a Top 1,000 house that gets a county notice leaves at once")
        if obit and obit not in (mn.get("any_lists") or []):
            rs._skip(m, "any_lists", [obit])
            says.append("Skips the Obituary list, which the county board works")
    return m, "", says


def county_preset(must: dict, ftm: str, top: str) -> bool:
    """A county preset reads the FTM tag and not the Top 1000 tag."""
    tags = list(must.get("any_tags") or []) + list(must.get("all_tags") or [])
    return bool(ftm) and ftm in tags and top not in tags


# --------------------------------------------------------------------------------------------- plan
def build_plan(reader, *, day: str = "", columns_reader=None) -> dict:
    """The change list, read from the account through `reader` (preset_patch.Reader: it cannot
    write). `columns_reader` is a client on this workstation's login, asked only when the board
    already exists, for its columns; without it the plan says it could not list them."""
    today = reader.today
    acct = rs.read_account(reader)
    boards = reader.rows(R_BOARDS, optional=True)
    folders, presets = acct["folders"], acct["presets"]
    blocking, flags, notes = [], [], []
    ftm_row, _ = rs._exact(acct["tags"], FTM_TAG)
    top_row, _ = rs._exact(acct["tags"], TOP_TAG)
    obit_row, obit_twins = rs._exact(acct["lists"], OBITUARY_LIST)
    obit_row = obit_row or (obit_twins[0] if len(obit_twins) == 1 else None)
    ftm, top, obit = (ftm_row or {}).get("uuid") or "", (top_row or {}).get("uuid") or "", (obit_row or {}).get("uuid") or ""
    if not ftm:
        blocking.append("The tag FTM does not exist, so there is no county anchor to restore.")
    if not top:
        blocking.append("The tag Top 1000 does not exist, so the Priority presets cannot be read for the Top 1,000.")
    if TOP_SKIPS_COUNTY and not obit:
        flags.append("There is no Obituary list, so the narrowed presets skip the FTM tag only.")

    # ---- (a) the Priority presets, narrowed to the Top 1,000
    narrow = []
    for p in presets:
        if not str(p["title"]).startswith(PRIORITY_PREFIX):
            continue
        before = _must(p)
        after, why, says = narrow_must(before, ftm, top, obit) if (ftm and top) else (None, "the tags are missing", [])
        row = {"title": p["title"], "uuid": p["uuid"], "folder": p["folder"], "before_sha": pp.sha(before),
               "must_before": before, "must_after": after, "says": says, "count_before": None, "count_after": None}
        if after is None:
            row["action"] = "conflict"
            blocking.append("%s cannot be narrowed: %s. Look at it in the app, then run the plan again." % (p["title"], why))
        elif after == before:
            row["action"] = "already"
        else:
            row["action"] = "change"
        narrow.append(row)
    if not narrow:
        blocking.append("No preset titled \"%s...\" was found, so there is nothing to narrow." % PRIORITY_PREFIX)

    # ---- the folders: the Priority ones renamed, the county ones restored
    fplan = []
    for old, new in RENAME.items():
        f, _ = rs._exact(folders, old)
        g, _ = rs._exact(folders, new)
        row = {"kind": "top1000", "title": old, "new_title": new, "uuid": (f or g or {}).get("uuid")}
        if f and g:
            row["action"] = "conflict"
            blocking.append("Both %s and %s exist, so the folder cannot be renamed." % (old, new))
        elif f:
            row["action"] = "rename"
        elif g:
            row["action"] = "already"
        else:
            row["action"] = "missing"
            notes.append("The folder %s was not found; there is nothing to rename." % old)
        fplan.append(row)
    restore = []
    for name in RESTORE:
        f, _ = rs._exact(folders, name)
        a, _ = rs._exact(folders, ARCHIVE_PREFIX + name)
        held_by = f or a
        inside = [q for q in presets if held_by and q["folder_uuid"] == held_by["uuid"]]
        row = {"kind": "county", "title": ARCHIVE_PREFIX + name, "new_title": name, "uuid": (held_by or {}).get("uuid"),
               "presets": [q["title"] for q in inside]}
        if f and a:
            row["action"] = "conflict"
            blocking.append("Both %s and %s exist, so the archived folder cannot take its name back." % (name, row["title"]))
        elif a:
            row["action"] = "rename"
        elif f:
            row["action"] = "already"
        else:
            row["action"] = "missing"
            blocking.append("Neither %s nor %s exists, so the county presets are not in the account." % (name, row["title"]))
        fplan.append(row)
        for q in inside:
            ok = county_preset(_must(q), ftm, top)
            restore.append({"title": q["title"], "folder": name, "uuid": q["uuid"], "county": ok, "count": None,
                            "must": _must(q)})
            if not ok and q["title"].startswith("FTM - "):
                flags.append("%s does not read the FTM tag alone; check it in the app before the county stages read it."
                             % q["title"])

    # ---- (c) the board and its columns
    brow = board_row(boards)
    board = {"title": BOARD, "uuid": (brow or {}).get("uuid") or "", "action": "exists" if brow else "create",
             "columns": [], "listed": ""}
    have: list = []
    if brow:
        try:
            have = columns_of_detail(reader._crm.call(R_BOARD_ONE % brow["uuid"]))
            board["listed"] = "the board detail" if have else ""
        except Exception as exc:  # noqa: BLE001  (the detail is a convenience; the login listing is the fallback)
            notes.append("The board detail could not be read (%s)." % type(exc).__name__)
        if not have and columns_reader is not None:
            try:
                import siftline_columns
                have = siftline_columns.list_columns(columns_reader, brow["uuid"])
                board["listed"] = "this workstation's login"
            except Exception as exc:  # noqa: BLE001
                notes.append("The board's columns could not be listed with this workstation's login (%s: %s)."
                             % (type(exc).__name__, str(exc)[:160]))
        if not board["listed"]:
            flags.append("The board %s exists but its columns could not be listed; apply lists them with this "
                         "workstation's login before it makes any." % BOARD)
    by_title = {c["title"].strip().lower(): c for c in have}
    for i, title in enumerate(COLUMNS):
        c = by_title.get(title.lower())
        board["columns"].append({"title": title, "order": i, "uuid": (c or {}).get("uuid") or "",
                                 "action": "exists" if c else ("unknown" if brow and not board["listed"] else "create")})
    extra = [c["title"] for c in have if c["title"].strip().lower() not in {t.lower() for t in COLUMNS}]
    if extra:
        flags.append("The board has columns the round does not use (%s); they are left alone." % ", ".join(extra))

    # ---- counts (the documented search; an object not made yet holds nobody)
    for row in narrow:
        row["count_before"] = reader.count(row["must_before"])
        if row["must_after"] is not None:
            row["count_after"] = row["count_before"] if row["action"] == "already" else reader.count(row["must_after"])
    for row in restore:
        row["count"] = reader.count(row["must"])
    obit_counts = {}
    for title in OBITUARY_PRESETS:
        p = next((q for q in presets if q["title"] == title), None)
        if p is None:
            blocking.append("The preset %s does not exist, so obituary records have no way onto the board." % title)
        else:
            obit_counts[title] = reader.count(_must(p))
    if top and reader.count({"any_tags": [top]}) == 0:
        notes.append("No record carries the Top 1000 tag yet, so the narrowed presets hold nobody until the Top 1,000 is "
                     "populated (top1000.py).")
    if top and ftm:
        both = reader.count({"all_tags": [ftm, top]})
        if both:
            notes.append("%s record(s) carry both FTM and Top 1000 today. The board takes them (the county lane ranks above); "
                         "the narrowed presets %s." % (pp._n(both), "skip them" if TOP_SKIPS_COUNTY else "still show them"))
    acts = Counter(r["action"] for r in narrow)
    return {
        "tool": "pipelines", "plan_date": day or date.today().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"), "counted_as_of": today.isoformat(),
        "account": rs._account_of(presets),
        "requests": {"made": reader.made, "from_cache": reader.from_cache, "budget": reader.max_requests,
                     "not_counted": reader.skipped, "stopped": reader.stopped, "errors": reader.errors[:20],
                     "notes": sorted(set(reader.notes))[:20]},
        "summary": {"presets_read": len(presets), "folders_read": len(folders), "presets_to_narrow": acts.get("change", 0),
                    "presets_narrowed": acts.get("already", 0), "folders_to_rename": sum(1 for f in fplan if f["action"] == "rename"),
                    "board": board["action"], "columns_to_create": sum(1 for c in board["columns"] if c["action"] == "create")},
        "blocking": blocking, "flags": flags, "notes": notes, "table": {"top_skips_county": TOP_SKIPS_COUNTY},
        "objects": {"ftm": ftm, "top": top, "obituary": obit},
        "narrow": narrow, "folders": fplan, "restore": restore, "obituary_presets": obit_counts, "board": board,
    }


def plan_lines(plan: dict) -> list[str]:
    """Every change the plan makes, one line each, with the records each preset holds."""
    s = plan["summary"]
    out = ["Three pipelines, plan for %s: %d presets in %d folders read, %d requests to the account."
           % (plan["plan_date"], s["presets_read"], s["folders_read"], plan["requests"]["made"])]
    if plan["blocking"]:
        out.append("BLOCKED, apply refuses while any of these stands:")
        out += ["  - %s" % b for b in plan["blocking"]]
    for r in plan["narrow"]:
        verb = {"change": "Narrow", "already": "Already narrowed:", "conflict": "CONFLICT:"}[r["action"]]
        out.append("%s %s: %s now, %s after" % (verb, r["title"], pp._records(r["count_before"]), pp._records(r["count_after"])))
    for f in plan["folders"]:
        if f["action"] == "rename":
            out.append("Rename folder %s to %s%s" % (f["title"], f["new_title"],
                                                     " (%d presets keep their titles)" % len(f.get("presets") or [])
                                                     if f["kind"] == "county" else ""))
        elif f["action"] == "already":
            out.append("Already renamed: %s" % f["new_title"])
        elif f["action"] == "missing":
            out.append("Not found: %s" % f["title"])
        else:
            out.append("CONFLICT on folder %s" % f["title"])
    for r in plan["restore"]:
        out.append("County preset %s: %s" % (r["title"], pp._records(r["count"])))
    for t, n in plan["obituary_presets"].items():
        out.append("Obituary door %s: %s" % (t, pp._records(n)))
    b = plan["board"]
    out.append("%s board %s" % ("Create" if b["action"] == "create" else "Keep", b["title"]))
    for c in b["columns"]:
        out.append("  %s column %s" % ({"create": "Create", "exists": "Keep", "unknown": "Not listed:"}[c["action"]], c["title"]))
    return out


def render_md(plan: dict) -> str:
    s, b = plan["summary"], plan["board"]
    out = []
    w = out.append
    w("# Three pipelines: the change list for %s" % plan["plan_date"])
    w("")
    w("Nothing has been changed in the account. This list was written from a read-only pass on %s (%d requests), "
      "with date windows worked out as of %s." % (plan["generated_at"].replace("T", " at "), plan["requests"]["made"],
                                                  plan["counted_as_of"]))
    w("")
    w("## The short version")
    w("")
    w("- %d Priority presets to narrow to the Top 1,000 (%d already narrowed)." % (s["presets_to_narrow"], s["presets_narrowed"]))
    w("- %d folders to rename: the Priority folders say Top 1000, the county folders come out of the archive."
      % s["folders_to_rename"])
    w("- The board %s: %s, %d columns to create." % (b["title"], "to create" if b["action"] == "create" else "already there",
                                                     s["columns_to_create"]))
    if plan["blocking"]:
        w("")
        w("### Blocked: apply refuses while any of these stands")
        w("")
        for x in plan["blocking"]:
            w("- %s" % x)
    if plan["flags"] or plan["notes"]:
        w("")
        w("### Read before you approve")
        w("")
        for x in plan["flags"] + plan["notes"]:
            w("- %s" % x)
    w("")
    w("## The Top 1,000 presets")
    w("")
    for r in plan["narrow"]:
        w("**%s** (%s): %s now, %s after." % (r["title"], {"change": "narrowed", "already": "already narrowed",
                                                          "conflict": "CONFLICT"}[r["action"]],
                                             pp._records(r["count_before"]), pp._records(r["count_after"])))
        for x in r["says"]:
            w("- %s." % x)
        w("")
    w("## The county presets")
    w("")
    for f in plan["folders"]:
        if f["kind"] == "county":
            w("- %s: %s." % (f["new_title"], {"rename": "comes back from %s" % f["title"], "already": "in place",
                                              "missing": "NOT FOUND", "conflict": "CONFLICT"}[f["action"]]))
    for r in plan["restore"]:
        w("- %s holds %s%s." % (r["title"], pp._records(r["count"]), "" if r["county"] else " (it does not read FTM alone)"))
    for t, n in plan["obituary_presets"].items():
        w("- Obituary records reach the board through %s, which holds %s." % (t, pp._records(n)))
    w("")
    w("## The board")
    w("")
    w("%s %s, columns in this order:" % ("Create the board" if b["action"] == "create" else "The board", b["title"]))
    w("")
    for c in b["columns"]:
        w("%d. %s (%s)" % (c["order"] + 1, c["title"], c["action"]))
    w("")
    w("The columns are made with this workstation's own login: the published API has no column route. Each one is "
      "read back, and apply prints the ids to put under `column_ids` in the ftm_pipeline stage block.")
    w("")
    w("## When you approve")
    w("")
    w("The same tool sends exactly what is recorded here. It needs two switches set by a person (--commit and %s=1). "
      "It backs up every folder, preset and board first, narrows the presets, renames the folders, makes the board "
      "and its columns, reads back everything and stops at the first difference. Every write is logged with what it "
      "replaced, and `undo` puts the presets and folders back; the board stays (deleting one is app-only)." % ALLOW_ENV)
    return "\n".join(out).rstrip() + "\n"


def write_plan(plan: dict, out_dir: Path | str = OUT_DIR) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / ("plan_%s.json" % plan["plan_date"])
    m = out_dir / ("plan_%s.md" % plan["plan_date"])
    j.write_text(json.dumps(plan, indent=1), encoding="utf-8", newline="\n")
    m.write_text(render_md(plan), encoding="utf-8", newline="\n")
    return j, m


# -------------------------------------------------------------------------------------------- apply
def may_write(commit: bool) -> bool:
    """Both switches, or nothing is sent."""
    return bool(commit) and os.environ.get(ALLOW_ENV) == "1"


def _write(crm, allowed: bool, path: str, method: str, body=None):
    """The one place this module sends a write. Refuses unless both switches are on."""
    if not allowed:
        raise Refused("refused: a write needs --commit AND %s=1" % ALLOW_ENV)
    return crm.call(path, method, body)


def write_backup(state, boards: list, backup_dir, label: str, day: str) -> Path:
    """Every folder, preset and board as apply found them, before its first write. A second run the
    same day keeps the first file: the first one is the true before."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    base = "presets_%s_%s_before_pipelines" % (label, day)
    path, n = backup_dir / (base + ".json"), 2
    while path.exists():
        path, n = backup_dir / ("%s_%d.json" % (base, n)), n + 1
    path.write_text(json.dumps({"folders": state.raw, "boards": boards}, indent=1), encoding="utf-8", newline="\n")
    return path


def _steps(plan: dict) -> list[str]:
    out = ["narrow preset %s" % r["title"] for r in plan["narrow"] if r["action"] == "change"]
    out += ["rename folder %s" % f["title"] for f in plan["folders"] if f["action"] == "rename"]
    if plan["board"]["action"] == "create":
        out.append("create board %s" % plan["board"]["title"])
    out += ["create column %s" % c["title"] for c in plan["board"]["columns"] if c["action"] in ("create", "unknown")]
    return out


def _preflight(st, plan: dict) -> list[str]:
    """What would stop the run part way, found before its first write: a preset that changed since
    the plan read it. A run that stops part way leaves the account half built."""
    out = []
    for r in plan["narrow"]:
        if r["action"] != "change":
            continue
        live = st.preset(r["title"])
        if live is None:
            out.append("%s is gone" % r["title"])
        elif _must(live) != r["must_after"] and pp.sha(_must(live)) != r["before_sha"]:
            out.append("%s changed since the plan was written" % r["title"])
    if out:
        out.append("run plan again")
    return out


def apply_plan(crm, plan: dict, *, commit: bool = False, log_path=None, backup_dir=BACKUP_DIR, label: str = "ty2",
               columns_crm=None, out=print) -> dict:
    """Send the approved plan. Dry unless both switches are on (a dry run touches nothing, not even
    to read). `columns_crm` is the client on this workstation's login; the board's columns cannot be
    made without it, so a plan that makes columns is refused before its first write when it is
    missing or reads a different account."""
    if plan.get("tool") != "pipelines":
        raise Refused("refused: this file is not a pipelines plan")
    if plan.get("blocking"):
        raise Refused("refused: the plan has %d blocking item(s), the first: %s" % (len(plan["blocking"]), plan["blocking"][0]))
    allowed = may_write(commit)
    done = {"allowed": allowed, "made": [], "already": [], "skipped": [], "would": [], "backup": "", "column_ids": {}}
    if not allowed:
        done["would"] = _steps(plan)
        out("DRY: nothing sent and nothing read. %d steps would run. A real run needs --commit and %s=1."
            % (len(done["would"]), ALLOW_ENV))
        return done
    st = rs.State(crm)
    boards = rows_of(crm.call(R_BOARDS))
    done["backup"] = str(write_backup(st, boards, backup_dir, label, plan.get("plan_date") or date.today().isoformat()))
    out("backup   %s" % done["backup"])
    if plan.get("account") and st.account() and st.account() != plan["account"]:
        raise Refused("refused: the key reads a different account from the one the plan was written for")
    needs_columns = plan["board"]["action"] == "create" or any(c["action"] != "exists" for c in plan["board"]["columns"])
    if needs_columns:
        if columns_crm is None:
            raise Refused("refused before any write: the board's columns need this workstation's login, and none was given")
        import siftline_columns
        acct = siftline_columns.token_account(columns_crm)
        if plan.get("account") and acct and acct != plan["account"]:
            raise Refused("refused before any write: the login belongs to a different account from the plan's")
    problems = _preflight(st, plan)
    if problems:
        raise Refused("refused before any write: %s" % "; ".join(problems))

    def say(kind, text):
        done[kind].append(text)
        out("%-8s %s" % ({"made": "done", "already": "already", "skipped": "skipped"}[kind], text))

    for r in plan["narrow"]:
        if r["action"] == "change":
            _narrow(crm, st, r, allowed, log_path, say)
        elif r["action"] == "already":
            say("already", "preset %s" % r["title"])
    for f in plan["folders"]:
        if f["action"] != "missing":
            _rename(crm, st, f, allowed, log_path, say)
    done["column_ids"] = _board(crm, columns_crm, plan, boards, allowed, log_path, say)
    if done["column_ids"]:
        out("Put these under the ftm_pipeline stage's column_ids in team/autopilot.yaml:")
        for title in COLUMNS:
            out('      "%s": "%s"' % (title, done["column_ids"].get(title, "")))
    return done


def _narrow(crm, st, r: dict, allowed: bool, log_path, say) -> None:
    path = rs.R_PRESET_ONE % r["uuid"]
    live = crm.call(path) or {}
    filters = copy.deepcopy(live.get("filters") or {})
    if (filters.get("must") or {}) == r["must_after"]:
        say("already", "preset %s" % r["title"])             # an earlier run of this plan got this far
        return
    if pp.sha(filters.get("must") or {}) != r["before_sha"]:
        raise Refused("stopped: %s changed since the plan was written; run plan again" % r["title"])
    filters["must"] = r["must_after"]                      # the PATCH replaces the whole filters object
    _write(crm, allowed, path, "PATCH", {"filters": filters})
    rs._log(log_path, {"step": "narrow", "uuid": r["uuid"], "title": r["title"], "before": r["must_before"],
                       "after": r["must_after"]})
    back = crm.call(path) or {}
    if (back.get("filters") or {}).get("must") != r["must_after"]:
        raise Refused("stopped: %s reads back differently from what was sent; nothing after it was touched" % r["title"])
    say("made", "narrowed %s to the Top 1,000" % r["title"])


def _rename(crm, st, f: dict, allowed: bool, log_path, say) -> None:
    old, new = st.folder(f["title"]), st.folder(f["new_title"])
    if new and not old:
        say("already", "folder %s" % f["new_title"])
        return
    if old and new:
        raise Refused("stopped: both %s and %s exist now" % (f["title"], f["new_title"]))
    if not old:
        say("skipped", "folder %s (not found)" % f["title"])
        return
    _write(crm, allowed, rs.R_FOLDER_ONE % old["uuid"], "PATCH", {"title": f["new_title"]})
    rs._log(log_path, {"step": "rename", "uuid": old["uuid"], "before": f["title"], "after": f["new_title"]})
    if (crm.call(rs.R_FOLDER_ONE % old["uuid"]) or {}).get("title") != f["new_title"]:
        raise Refused("stopped: %s was renamed but does not read back as %s" % (f["title"], f["new_title"]))
    old["title"] = f["new_title"]
    say("made", "renamed %s to %s" % (f["title"], f["new_title"]))


def _board(crm, columns_crm, plan: dict, boards: list, allowed: bool, log_path, say) -> dict:
    """The board on the key, its columns on the login, each read back. Returns title -> column id."""
    import siftline_columns
    row = board_row(boards)
    if row:
        say("already", "board %s" % BOARD)
        uuid = row["uuid"]
    else:
        made = _write(crm, allowed, R_BOARD_NEW, "POST", {"title": BOARD}) or {}
        uuid = made.get("uuid") if isinstance(made, dict) else None
        rs._log(log_path, {"step": "board", "title": BOARD, "uuid": uuid})
        if not uuid:
            uuid = (board_row(rows_of(crm.call(R_BOARDS))) or {}).get("uuid")
            if uuid:
                rs._log(log_path, {"step": "board", "title": BOARD, "uuid": uuid})
        if not uuid or str((crm.call(R_BOARD_ONE % uuid) or {}).get("title") or "").strip() != BOARD:
            raise Refused("stopped: the board %s was sent but does not read back" % BOARD)
        say("made", "board %s" % BOARD)
    if columns_crm is None:
        # Every column was already there when the plan listed them: their ids are the plan's.
        return {c["title"]: c["uuid"] for c in plan["board"]["columns"] if c.get("uuid")}
    have = {c["title"].strip().lower(): c for c in siftline_columns.list_columns(columns_crm, uuid)}
    ids = {}
    for i, title in enumerate(COLUMNS):
        c = have.get(title.lower())
        if c:
            say("already", "column %s" % title)
            ids[title] = c["uuid"]
            continue
        if not allowed:
            raise Refused("refused: a write needs --commit AND %s=1" % ALLOW_ENV)
        made = siftline_columns.create_column(columns_crm, uuid, title, i)
        rs._log(log_path, {"step": "column", "board": uuid, "title": title, "uuid": (made or {}).get("uuid")})
        back = {c["title"].strip().lower(): c for c in siftline_columns.list_columns(columns_crm, uuid)}.get(title.lower())
        if not back:
            raise Refused("stopped: the column %s was sent but does not read back; nothing after it was made" % title)
        ids[title] = back["uuid"]
        say("made", "column %s" % title)
    return ids


# --------------------------------------------------------------------------------------------- undo
def undo(crm, log_path, *, commit: bool = False, out=print) -> dict:
    """Put the logged writes back, newest first. A preset someone edited since, and a folder someone
    renamed since, are left where they are and named. The board and its columns stay: deleting a
    SiftLine board is app-only."""
    rows = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    allowed = may_write(commit)
    done = {"allowed": allowed, "restored": [], "left": [], "would": [], "stopped": ""}
    seen: set = set()
    for row in reversed(rows):
        key = (row.get("step"), row.get("uuid") or row.get("title"))
        if key in seen:
            continue
        seen.add(key)
        what = "%s %s" % (row.get("step"), row.get("title") or row.get("after") or row.get("uuid"))
        if row.get("step") in ("board", "column"):
            done["left"].append("%s: deleting a board or a column is app-only; remove the %s board in the app if it should go"
                                % (what, BOARD))
            out("left   %s" % done["left"][-1])
            continue
        if not allowed:
            done["would"].append(what)
            out("would undo %s" % what)
            continue
        try:
            left = _undo_one(crm, row, allowed)
        except Refused as exc:
            done["stopped"] = "%s: %s" % (what, exc)
            out("STOP   " + done["stopped"])
            break
        if left:
            done["left"].append("%s: %s" % (what, left))
            out("left   %s: %s" % (what, left))
        else:
            done["restored"].append(what)
            out("undone %s" % what)
    return done


def _undo_one(crm, row: dict, allowed: bool) -> str:
    step, uid = row.get("step"), row.get("uuid")
    if step == "rename":
        cur = (crm.call(rs.R_FOLDER_ONE % uid) or {}).get("title")
        if cur == row["before"]:
            return ""
        if cur != row["after"]:
            return "renamed by someone since (it reads %r)" % cur
        _write(crm, allowed, rs.R_FOLDER_ONE % uid, "PATCH", {"title": row["before"]})
        if (crm.call(rs.R_FOLDER_ONE % uid) or {}).get("title") != row["before"]:
            raise Refused("the old title did not read back")
        return ""
    if step == "narrow":
        path = rs.R_PRESET_ONE % uid
        cur = crm.call(path) or {}
        filters = copy.deepcopy(cur.get("filters") or {})
        if (filters.get("must") or {}) == row["before"]:
            return ""
        if (filters.get("must") or {}) != row["after"]:
            return "its filter was edited since it was narrowed"
        filters["must"] = row["before"]
        _write(crm, allowed, path, "PATCH", {"filters": filters})
        if ((crm.call(path) or {}).get("filters") or {}).get("must") != row["before"]:
            raise Refused("the old filter did not read back")
        return ""
    return "not a step this tool writes"


# ---------------------------------------------------------------------------------------------- cli
class LazyLogin:
    """This workstation's login, minted the first time a request needs it. A plan asks it only when
    the board already exists (to list its columns), so a plan for a new board never logs in."""

    def __init__(self, gap: float = 2.0):
        self.gap, self._crm = gap, None

    def call(self, path, method="GET", body=None, headers=None):
        if self._crm is None:
            import siftline_columns
            self._crm = siftline_columns.minted_crm(self.gap)
        return self._crm.call(path, method, body, headers)


def main(argv: list[str] | None = None) -> int:
    from agents.autopilot.budget import BudgetExhausted
    ap = argparse.ArgumentParser(description="Three pipelines: plan (read-only), apply and undo (each needs --commit and %s=1)."
                                             % ALLOW_ENV)
    sub = ap.add_subparsers(dest="cmd")
    pl = sub.add_parser("plan", help="read the account, count, write the change list (the default)")
    pl.add_argument("--date", default="", help="the day the plan is for, YYYY-MM-DD (default today)")
    pl.add_argument("--out-dir", default=str(OUT_DIR))
    pl.add_argument("--cache", default="", help="a file that keeps listings and counts, so a re-run does not re-read")
    pl.add_argument("--max-requests", type=int, default=300)
    pl.add_argument("--gap", type=float, default=2.0, help="seconds between requests (2.0 is 30 a minute)")
    pl.add_argument("--as-of", default="", help="the day date windows are worked out from (default today)")
    pl.add_argument("--no-login", action="store_true", help="do not use this workstation's login to list an existing board's columns")
    aq = sub.add_parser("apply", help="send an approved plan; dry unless --commit and %s=1" % ALLOW_ENV)
    aq.add_argument("--plan", required=True)
    aq.add_argument("--commit", action="store_true")
    aq.add_argument("--gap", type=float, default=2.0)
    aq.add_argument("--label", default="ty2", help="the account's short name in the backup file name")
    uq = sub.add_parser("undo", help="replay an apply log backwards; dry unless --commit and %s=1" % ALLOW_ENV)
    uq.add_argument("--log", required=True)
    uq.add_argument("--commit", action="store_true")
    uq.add_argument("--gap", type=float, default=2.0)
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] not in ("plan", "apply", "undo", "-h", "--help"):
        raw = ["plan"] + raw
    args = ap.parse_args(raw)
    if args.cmd in ("apply", "undo") and args.commit and not may_write(True):
        print("refused: --commit was passed but %s is not 1. Nothing was sent." % ALLOW_ENV)
        return 3
    try:
        if args.cmd == "apply":
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            if not may_write(args.commit):
                apply_plan(None, plan, commit=False)
                return 0
            import siftline_columns
            columns = None
            if plan["board"]["action"] == "create" or any(c["action"] != "exists" for c in plan["board"]["columns"]):
                columns = siftline_columns.minted_crm(args.gap)
            log_path = OUT_DIR / ("applied_%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
            done = apply_plan(pp.key_only_crm(args.gap), plan, commit=True, log_path=log_path, label=args.label,
                              columns_crm=columns)
            print("made %d, already %d, skipped %d. Log: %s" % (len(done["made"]), len(done["already"]),
                                                                len(done["skipped"]), log_path))
            return 0
        if args.cmd == "undo":
            if not may_write(args.commit):
                undo(None, args.log, commit=False)
                return 0
            done = undo(pp.key_only_crm(args.gap), args.log, commit=True)
            print("undone %d, left %d%s" % (len(done["restored"]), len(done["left"]),
                                            "; STOPPED: " + done["stopped"] if done["stopped"] else ""))
            return 1 if done["stopped"] else 0
        reader = pp.Reader(pp.key_only_crm(args.gap), cache_path=args.cache or None, max_requests=args.max_requests,
                           now=date.fromisoformat(args.as_of) if args.as_of else None)
        plan = build_plan(reader, day=args.date, columns_reader=None if args.no_login else LazyLogin(args.gap))
        j, m = write_plan(plan, args.out_dir)
        for line in plan_lines(plan):
            print(line)
        print(j)
        print(m)
        return 2 if plan["blocking"] else 0
    except BudgetExhausted as exc:
        print("stopped: %s. Run it again after the hour: apply and undo pick up where they stopped." % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
