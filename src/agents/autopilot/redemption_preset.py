"""The "Tax Sale Redemption" preset: every record tax_sale_results tagged, in the lead manager's folder.

The stage tags a former owner who can still redeem a Knox tax sale house, the research queue takes the
tagged records two a day, and the gate holds them out of every outreach channel. This preset is where a
person sees them all in DataSift: the tagged records, nothing else. The deadline order is in the stage's
own list (Tax_Sale_Redemption_Open_Windows.csv, nearest deadline first, one per run): a saved preset
holds a filter and no sort (none of the 146 presets in the account carried one, read 2026-11-17), so the
records page shows them in its own order.

    python src/agents/autopilot/redemption_preset.py                          # dry: reads, prints the plan
    python src/agents/autopilot/redemption_preset.py --commit                 # makes it and reads it back
    python src/agents/autopilot/redemption_preset.py --undo <uuid> --commit   # takes it away again

Dry by default: without --commit it only reads (the tag, the folders, every preset title, one count).
A preset title is unique in an account, so a preset already named this way is left alone when its
filter is the one planned and stops the run when it is not. The write is read back and must equal what
was sent; every write is logged with what it made, under output/autopilot/redemption_preset/. Runs on the
Open API key only (no login is minted), from the repo root so the key is found in .env.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
ROOT = SRC.parent
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would come first on the path and its modules would shadow others.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

TITLE = "Tax Sale Redemption"
TAG = "Tax Sale Redemption"
FOLDER = "15. Lead Management"          # the lead manager's folder in the account (read 2026-11-20)
OUT_DIR = ROOT / "output" / "autopilot" / "redemption_preset"
R_TAGS = "/api/internal/tag/?offset=0&limit=10000&ordering=title"
R_FOLDERS = "/api/internal/filter-preset-folder/?type=properties&limit=999"
R_FOLDER_PRESETS = "/api/internal/filter-preset-folder/%s/filter-preset/?limit=999"
R_PRESET_NEW = "/api/internal/filter-preset/"
R_PRESET_ONE = "/api/internal/filter-preset/%s/"


class Refused(SystemExit):
    """The run stops here and says why; nothing after it is sent."""


def rows_of(r) -> list:
    if isinstance(r, dict):
        return r.get("results") or r.get("data") or []
    return r or []


def _code(exc) -> int | None:
    return getattr(exc, "code", None)


def plan(crm, *, title: str = TITLE, tag: str = TAG, folder: str = FOLDER) -> dict:
    """Everything the run would do, read from the account and nothing written. Stops (Refused) when the tag
    or the folder is missing, or when a preset of this title already holds a different filter."""
    tag_uuid = next((t.get("uuid") for t in rows_of(crm.call(R_TAGS))
                     if (t.get("title") or "").strip().lower() == tag.strip().lower()), "")
    if not tag_uuid:
        raise Refused("there is no %r tag in this account; create the tag first (tax_sale_results writes it)" % tag)
    folders = rows_of(crm.call(R_FOLDERS))
    target = next((f for f in folders if (f.get("title") or "").strip() == folder.strip()), None)
    if target is None:
        raise Refused("there is no preset folder titled %r; the folders are: %s" % (
            folder, ", ".join(str(f.get("title")) for f in folders)))
    must = {"any_tags": [tag_uuid]}
    account, already = None, None
    for f in folders:
        for p in rows_of(crm.call(R_FOLDER_PRESETS % f["uuid"])):
            if f["uuid"] == target["uuid"] and account is None and (p.get("filters") or {}).get("account"):
                account = p["filters"]["account"]
            if (p.get("title") or "").strip().lower() == title.strip().lower():
                already = dict(p, folder_title=f.get("title"))
    if already is not None and ((already.get("filters") or {}).get("must") or {}) != must:
        raise Refused("a preset named %r already exists (in %s) with a different filter; it was left alone"
                      % (already.get("title"), already["folder_title"]))
    count = crm.count(must)
    body = {"title": title, "folder": target["uuid"], "quick_filter": False, "filters": {"must": must}, "type": "properties"}
    if account:
        body["filters"]["account"] = account
    return {"title": title, "tag": tag, "tag_uuid": tag_uuid, "folder": folder, "folder_uuid": target["uuid"],
            "must": must, "body": body, "count": count,
            "already": {"uuid": already.get("uuid"), "folder": already["folder_title"]} if already else None}


def _log(row: dict, out_dir: Path) -> Path:
    path = Path(out_dir) / ("applied_%s.jsonl" % date.today().isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(dict(row, at=datetime.now().isoformat(timespec="seconds"))) + "\n")
    return path


def apply(crm, p: dict, *, commit: bool = False, out=print, out_dir: Path = OUT_DIR) -> dict:
    """Make the preset from the plan and read it back. Without commit nothing is sent."""
    if p["already"]:
        out("already there: %r in %s, the same filter (%s)" % (p["title"], p["already"]["folder"], p["already"]["uuid"]))
        return {"made": "", "already": p["already"]["uuid"]}
    if not commit:
        out("DRY: nothing sent. A real run makes %r in %s (%d record(s) carry the tag today). Add --commit."
            % (p["title"], p["folder"], p["count"]))
        return {"made": "", "would": p["body"]}
    made = crm.call(R_PRESET_NEW, "POST", p["body"]) or {}
    uuid = made.get("uuid") if isinstance(made, dict) else None
    if not uuid:
        uuid = next((r.get("uuid") for r in rows_of(crm.call(R_FOLDER_PRESETS % p["folder_uuid"]))
                     if r.get("title") == p["title"]), None)
    back = crm.call(R_PRESET_ONE % uuid) if uuid else {}
    if ((back or {}).get("filters") or {}).get("must") != p["must"] or (back or {}).get("title") != p["title"]:
        raise Refused("stopped: %r was sent but reads back differently (uuid %s); look at it by hand" % (p["title"], uuid))
    log = _log({"step": "preset", "uuid": uuid, "title": p["title"], "folder": p["folder"], "must": p["must"]}, out_dir)
    out("made %r in %s (%s), read back; logged in %s" % (p["title"], p["folder"], uuid, log))
    out("undo: python src/agents/autopilot/redemption_preset.py --undo %s --commit" % uuid)
    return {"made": uuid, "log": str(log)}


def undo(crm, uuid: str, *, commit: bool = False, title: str = TITLE, out=print, out_dir: Path = OUT_DIR) -> str:
    """Delete the preset this script made, only while it still reads as made: its title, and a filter of
    the tag alone. Anything else was edited by a person since and is left where it is."""
    try:
        cur = crm.call(R_PRESET_ONE % uuid) or {}
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == 404:
            out("nothing to undo: preset %s is not there" % uuid)
            return "gone"
        raise
    must = (cur.get("filters") or {}).get("must") or {}
    if cur.get("title") != title or set(must) != {"any_tags"} or len(must.get("any_tags") or []) != 1:
        raise Refused("left alone: preset %s reads %r with filter %s, not as this script made it"
                      % (uuid, cur.get("title"), json.dumps(must)[:120]))
    if not commit:
        out("DRY: nothing sent. A real run deletes %r (%s). Add --commit." % (title, uuid))
        return "would"
    crm.call(R_PRESET_ONE % uuid, "DELETE")
    try:
        crm.call(R_PRESET_ONE % uuid)
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == 404:
            _log({"step": "undo", "uuid": uuid, "title": title}, out_dir)
            out("deleted %r (%s)" % (title, uuid))
            return "deleted"
        raise
    raise Refused("the preset %s still reads after the delete" % uuid)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The Tax Sale Redemption preset: dry by default, --commit to make it.")
    ap.add_argument("--commit", action="store_true", help="send the write (without it the run only reads)")
    ap.add_argument("--folder", default=FOLDER, help="the preset folder, by its exact title")
    ap.add_argument("--title", default=TITLE)
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--undo", default="", help="the uuid of a preset this script made, to delete it again")
    ap.add_argument("--gap", type=float, default=1.0, help="seconds between requests")
    args = ap.parse_args(argv)
    from agents.autopilot import preset_patch as pp
    crm = pp.key_only_crm(args.gap)
    try:
        if args.undo:
            undo(crm, args.undo, commit=args.commit, title=args.title)
            return 0
        p = plan(crm, title=args.title, tag=args.tag, folder=args.folder)
        print(json.dumps({k: p[k] for k in ("title", "folder", "must", "count", "already")}, indent=1))
        apply(crm, p, commit=args.commit)
    except Refused as exc:
        print(str(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
