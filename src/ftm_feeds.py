"""Every first-to-market feed this pipeline knows, in one table (build plan 1.10, 1.12, 1.17).

WHY ONE TABLE. The feeds used to be described in four places that had drifted apart: the saved
searches in config.py, the list names in datasift_formatter and knox_ftm_pull (which disagree on
purpose for condemnations), the four feeds the health check watches, and the source inventory the
health report prints, which still called liens a hand-run pull after they became a daily stage. A
feed added in one place and not the others is how a list ends up uploading with no name, or going
quiet with nobody watching.

This table answers, per feed:
  * which runner stage pulls it and which upload carries its rows
  * which DataSift list it lands on (a setting, so the account's naming decides)
  * whether it rides a paid source that can lapse
  * how the lanes should treat a record on that list (build plan 1.17): when research runs,
    whether a sale date makes it hot, and how long before a second pass

It holds no network code and reads nothing but environment variables.

SWITCHES. A feed is live when its STAGE is named in the run's --stages. Nothing here turns a feed
on. The four notice feeds, condemnations and liens are in today's run; every other row is off
until its stage name is added to FTM_ARGS.

    python src/ftm_feeds.py             # the table
    python src/ftm_feeds.py --routes    # list name -> research rule and lane hints, as JSON
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import asdict, dataclass

# What the teaching expects of one first-to-market list in a month. Printed beside the measured
# count as a reference, never used as an alarm: Blount foreclosure runs about 33 raw notices a
# month and is healthy.
TEACHING_MONTHLY = (100, 200)

# When research runs for a record on the list (build plan 1.17).
ON_ARRIVAL = "on_arrival"          # researched the day it lands (foreclosure, probate, condemned)
AT_EXHAUSTION = "at_exhaustion"    # dialed and mailed first, researched only when every number is spent (liens)


@dataclass(frozen=True)
class Feed:
    key: str
    label: str
    county: str
    notice_type: str
    list_default: str
    stage: str                     # the runner stage that pulls it
    carried_by: str                # the stage whose upload carries its rows
    csv_prefix: str                # the CSV family those rows are written to
    source: str
    paid_source: str = ""          # "tnpublicnotice" | "register_of_deeds" | ""
    core: bool = False             # one of the four feeds the health check has always alarmed on
    stale_days: int = 0            # days of silence that count as a finding; 0 = never judged by silence
    research: str = AT_EXHAUSTION
    hot_by_sale_date: bool = False     # a sale date on the record moves it to the hot auction lane
    long_runway: bool = False          # worked like first-to-market, weeks before any sale notice
    second_chance_days: int = 0
    saved_search: str = ""         # the notice site's dropdown label, for notice feeds
    list_env: str = ""             # the setting that renames the list

    @property
    def list_name(self) -> str:
        if self.list_env:
            named = (os.environ.get(self.list_env) or "").strip()
            if named:
                return named
        return self.list_default

    @property
    def search_label(self) -> str:
        """The dropdown label to scrape. FTM_SEARCH_<FEED KEY> corrects it, and the value "off"
        drops this one search from its stage: a county whose search really holds nothing would
        otherwise turn the stage red every morning for being unreadable."""
        named = (os.environ.get("FTM_SEARCH_" + self.key.upper()) or "").strip()
        if named.lower() in ("off", "none"):
            return ""
        return named or self.saved_search


_TNPN = "tnpublicnotice"
_ROD = "register_of_deeds"

FEEDS: tuple[Feed, ...] = (
    # ── in today's run ────────────────────────────────────────────────
    Feed("knox_foreclosure", "Foreclosure notices, Knox", "Knox", "foreclosure", "Foreclosure",
         "notices", "notices", "ftm_notices", _TNPN, _TNPN, core=True, stale_days=7,
         research=ON_ARRIVAL, hot_by_sale_date=True, saved_search="Foreclosure V2 Knox"),
    Feed("blount_foreclosure", "Foreclosure notices, Blount", "Blount", "foreclosure", "Foreclosure",
         "notices", "notices", "ftm_notices", _TNPN, _TNPN, core=True, stale_days=7,
         research=ON_ARRIVAL, hot_by_sale_date=True, saved_search="Foreclosure V2 Blount"),
    Feed("knox_probate", "Probate notices, Knox", "Knox", "probate", "Probate",
         "notices", "notices", "ftm_notices", _TNPN, _TNPN, core=True, stale_days=7,
         research=ON_ARRIVAL, saved_search="Probate V2 Knox"),
    Feed("blount_probate", "Probate notices, Blount", "Blount", "probate", "Probate",
         "notices", "notices", "ftm_notices", _TNPN, _TNPN, core=True, stale_days=7,
         research=ON_ARRIVAL, saved_search="Probate V2 Blount"),
    Feed("knox_condemned", "Condemnations, Knox", "Knox", "code_violation", "Condemned",
         "capture", "county", "knox_ftm_pull", "City of Knoxville agendas", stale_days=45,
         research=ON_ARRIVAL),
    Feed("knox_liens", "Liens, Knox", "Knox", "lien", "Liens",
         "liens", "county", "knox_ftm_pull", _ROD, _ROD, stale_days=7,
         research=AT_EXHAUSTION),
    # ── off until the stage is named in FTM_ARGS ──────────────────────
    Feed("knox_substitute_trustee", "Substitute trustee appointments, Knox", "Knox", "substitute_trustee",
         "Substitute Trustee", "appointments", "county", "knox_ftm_pull", _ROD, _ROD, stale_days=60,
         research=ON_ARRIVAL, long_runway=True, list_env="FTM_LIST_SUBSTITUTE_TRUSTEE"),
    Feed("knox_tax_sale", "Tax sale notices, Knox", "Knox", "tax_sale", "Tax Sale",
         "tax_sale", "tax_sale", "ftm_feed_tax_sale", _TNPN, _TNPN,
         research=ON_ARRIVAL, hot_by_sale_date=True, saved_search="Tax Sale V2 Knox",
         list_env="FTM_LIST_TAX_SALE"),
    Feed("blount_tax_sale", "Tax sale notices, Blount", "Blount", "tax_sale", "Tax Sale",
         "tax_sale", "tax_sale", "ftm_feed_tax_sale", _TNPN, _TNPN,
         research=ON_ARRIVAL, hot_by_sale_date=True, saved_search="Tax Sale V2 Blount",
         list_env="FTM_LIST_TAX_SALE"),
    # Not "Tax Delinquent": DataSift attaches a list of that exact name by itself, and the sold
    # guard treats it as DataSift's list, not ours. A courthouse upload has to land on a list of
    # its own or a lead that attaches to an old Sold record is never revived.
    Feed("knox_tax_delinquent", "Tax delinquent notices, Knox", "Knox", "tax_delinquent",
         "Tax Delinquent Notice", "tax_delinquent", "tax_delinquent", "ftm_feed_tax_delinquent", _TNPN, _TNPN,
         second_chance_days=90, saved_search="Tax Delinquent V2 Knox", list_env="FTM_LIST_TAX_DELINQUENT"),
    Feed("blount_tax_delinquent", "Tax delinquent notices, Blount", "Blount", "tax_delinquent",
         "Tax Delinquent Notice", "tax_delinquent", "tax_delinquent", "ftm_feed_tax_delinquent", _TNPN, _TNPN,
         second_chance_days=90, saved_search="Tax Delinquent V2 Blount", list_env="FTM_LIST_TAX_DELINQUENT"),
    Feed("knox_tax_sale_report", "County tax sale report, Knox", "Knox", "tax_sale", "Tax Sale",
         "tax_sale_report", "county", "knox_ftm_pull", "Knox County Trustee",
         research=ON_ARRIVAL, hot_by_sale_date=True, list_env="FTM_LIST_TAX_SALE"),
    Feed("knox_code_violations", "Code violations, Knox", "Knox", "code_violation", "Code Violation",
         "code_violations", "county", "knox_ftm_pull", "file drop",
         second_chance_days=90, list_env="FTM_LIST_CODE_VIOLATION"),
    Feed("public_sale", "Public sale notices", "", "public_sale", "Public Sale",
         "public_notices", "public_notices", "ftm_feed_public_notices", _TNPN, _TNPN,
         list_env="FTM_LIST_PUBLIC_SALE"),
    Feed("court_order", "Court order notices", "", "order", "Court Order",
         "public_notices", "public_notices", "ftm_feed_public_notices", _TNPN, _TNPN,
         list_env="FTM_LIST_COURT_ORDER"),
)

BY_KEY = {f.key: f for f in FEEDS}

# Stages that existed before this table. A feed on one of these is part of today's run.
TODAY_STAGES = ("notices", "capture", "liens", "county", "sold")


def requested_stages(raw_args: str | None = None) -> list[str]:
    """The stages FTM_ARGS asks for, in order. Empty when --stages is absent."""
    raw = os.environ.get("FTM_ARGS", "") if raw_args is None else raw_args
    try:
        parts = shlex.split(raw.replace("=", " "))
    except ValueError:
        parts = raw.replace("=", " ").split()
    if "--stages" not in parts:
        return []
    i = parts.index("--stages")
    return [x.strip() for x in parts[i + 1].split(",") if x.strip()] if i + 1 < len(parts) else []


def live_feeds(stages: list[str] | None = None) -> list[Feed]:
    """Feeds whose pulling stage AND carrying stage are both requested. A recorder feed whose
    county stage is missing is staged every morning and never uploaded, so it is not live."""
    want = set(requested_stages() if stages is None else stages)
    return [f for f in FEEDS if f.stage in want and f.carried_by in want]


def feeds_for_stage(stage: str) -> list[Feed]:
    return [f for f in FEEDS if f.stage == stage]


def public_notice_searches() -> list[tuple[str, str, str]]:
    """(county, notice_type, dropdown label) for the public sale and court order feed.

    These saved searches do not exist in the account today, so there is no default label to fall
    back on: a guessed label scrapes nothing and reads exactly like a quiet day. Someone creates
    the searches on the site, then names them here.

        FTM_PUBLIC_NOTICE_SEARCHES="Knox|public_sale|Public Sale V2 Knox;Knox|order|Orders V2 Knox"
    """
    out = []
    for chunk in (os.environ.get("FTM_PUBLIC_NOTICE_SEARCHES") or "").split(";"):
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) != 3 or not all(parts):
            continue
        county, kind, label = parts
        if kind not in ("public_sale", "order"):
            continue
        out.append((county.title(), kind, label))
    return out


def list_for(notice_type: str, stage: str = "") -> str:
    """The list a record of this type lands on. With a stage, only that stage's feeds are read,
    which is what keeps a code violation file row off the Condemned list."""
    for f in FEEDS:
        if f.notice_type == notice_type and (not stage or f.stage == stage):
            return f.list_name
    return ""


def routes() -> dict:
    """List name -> how a record on that list is worked (build plan 1.17).

    This is the half of "liens and condemned records as lists the stages can route by" that the
    pipeline owns: it puts each record on the list, and says here what the list means. The lanes
    read it; nothing in this pipeline acts on it."""
    out: dict[str, dict] = {}
    for f in FEEDS:
        row = out.setdefault(f.list_name, {
            "research": f.research, "hot_by_sale_date": f.hot_by_sale_date, "long_runway": f.long_runway,
            "second_chance_days": f.second_chance_days, "notice_types": [], "feeds": [],
            "in_todays_run": False})
        if f.notice_type not in row["notice_types"]:
            row["notice_types"].append(f.notice_type)
        row["feeds"].append(f.key)
        row["in_todays_run"] = row["in_todays_run"] or (f.stage in TODAY_STAGES and f.carried_by in TODAY_STAGES)
    return out


def table() -> list[dict]:
    live = {f.key for f in live_feeds()}
    rows = []
    for f in FEEDS:
        d = asdict(f)
        d.update(list=f.list_name, saved_search=f.search_label, live=f.key in live)
        rows.append(d)
    return rows


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--routes" in argv:
        print(json.dumps(routes(), indent=1))
        return 0
    if "--json" in argv:
        print(json.dumps(table(), indent=1))
        return 0
    print("%-28s %-16s %-22s %-5s %s" % ("feed", "stage", "list", "live", "source"))
    for r in table():
        print("%-28s %-16s %-22s %-5s %s" % (r["key"], r["stage"], r["list"], "yes" if r["live"] else "no", r["source"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
