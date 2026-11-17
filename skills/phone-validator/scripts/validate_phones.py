#!/usr/bin/env python3
"""
Phone Validator & Tagger
========================
Validates phone numbers via Trestle's phone_intel API, assigns tier-based
phone tags, and produces DataSift/REISift-ready CSVs for upload.

Designed to work directly with the DataSift "Phone Enrichment" export format,
which uses a wide layout: Phone 1 through Phone 30, each with associated
Phone Type N, Phone Status N, Phone Tags N, and Phone Is Connected N columns.

Usage:
    # Step 1: Estimate cost (always do this first)
    python3 validate_phones.py --input phones.csv --estimate

    # Step 2: Run validation after user confirms
    python3 validate_phones.py --input phones.csv --output ./results/ --api-key YOUR_KEY
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library required. Install with: pip install --break-system-packages requests")
    sys.exit(1)


# ─── Default Tier Configuration ──────────────────────────────────────────────

DEFAULT_TIERS = {
    "Dial First":  (81, 100),
    "Dial Second": (61, 80),
    "Dial Third":  (41, 60),
    "Dial Fourth": (21, 40),
    "Drop":        (0, 20),
}

# Cost per API call (Trestle phone_intel pricing)
COST_PER_PHONE = 0.015

# Trestle API config
TRESTLE_ENDPOINT = "https://api.trestleiq.com/3.0/phone_intel"
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5  # seconds, multiplied each retry


# ─── Phone Number Cleaning ───────────────────────────────────────────────────

def clean_phone(raw: str) -> str:
    """Strip a phone string down to digits, normalize to 10-digit US format."""
    if not raw:
        return ""
    digits = re.sub(r"[^\d]", "", str(raw).strip())
    # Handle 11-digit with leading 1
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # Handle E.164 with +1
    if len(digits) > 10 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


# ─── Trestle API Caller ─────────────────────────────────────────────────────

def call_trestle(phone: str, api_key: str, add_litigator: bool = False) -> dict:
    """
    Call Trestle phone_intel API for a single phone number.
    Returns the parsed JSON response or an error dict.
    """
    params = {"phone": phone}
    if add_litigator:
        params["add_ons"] = "litigator_checks"

    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                TRESTLE_ENDPOINT,
                params=params,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                # Rate limited — wait and retry
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"  Rate limited on {phone}, waiting {wait:.1f}s...")
                time.sleep(wait)
                continue
            elif resp.status_code == 403:
                return {"error": "Invalid API key", "phone_number": phone}
            else:
                return {
                    "error": f"HTTP {resp.status_code}",
                    "phone_number": phone,
                    "detail": resp.text[:200],
                }
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            return {"error": "Timeout after retries", "phone_number": phone}
        except requests.exceptions.RequestException as e:
            return {"error": str(e), "phone_number": phone}

    return {"error": "Max retries exceeded", "phone_number": phone}


# ─── Tier Assignment ─────────────────────────────────────────────────────────

def assign_tier(score: int, tiers: dict) -> str:
    """Given an activity score, return the matching tier tag name."""
    if score is None:
        return "Unknown"
    for tag_name, (low, high) in tiers.items():
        if low <= score <= high:
            return tag_name
    return "Unknown"


def build_tag(tier: str) -> str:
    """Build the final phone tag string — always just the tier name."""
    return tier


# ─── CSV Detection & Reading ────────────────────────────────────────────────

def detect_phone_columns(headers: list) -> list:
    """
    Find all columns that contain phone numbers.

    Handles the DataSift wide export format (Phone 1 through Phone 30) as well
    as simpler formats with a single Phone or Phone Number column.

    Excludes metadata columns like 'Phone Type N', 'Phone Status N',
    'Phone Tags N', and 'Phone Is Connected N'.
    """
    found = []
    # Metadata suffixes to exclude — these are per-phone metadata columns, not phone numbers
    metadata_patterns = re.compile(
        r"phone\s*(type|status|tags?|is\s*connected)\s*\d*",
        re.IGNORECASE
    )

    for header in headers:
        lower = header.strip().lower()

        # Skip metadata columns
        if metadata_patterns.match(lower):
            continue

        # Match numbered phone columns: "Phone 1", "Phone 2", ..., "Phone 30"
        if re.match(r"^phone[\s_]?\d+$", lower):
            found.append(header)
            continue

        # Match generic phone column names
        if lower in (
            "phone", "phone_number", "phone number", "phonenumber",
            "mobile", "cell", "landline", "home phone", "work phone",
            "contact phone", "primary phone",
        ):
            found.append(header)
            continue

    return found


def read_phones_from_csv(filepath: str, phone_column: str = None) -> tuple:
    """
    Read phone numbers from a CSV file.

    Returns:
        tuple: (phones_list, unique_count, total_entries)
            - phones_list: list of (raw_phone, cleaned_phone) tuples
            - unique_count: number of unique cleaned phone numbers
            - total_entries: total phone entries found (before dedup)
    """
    phones = []
    seen = set()

    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        if phone_column:
            columns = [phone_column]
        else:
            columns = detect_phone_columns(headers)

        if not columns:
            print(f"ERROR: No phone columns detected in headers: {headers[:20]}...")
            print("Hint: Name your phone column 'Phone', 'Phone Number', or 'Phone 1'")
            sys.exit(1)

        if not os.environ.get("PHONE_VALIDATOR_QUIET"):
            print(f"Detected {len(columns)} phone column(s): {columns[0]}", end="")
            if len(columns) > 1:
                print(f" through {columns[-1]}", end="")
            print()

        for row in reader:
            for col in columns:
                raw = row.get(col, "").strip()
                if raw:
                    cleaned = clean_phone(raw)
                    if cleaned:
                        phones.append((raw, cleaned))
                        seen.add(cleaned)

    return phones, len(seen), len(phones)


# ─── Cost Estimation ────────────────────────────────────────────────────────

def estimate_cost(filepath: str, phone_column: str = None) -> dict:
    """
    Parse the CSV to count unique phones and estimate Trestle API cost.

    Returns a dict with stats that can be printed or returned as JSON.
    """
    phones, unique_count, total_entries = read_phones_from_csv(filepath, phone_column)

    cost = unique_count * COST_PER_PHONE

    result = {
        "input_file": os.path.basename(filepath),
        "total_entries": total_entries,
        "unique_phones": unique_count,
        "duplicates_saved": total_entries - unique_count,
        "cost_per_phone": COST_PER_PHONE,
        "estimated_cost": round(cost, 2),
    }

    return result


def print_estimate(est: dict):
    """Print a formatted cost estimate."""
    print()
    print("=" * 50)
    print("  PHONE VALIDATION COST ESTIMATE")
    print("=" * 50)
    print(f"  Input file:          {est['input_file']}")
    print(f"  Total phone entries: {est['total_entries']:,}")
    print(f"  Unique phones:       {est['unique_phones']:,}")
    print(f"  Duplicates saved:    {est['duplicates_saved']:,}")
    print(f"  Cost per phone:      ${est['cost_per_phone']:.3f}")
    print(f"  ─────────────────────────────────")
    print(f"  ESTIMATED COST:      ${est['estimated_cost']:.2f}")
    print("=" * 50)
    print()


# ─── Main Processing ─────────────────────────────────────────────────────────

def process_phones(
    phones: list,
    api_key: str,
    tiers: dict,
    add_litigator: bool = False,
    batch_size: int = 10,
    delay: float = 0.1,
    dry_run: bool = False,
) -> tuple:
    """
    Process all phone numbers through Trestle API.
    Returns (results_list, errors_list).
    """
    # Deduplicate
    unique_phones = list(dict.fromkeys(p[1] for p in phones))
    total = len(unique_phones)
    print(f"\nProcessing {total} unique phone numbers...")

    if dry_run:
        print("DRY RUN — generating template without API calls")
        results = []
        for phone in unique_phones:
            results.append({
                "phone_number": phone,
                "activity_score": None,
                "line_type": None,
                "carrier": None,
                "is_valid": None,
                "is_prepaid": None,
                "assigned_tag": "Unscored",
                "is_litigator_risk": None,
            })
        return results, []

    results = []
    errors = []
    processed = 0

    # Process in batches
    for batch_start in range(0, total, batch_size):
        batch = unique_phones[batch_start : batch_start + batch_size]

        with ThreadPoolExecutor(max_workers=min(batch_size, len(batch))) as executor:
            future_to_phone = {
                executor.submit(call_trestle, phone, api_key, add_litigator): phone
                for phone in batch
            }

            for future in as_completed(future_to_phone):
                phone = future_to_phone[future]
                processed += 1

                try:
                    data = future.result()
                except Exception as e:
                    errors.append({"phone_number": phone, "error": str(e)})
                    continue

                if "error" in data and not data.get("is_valid"):
                    # Check if this is a real error vs just a warning
                    if data.get("error") == "Invalid API key":
                        print(f"\nERROR: Invalid Trestle API key. Please check your key.")
                        sys.exit(1)
                    errors.append(data)
                    continue

                score = data.get("activity_score")
                line_type = data.get("line_type")
                tier = assign_tier(score, tiers)
                tag = build_tag(tier)

                litigator_risk = None
                if add_litigator and data.get("add_ons", {}).get("litigator_checks"):
                    litigator_risk = data["add_ons"]["litigator_checks"].get(
                        "phone.is_litigator_risk", None
                    )

                results.append({
                    "phone_number": phone,
                    "activity_score": score,
                    "line_type": line_type,
                    "carrier": data.get("carrier"),
                    "is_valid": data.get("is_valid"),
                    "is_prepaid": data.get("is_prepaid"),
                    "assigned_tag": tag,
                    "is_litigator_risk": litigator_risk,
                })

                # Progress indicator
                if processed % 25 == 0 or processed == total:
                    pct = (processed / total) * 100
                    print(f"  Progress: {processed}/{total} ({pct:.0f}%)")

        # Delay between batches
        if batch_start + batch_size < total and delay > 0:
            time.sleep(delay)

    return results, errors


# ─── Output Writers ──────────────────────────────────────────────────────────

def write_datasift_csv(results: list, output_dir: str) -> str:
    """Write the DataSift-ready phone tags CSV (Phone Number + Phone Tag)."""
    filepath = os.path.join(output_dir, "phone_tags_for_datasift.csv")
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Phone Number", "Phone Tag"])
        for r in results:
            if r.get("is_valid") is not False:  # Skip invalid numbers
                writer.writerow([r["phone_number"], r["assigned_tag"]])
    return filepath


def write_detailed_csv(results: list, output_dir: str) -> str:
    """Write the detailed validation results CSV."""
    filepath = os.path.join(output_dir, "validation_results.csv")
    fieldnames = [
        "phone_number", "activity_score", "line_type", "carrier",
        "is_valid", "is_prepaid", "assigned_tag", "is_litigator_risk",
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    return filepath


def write_errors_csv(errors: list, output_dir: str) -> str:
    """Write errors to CSV for review."""
    if not errors:
        return ""
    filepath = os.path.join(output_dir, "errors.csv")
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["phone_number", "error", "detail"])
        for e in errors:
            writer.writerow([
                e.get("phone_number", ""),
                e.get("error", ""),
                e.get("detail", ""),
            ])
    return filepath


def write_summary(results: list, errors: list, tiers: dict, output_dir: str) -> str:
    """Write a human-readable summary of the validation run."""
    filepath = os.path.join(output_dir, "summary.txt")

    # Compute stats
    total = len(results)
    scores = [r["activity_score"] for r in results if r["activity_score"] is not None]
    tag_counts = Counter(r["assigned_tag"] for r in results)
    line_type_counts = Counter(r["line_type"] for r in results if r["line_type"])
    avg_score = sum(scores) / len(scores) if scores else 0

    # Score distribution buckets
    buckets = defaultdict(int)
    for s in scores:
        bucket = (s // 10) * 10
        buckets[bucket] += 1

    with open(filepath, "w") as f:
        f.write("=" * 60 + "\n")
        f.write("PHONE VALIDATION SUMMARY\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")

        f.write(f"Total phones processed: {total}\n")
        f.write(f"Errors/failures:        {len(errors)}\n")
        f.write(f"Average activity score: {avg_score:.1f}\n\n")

        f.write("─── TIER BREAKDOWN ───\n\n")
        for tag_name in tiers.keys():
            count = tag_counts.get(tag_name, 0)
            pct = (count / total * 100) if total else 0
            f.write(f"  {tag_name:20s}  {count:5d}  ({pct:5.1f}%)\n")
        # Include Unknown/Invalid/Unscored if present
        for tag_name in sorted(tag_counts.keys()):
            if tag_name not in tiers:
                count = tag_counts[tag_name]
                pct = (count / total * 100) if total else 0
                f.write(f"  {tag_name:20s}  {count:5d}  ({pct:5.1f}%)\n")

        f.write(f"\n─── LINE TYPE BREAKDOWN ───\n\n")
        for lt, count in line_type_counts.most_common():
            pct = (count / total * 100) if total else 0
            f.write(f"  {lt:20s}  {count:5d}  ({pct:5.1f}%)\n")

        f.write(f"\n─── SCORE DISTRIBUTION ───\n\n")
        for bucket in sorted(buckets.keys()):
            count = buckets[bucket]
            bar = "█" * (count // max(1, total // 40))
            f.write(f"  {bucket:3d}-{min(bucket+9, 100):3d}  {count:5d}  {bar}\n")

        f.write(f"\n─── DATASIFT UPLOAD INSTRUCTIONS ───\n\n")
        f.write("1. Open your DataSift/REISift account\n")
        f.write("2. Go to Upload → Update Data\n")
        f.write("3. Select 'Tag phones by phone number'\n")
        f.write("4. Upload phone_tags_for_datasift.csv\n")
        f.write("5. Map 'Phone Number' → Phone Number\n")
        f.write("6. Map 'Phone Tag' → Phone Tag\n")
        f.write("7. Complete the upload\n\n")
        f.write("Tags will apply to ALL records sharing each phone number.\n")
        f.write("When sending to a dialer, send ONE tier at a time.\n")

    return filepath


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Validate phones via Trestle API and generate DataSift phone tags"
    )
    parser.add_argument("--input", "-i", required=True, help="Input CSV file path")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory path (required unless --estimate)")
    parser.add_argument("--api-key", "-k", default=os.environ.get("TRESTLE_API_KEY", ""),
                        help="Trestle API key (or set TRESTLE_API_KEY env var)")
    parser.add_argument("--estimate", action="store_true",
                        help="Estimate cost only — parse CSV, count unique phones, "
                             "print cost at $0.015/phone, then exit. No API calls.")
    parser.add_argument("--estimate-json", action="store_true",
                        help="Like --estimate but output as JSON (for programmatic use)")
    parser.add_argument("--tiers", choices=["default", "custom"], default="default",
                        help="Tier strategy: default (5 tiers) or custom")
    parser.add_argument("--custom-tiers", type=str, default=None,
                        help='JSON string for custom tiers, e.g. \'{"Hot": [80,100], "Cold": [0,79]}\'')
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Concurrent API requests per batch")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="Seconds to wait between batches")
    parser.add_argument("--phone-column", type=str, default=None,
                        help="Override phone column name")
    parser.add_argument("--add-litigator", action="store_true",
                        help="Include litigator risk check")
    parser.add_argument("--full-report", action="store_true",
                        help="Generate XLSX report (requires openpyxl)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate CSV template without API calls")
    return parser.parse_args()


def main():
    args = parse_args()

    # Validate input file exists
    if not os.path.isfile(args.input):
        print(f"ERROR: Input file not found: {args.input}")
        sys.exit(1)

    # ─── Estimate mode ───────────────────────────────────────────────
    if args.estimate or args.estimate_json:
        if args.estimate_json:
            os.environ["PHONE_VALIDATOR_QUIET"] = "1"
        est = estimate_cost(args.input, args.phone_column)
        if args.estimate_json:
            print(json.dumps(est, indent=2))
        else:
            print_estimate(est)
        sys.exit(0)

    # ─── Full validation mode ────────────────────────────────────────

    # Validate required args for full run
    if not args.output:
        print("ERROR: --output is required for validation (or use --estimate for cost only)")
        sys.exit(1)

    if not args.api_key and not args.dry_run:
        print("ERROR: No Trestle API key provided.")
        print("Either pass --api-key YOUR_KEY or set TRESTLE_API_KEY env var.")
        print("Sign up at https://trestleiq.com for 25 free trial queries.")
        sys.exit(1)

    # Select tier strategy
    if args.tiers == "custom":
        if not args.custom_tiers:
            print("ERROR: --custom-tiers required when using --tiers custom")
            sys.exit(1)
        try:
            raw = json.loads(args.custom_tiers)
            tiers = {k: tuple(v) for k, v in raw.items()}
        except (json.JSONDecodeError, ValueError) as e:
            print(f"ERROR: Invalid --custom-tiers JSON: {e}")
            sys.exit(1)
    else:
        tiers = DEFAULT_TIERS

    # Create output directory
    os.makedirs(args.output, exist_ok=True)

    # Read phones
    print(f"Reading phones from: {args.input}")
    phones, unique_count, total_entries = read_phones_from_csv(args.input, args.phone_column)
    if not phones:
        print("ERROR: No valid phone numbers found in the input file.")
        sys.exit(1)
    print(f"Found {total_entries} phone entries ({unique_count} unique)")
    print(f"Estimated cost: ${unique_count * COST_PER_PHONE:.2f} ({unique_count} x ${COST_PER_PHONE})")

    # Process
    results, errors = process_phones(
        phones=phones,
        api_key=args.api_key,
        tiers=tiers,
        add_litigator=args.add_litigator,
        batch_size=args.batch_size,
        delay=args.delay,
        dry_run=args.dry_run,
    )

    # Write outputs
    print(f"\nWriting outputs to: {args.output}")
    tag_file = write_datasift_csv(results, args.output)
    print(f"  ✓ DataSift phone tags: {tag_file}")

    detail_file = write_detailed_csv(results, args.output)
    print(f"  ✓ Detailed results:    {detail_file}")

    if errors:
        err_file = write_errors_csv(errors, args.output)
        print(f"  ✓ Errors log:          {err_file}")

    summary_file = write_summary(results, errors, tiers, args.output)
    print(f"  ✓ Summary:             {summary_file}")

    # Optional XLSX report
    if args.full_report:
        try:
            from generate_report import create_xlsx_report
            report_file = create_xlsx_report(results, errors, tiers, args.output)
            print(f"  ✓ XLSX report:         {report_file}")
        except ImportError:
            print("  ⚠ XLSX report skipped (openpyxl not installed)")

    # Print quick summary
    tag_counts = Counter(r["assigned_tag"] for r in results)
    print(f"\n{'─' * 40}")
    print(f"RESULTS: {len(results)} scored, {len(errors)} errors")
    print(f"{'─' * 40}")
    for tag_name in tiers.keys():
        count = tag_counts.get(tag_name, 0)
        print(f"  {tag_name:20s}  {count:5d}")
    for tag_name in sorted(tag_counts.keys()):
        if tag_name not in tiers:
            print(f"  {tag_name:20s}  {tag_counts[tag_name]:5d}")
    print(f"{'─' * 40}")
    print(f"\nUpload '{os.path.basename(tag_file)}' to DataSift → Update Data → Tag phones by phone number")
    print("Done!")


if __name__ == "__main__":
    main()
