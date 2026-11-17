"""Autoresearch-style experiment harness for obituary enrichment quality.

Runs the obituary enrichment pipeline against a ground truth test set and
measures quality metrics. Designed for iterative optimization: modify
search/parse parameters in obituary_enricher.py, run this harness, measure
composite_score, keep or revert.

Usage:
    python src/obituary_experiment.py --test-set tests/obituary_ground_truth.json
    python src/obituary_experiment.py --test-set tests/obituary_ground_truth.json --sample 30
    python src/obituary_experiment.py --test-set tests/obituary_ground_truth.json --append-results
"""

import argparse
import csv
import json
import logging
import os
import random
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Add src/ to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from notice_parser import NoticeData
from obituary_enricher import enrich_obituary_data

logger = logging.getLogger(__name__)

# Metric weights for composite score
WEIGHT_HIT_RATE = 0.20        # % of known deceased correctly identified
WEIGHT_CONFIDENCE = 0.25      # Weighted confidence score (high=3, medium=2, low=1)
WEIGHT_FULL_PAGE = 0.15       # % fetched as full page (not snippet)
WEIGHT_HEIR_COMPLETENESS = 0.20  # Avg heirs found / expected heirs
WEIGHT_DM_IDENTIFIED = 0.10   # % with decision-maker name
WEIGHT_ESTATE_PENALTY = 0.10  # Penalty for estate fallback rate (lower = better)


