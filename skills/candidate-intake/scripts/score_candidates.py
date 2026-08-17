#!/usr/bin/env python3
"""Score Candidate Records against the configured rubric.

Reads a JSON array of Candidate Records plus .candidate-intake-config.json,
fills in `score` (0-100), `tier` (Interview / Maybe / Dismiss), and
`score_reason`, and writes the same array back out. Deterministic: same
input + config always produces the same output. Stdlib only.

Usage:
    python score_candidates.py --config .candidate-intake-config.json \
        --in candidates.json --out scored.json
"""
import argparse
import json
import re
import sys


def searchable_text(cand):
    parts = [
        str(cand.get("role_fit_notes", "")),
        str(cand.get("screener_answers", "")),
        str(cand.get("raw_text", "")),
        str(cand.get("english_level", "")),
        str(cand.get("location", "")),
    ]
    return " ".join(parts).lower()


def match_criteria(criteria, text):
    """Return (matched_labels, missing_labels) for keyword criteria lists."""
    matched, missing = [], []
    for crit in criteria:
        keywords = [k.lower() for k in crit.get("keywords", []) if k]
        label = crit.get("label", "unnamed")
        if keywords and any(k in text for k in keywords):
            matched.append(label)
        else:
            missing.append(label)
    return matched, missing


def parse_years(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def score_candidate(cand, cfg):
    scoring = cfg.get("scoring", {})
    weights = scoring.get("weights", {})
    w_must = float(weights.get("must_haves", 40))
    w_exp = float(weights.get("experience", 20))
    w_eng = float(weights.get("english", 15))
    w_scr = float(weights.get("screeners", 15))
    w_nice = float(weights.get("nice_to_haves", 10))

    text = searchable_text(cand)
    reasons = []

    # Must-haves
    must = cfg.get("must_haves", [])
    must_matched, must_missing = match_criteria(must, text)
    must_frac = (len(must_matched) / len(must)) if must else 1.0
    pts_must = must_frac * w_must
    if must:
        reasons.append("%d/%d must-haves" % (len(must_matched), len(must)))

    # Experience
    full_years = float(scoring.get("experience_full_credit_years", 2)) or 1.0
    years = parse_years(cand.get("years_experience"))
    if years is None:
        pts_exp = 0.0
        reasons.append("exp unknown")
    else:
        pts_exp = min(years / full_years, 1.0) * w_exp
        reasons.append("%g yrs exp" % years)

    # English
    levels = scoring.get("english_levels", {})
    level = str(cand.get("english_level", "")).strip().lower()
    if level and level in levels:
        eng_frac = float(levels[level])
        reasons.append("%s English" % level)
    else:
        eng_frac = float(scoring.get("unknown_english_credit", 0.5))
        reasons.append("English unknown")
    pts_eng = eng_frac * w_eng

    # Screeners
    screeners = cfg.get("screener_questions", [])
    answers = str(cand.get("screener_answers", "")).lower()
    if screeners and answers.strip():
        hit = 0
        for q in screeners:
            ideal = [k.lower() for k in q.get("ideal_keywords", []) if k]
            if ideal and any(k in answers for k in ideal):
                hit += 1
        scr_frac = hit / len(screeners)
        reasons.append("%d/%d screeners" % (hit, len(screeners)))
    else:
        scr_frac = 0.0
        if screeners:
            reasons.append("no screener answers")
    pts_scr = scr_frac * w_scr

    # Nice-to-haves
    nice = cfg.get("nice_to_haves", [])
    nice_matched, _ = match_criteria(nice, text)
    nice_frac = (len(nice_matched) / len(nice)) if nice else 0.0
    pts_nice = nice_frac * w_nice
    if nice_matched:
        reasons.append("+%d nice-to-have%s" % (len(nice_matched), "s" if len(nice_matched) != 1 else ""))

    score = round(pts_must + pts_exp + pts_eng + pts_scr + pts_nice)

    tiers = scoring.get("tiers", {})
    interview_min = float(tiers.get("interview_min", 75))
    maybe_min = float(tiers.get("maybe_min", 50))
    if score >= interview_min:
        tier = "Interview"
    elif score >= maybe_min:
        tier = "Maybe"
    else:
        tier = "Dismiss"

    if must_missing:
        reasons.append("missing: " + ", ".join(must_missing))

    cand["score"] = score
    cand["tier"] = tier
    cand["score_reason"] = ", ".join(reasons)
    return cand


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=".candidate-intake-config.json")
    ap.add_argument("--in", dest="infile", default="candidates.json")
    ap.add_argument("--out", dest="outfile", default="scored.json")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    with open(args.infile, encoding="utf-8-sig") as f:
        candidates = json.load(f)
    if not isinstance(candidates, list):
        sys.exit("Input must be a JSON array of Candidate Records")

    scored = [score_candidate(dict(c), cfg) for c in candidates]
    scored.sort(key=lambda c: (-c["score"], str(c.get("full_name", ""))))

    with open(args.outfile, "w", encoding="utf-8") as f:
        json.dump(scored, f, indent=2, ensure_ascii=False)

    for c in scored:
        print("%3d  %-9s %-24s %s" % (c["score"], c["tier"],
              str(c.get("full_name", "?"))[:24], c["score_reason"]),
              file=sys.stderr)
    print("Scored %d candidate(s) -> %s" % (len(scored), args.outfile),
          file=sys.stderr)


if __name__ == "__main__":
    main()
