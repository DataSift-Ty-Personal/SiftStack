"""lender_package: the eight-piece private lender set for one property, built on the brain for Roma.

src/lender_package.py (the workbook, document 2) and src/lender_docs.py (the seven letters and forms)
run on a deal spec JSON, exactly as `--spec deals/<deal>.json` runs them on the workstation: the same
build_workbook(), the same compute() the log line prints, the same build_all() with its guard that no
internal note ever reaches a lender.

The spec is inline JSON, or named the way the offer sheet names a pack: a file name in the employee's
workspace or the deals folder (AGENTS_DEALS_DIR, /data/deals on the box). Nothing outside the
workspace, the run folders and the deals folder is opened.

The letters need python-docx. Where it is not installed (the brain's image did not carry it when this
was written) the workbook still builds and the result says plainly that the letters did not, rather
than posting a package with seven pieces silently missing.

Free: it reads one file and writes nine. Nothing goes to DataSift and nothing is sent to a lender.
"""
from __future__ import annotations

from pathlib import Path

from .quick_screen import file_line, files_dir, inputs_of, json_input, safe_text, usd

NAME = "lender_package"
WORKBOOK = "2. The Private Lender Package.xlsx"


def missing_keys(spec: dict) -> list[str]:
    """What compute() and the file names cannot do without, in plain words."""
    loan, resale = spec.get("loan") or {}, spec.get("resale") or {}
    need = [("deal_name", spec.get("deal_name")), ("contract_price", spec.get("contract_price")),
            ("loan.amount", loan.get("amount")), ("loan.rate", loan.get("rate")),
            ("loan.term_months", loan.get("term_months")), ("resale.point", resale.get("point"))]
    return [k for k, v in need if v in (None, "", 0)]


def run(task: dict) -> dict:
    import lender_package as lp
    inputs, _ = inputs_of(task)
    folder = files_dir(task, NAME)
    out = {"ok": False, "run_dir": str(folder.parent), "files": []}
    spec, spec_from, why = json_input(inputs.get("spec") or "", "spec", task)
    if spec is None:
        out["why"] = why
        return out
    gaps = missing_keys(spec)
    if gaps:
        out["why"] = "the deal spec from %s is missing %s" % (spec_from, ", ".join(gaps))
        return out
    lp.build_workbook(spec, str(folder / WORKBOOK))
    m = lp.compute(spec)
    files, docs_why = [WORKBOOK], ""
    try:
        import lender_docs
    except ImportError as exc:
        docs_why = "the seven letters were not built: python-docx is not installed on this box (%s)" % str(exc)[:80]
        written = []
    else:
        written = lender_docs.build_all(spec, folder)
        if len(written) != len(lender_docs.DOCS):
            docs_why = "%d of %d letters built; the rest failed (see the log)" % (len(written), len(lender_docs.DOCS))
    files += sorted(Path(p).name for p in written)
    out.update(ok=True, deal=spec.get("deal_name"), spec_from=spec_from, files=files, docs=len(written),
               docs_why=docs_why, metrics={k: m[k] for k in ("loan", "rate", "term", "payoff", "ltv", "cover_point",
                                                             "cover_floor", "net_profit", "borrower_cash", "interest")})
    return out


def render(task: dict, r: dict) -> str:
    head = ":bank: *Lender package: %s*" % (r.get("deal") or task.get("subject") or "this deal")
    if not r.get("ok"):
        return safe_text(head + "\nNot built: %s" % (r.get("why") or "no reason given"))
    m = r["metrics"]
    lines = [head,
             "Loan %s at %g%% for %d months | payoff %s | %.1f%% of value | covers %.2fx at the ask, %.2fx at the band floor"
             % (usd(m["loan"]), round(m["rate"] * 100, 2), m["term"], usd(m["payoff"]), m["ltv"] * 100,
                m["cover_point"], m["cover_floor"]),
             "Our profit %s | borrower cash at closing %s | from %s" % (usd(m["net_profit"]), usd(m["borrower_cash"]),
                                                                       r.get("spec_from") or "the spec")]
    if r.get("docs_why"):
        lines.append(":warning: " + r["docs_why"][:1].upper() + r["docs_why"][1:])
    lines.append(file_line(task, "Workbook", r["files"][0]))
    if len(r["files"]) > 1:
        lines.append("Letters: %d of 7 built, every one in the same run folder" % (r.get("docs") or 0))
        lines.append(file_line(task, Path(r["files"][1]).stem, r["files"][1]))
    lines.append("Nothing was sent to a lender.")
    return safe_text("\n".join(lines))