def load_ground_truth(path: str) -> list[dict]:
    """Load ground truth test set from JSON."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ground_truth_to_notices(records: list[dict]) -> list[NoticeData]:
    """Convert ground truth records to NoticeData objects for pipeline input."""
    notices = []
    for r in records:
        n = NoticeData()
        n.owner_name = r["owner_name"]
        n.address = r["address"]
        n.city = r["city"]
        n.state = r.get("state", "TN")
        n.zip = r.get("zip", "")
        n.county = r.get("county", "Knox")
        n.notice_type = "foreclosure"
        # Clear any deceased fields — pipeline should re-discover them
        n.owner_deceased = ""
        n.date_of_death = ""
        n.obituary_url = ""
        n.decision_maker_name = ""
        n.decision_maker_relationship = ""
        n.dm_confidence = ""
        n.heir_map_json = ""
        notices.append(n)
    return notices


def compute_metrics(
    ground_truth: list[dict],
    results: list[NoticeData],
) -> dict:
    """Compare pipeline results against ground truth and compute quality metrics."""
    total = len(ground_truth)
    if total == 0:
        return {"composite_score": 0.0}

    # Map results by address for matching
    result_map = {}
    for n in results:
        key = (n.address.strip().lower(), n.city.strip().lower())
        result_map[key] = n

    # Metrics accumulators
    hits = 0           # Correctly identified as deceased
    confidence_sum = 0  # Weighted confidence (high=3, medium=2, low=1)
    full_page_count = 0
    dm_identified = 0
    estate_count = 0
    heir_completeness_sum = 0.0

    for gt in ground_truth:
        key = (gt["address"].strip().lower(), gt["city"].strip().lower())
        result = result_map.get(key)

        if not result:
            continue

        # Hit rate: did we correctly identify as deceased?
        if result.owner_deceased == "yes":
            hits += 1

        # Confidence score
        conf = result.dm_confidence.lower() if result.dm_confidence else ""
        if conf == "high":
            confidence_sum += 3
        elif conf == "medium":
            confidence_sum += 2
        elif conf == "low":
            confidence_sum += 1

        # Full page rate
        src_type = getattr(result, "obituary_source_type", "")
        if src_type == "full_page":
            full_page_count += 1

        # DM identified
        if result.decision_maker_name:
            dm_identified += 1

        # Estate fallback
        rel = result.decision_maker_relationship or ""
        if rel.lower() == "estate":
            estate_count += 1

        # Heir completeness: compare heir count in result vs ground truth
        gt_heirs = gt.get("total_heirs_in_map", 1) or 1
        result_heirs = 0
        if result.heir_map_json:
            try:
                result_heirs = len(json.loads(result.heir_map_json))
            except (json.JSONDecodeError, TypeError):
                pass
        heir_completeness_sum += min(result_heirs / gt_heirs, 1.0)

    # Compute normalized metrics (0.0 to 1.0)
    hit_rate = hits / total
    confidence_score = confidence_sum / (total * 3)  # Max is 3 per record
    full_page_rate = full_page_count / total
    dm_rate = dm_identified / total
    estate_rate = estate_count / total
    heir_completeness = heir_completeness_sum / total

    # Composite score (weighted)
    composite = (
        WEIGHT_HIT_RATE * hit_rate
        + WEIGHT_CONFIDENCE * confidence_score
        + WEIGHT_FULL_PAGE * full_page_rate
        + WEIGHT_HEIR_COMPLETENESS * heir_completeness
        + WEIGHT_DM_IDENTIFIED * dm_rate
        + WEIGHT_ESTATE_PENALTY * (1.0 - estate_rate)  # Invert: lower estate = better
    )

    return {
        "composite_score": round(composite, 4),
        "hit_rate": round(hit_rate, 4),
        "confidence_score": round(confidence_score, 4),
        "full_page_rate": round(full_page_rate, 4),
        "dm_rate": round(dm_rate, 4),
        "estate_rate": round(estate_rate, 4),
        "heir_completeness": round(heir_completeness, 4),
        "hits": hits,
        "total": total,
        "full_page": full_page_count,
        "snippet": total - full_page_count,
        "dm_identified": dm_identified,
        "estate_fallback": estate_count,
        "confidence_high": sum(
            1 for n in results
            if n.dm_confidence and n.dm_confidence.lower() == "high"
        ),
        "confidence_medium": sum(
            1 for n in results
            if n.dm_confidence and n.dm_confidence.lower() == "medium"
        ),
        "confidence_low": sum(
            1 for n in results
            if n.dm_confidence and n.dm_confidence.lower() == "low"
        ),
    }


def get_git_commit() -> str:
    """Get current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def append_results(metrics: dict, description: str = "", results_path: str = "results.tsv"):
    """Append experiment results to TSV file (autoresearch format)."""
    commit = get_git_commit()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header_needed = not os.path.exists(results_path)
    with open(results_path, "a", encoding="utf-8") as f:
        if header_needed:
            f.write("timestamp\tcommit\tcomposite_score\thit_rate\tconfidence_score\t"
                    "full_page_rate\tdm_rate\testate_rate\their_completeness\t"
                    "hits\ttotal\tsec_per_record\tstatus\tdescription\n")
        status = "baseline" if not description else "experiment"
        spr = metrics.get("sec_per_record", "")
        f.write(
            f"{timestamp}\t{commit}\t{metrics['composite_score']}\t"
            f"{metrics['hit_rate']}\t{metrics['confidence_score']}\t"
            f"{metrics['full_page_rate']}\t{metrics['dm_rate']}\t"
            f"{metrics['estate_rate']}\t{metrics['heir_completeness']}\t"
            f"{metrics['hits']}\t{metrics['total']}\t{spr}\t{status}\t{description}\n"
        )


