# Pull Foreclosure Records

## Overview

This SOP pulls new foreclosure notices for a county, qualifies each record against the buy box, and writes an upload CSV. Use it for the daily first-to-market pull or a bounded backfill.

## Parameters

- **county** (required): The county to pull (Knox or Blount)
- **output_dir** (optional, default: "output/"): Directory where the CSV lands
- **max_records** (optional, default: "50"): Cap on records processed this run

**Constraints for parameter acquisition:**
- If all required parameters are already provided, You MUST proceed to the Steps
- If any required parameters are missing, You MUST ask for them before proceeding
- When asking for parameters, You MUST request all parameters in a single prompt
- When asking for parameters, You MUST use the exact parameter names as defined

## Steps

### 1. Pull New Notices

Pull notices published since the last run for {county}.

**Constraints:**
- You MUST record the run start time and county in {output_dir}/progress.md
- You MUST NOT re-pull notices already in the seen list because duplicate records corrupt downstream dedup
- You SHOULD stop and report if the pull returns zero notices

### 2. Qualify Records

Filter each notice against the buy box.

**Constraints:**
- You MUST keep only single family properties
- You MUST write qualified records to {output_dir}/qualified.csv
- You MAY flag borderline records for manual review

## Examples

### Example Input
county: Knox

### Example Output
output/qualified.csv with 12 records, progress.md updated

## Troubleshooting

### Zero notices returned
Check whether the pull was blocked before assuming a quiet day.