def main():
    parser = argparse.ArgumentParser(description="Obituary enrichment experiment harness")
    parser.add_argument("--test-set", required=True, help="Path to ground truth JSON")
    parser.add_argument("--sample", type=int, default=0,
                        help="Run on a random sample of N records (0 = all)")
    parser.add_argument("--append-results", action="store_true",
                        help="Append metrics to results.tsv")
    parser.add_argument("--description", default="",
                        help="Experiment description for results.tsv")
    parser.add_argument("--max-heir-depth", type=int, default=2,
                        help="Max heir verification depth")
    parser.add_argument("--skip-heir-verification", action="store_true",
                        help="Skip heir verification (Phase B only)")
    parser.add_argument(
        "--llm-backend", choices=["anthropic", "ollama", "openrouter"],
        default=os.getenv("LLM_BACKEND", "anthropic"),
        help="LLM backend: 'anthropic' (Claude Haiku), 'ollama' (local), or 'openrouter' (cheap cloud)",
    )
    parser.add_argument("--skip-ancestry", action="store_true",
                        help="Skip Ancestry.com fallback (test Haiku-only baseline)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Apply LLM backend override
    if args.llm_backend:
        import config as cfg_mod
        cfg_mod.LLM_BACKEND = args.llm_backend
        if args.llm_backend == "ollama":
            logger.info("Using Ollama backend (%s)", cfg_mod.OLLAMA_MODEL)
        elif args.llm_backend == "openrouter":
            logger.info("Using OpenRouter backend (%s)", cfg_mod.OPENROUTER_MODEL)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load ground truth
    ground_truth = load_ground_truth(args.test_set)
    logger.info("Loaded %d ground truth records", len(ground_truth))

    # Optional sampling
    if args.sample and args.sample < len(ground_truth):
        ground_truth = random.sample(ground_truth, args.sample)
        logger.info("Sampled %d records for this experiment", len(ground_truth))

    # Convert to NoticeData
    notices = ground_truth_to_notices(ground_truth)

    # Get API key
    import config as cfg
    api_key = cfg.ANTHROPIC_API_KEY
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    # Run pipeline
    start = time.time()
    logger.info("Running obituary enrichment pipeline...")
    enrich_obituary_data(
        notices,
        api_key=api_key,
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_ancestry=args.skip_ancestry,
    )
    elapsed = time.time() - start

    # Compute metrics
    metrics = compute_metrics(ground_truth, notices)
    metrics["elapsed_s"] = round(elapsed, 1)
    metrics["sec_per_record"] = round(elapsed / max(metrics["total"], 1), 1)

    # Append to results.tsv (do this BEFORE printing to avoid losing data on encoding errors)
    if args.append_results:
        append_results(metrics, args.description)
        logger.info("Results appended to results.tsv")

    # Print results
    print("\n" + "=" * 60)
    print("EXPERIMENT RESULTS")
    print("=" * 60)
    print(f"  Records tested:      {metrics['total']}")
    print(f"  Elapsed time:        {elapsed:.0f}s ({elapsed/max(metrics['total'],1):.1f}s/record)")
    print(f"  Composite score:     {metrics['composite_score']:.4f}")
    print(f"  " + "-" * 37)
    print(f"  Hit rate:            {metrics['hit_rate']:.1%} ({metrics['hits']}/{metrics['total']})")
    print(f"  Confidence score:    {metrics['confidence_score']:.4f}")
    print(f"    High:              {metrics['confidence_high']}")
    print(f"    Medium:            {metrics['confidence_medium']}")
    print(f"    Low:               {metrics['confidence_low']}")
    print(f"  Full-page rate:      {metrics['full_page_rate']:.1%} ({metrics['full_page']}/{metrics['total']})")
    print(f"  DM identified:       {metrics['dm_rate']:.1%} ({metrics['dm_identified']}/{metrics['total']})")
    print(f"  Estate fallback:     {metrics['estate_rate']:.1%} ({metrics['estate_fallback']}/{metrics['total']})")
    print(f"  Heir completeness:   {metrics['heir_completeness']:.4f}")
    print(f"  Sec/record:          {metrics['sec_per_record']:.1f}s")

    # Ancestry fallback stats
    ancestry_types = {"ssdi", "ancestry", "obituary_collection"}
    ancestry_recovered = [n for n in notices
                          if getattr(n, "obituary_source_type", "") in ancestry_types
                          and n.owner_deceased == "yes"]
    total_misses = metrics["total"] - metrics["hits"] + len(ancestry_recovered)
    if ancestry_recovered:
        print(f"  " + "-" * 37)
        print(f"  Ancestry recoveries: {len(ancestry_recovered)}/{total_misses} misses")
        for n in ancestry_recovered:
            print(f"    + {n.owner_name} (DOD: {n.date_of_death}) via {n.obituary_source_type}")
    elif not args.skip_ancestry:
        print(f"  Ancestry recoveries: 0 (no Haiku misses recovered)")

    print("=" * 60)

    return metrics["composite_score"]


if __name__ == "__main__":
    main()
