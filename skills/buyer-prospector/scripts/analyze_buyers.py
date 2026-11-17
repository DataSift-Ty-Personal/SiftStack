#!/usr/bin/env python
# coding: utf-8

"""
Buyer Prospector Analysis Script
Filters nationwide buyer data by county/state, categorizes entities,
and prepares a multi-tab Excel workbook for decision-maker research.
"""

import pandas as pd
import sys
import os
import argparse


def categorize_entity(name):
    """Categorize a buyer name into entity types for research prioritization."""
    if pd.isna(name):
        return "UNKNOWN"
    name_upper = str(name).upper().strip()

    # Order matters — check more specific patterns first
    if 'LLC' in name_upper or 'L.L.C' in name_upper:
        return "LLC"
    elif 'TRUST' in name_upper or ' TRU ' in name_upper or name_upper.endswith(' TRU'):
        return "TRUST"
    elif any(kw in name_upper for kw in [' INC', ' INC.', 'INCORPORATED']):
        return "CORPORATION"
    elif any(kw in name_upper for kw in [' CORP', ' CORP.', 'CORPORATION']):
        return "CORPORATION"
    elif 'ESTATE OF' in name_upper or 'ESTATE' in name_upper:
        return "ESTATE"
    elif any(kw in name_upper for kw in [' LP', ' LLP', 'LIMITED PARTNERSHIP']):
        return "LIMITED PARTNERSHIP"
    elif any(kw in name_upper for kw in ['COUNTY', 'CITY OF', 'STATE OF', 'HOUSING AUTHORITY', 'GOVERNMENT']):
        return "GOVERNMENT/AGENCY"
    else:
        words = name.split()
        business_keywords = [
            'PROPERTIES', 'HOLDINGS', 'INVESTMENTS', 'CAPITAL', 'GROUP',
            'PARTNERS', 'COMPANY', 'ENTERPRISES', 'VENTURES', 'REALTY',
            'HOMES', 'REAL ESTATE', 'BUILDERS', 'CONSTRUCTION', 'MANAGEMENT',
            'SOLUTIONS', 'SERVICES', 'ASSOCIATES', 'FUNDING', 'ACQUISITIONS',
            'BUYERS', 'RENOVATIONS', 'DEVELOPMENT', 'CONSULTING'
        ]
        if len(words) <= 3 and not any(kw in name_upper for kw in business_keywords):
            return "INDIVIDUAL"
        elif any(kw in name_upper for kw in business_keywords):
            return "OTHER ENTITY"
        elif len(words) > 3:
            return "OTHER ENTITY"
        else:
            return "INDIVIDUAL"


def reorder_columns(df):
    """Reorder columns for optimal workflow."""
    priority_order = [
        'ResearchPriority',
        'BuyerPurchases6MSum',
        'County Name',
        'County State',
        'BuyerFullName',
        'EntityType',
        'DecisionMaker_FullName',
        'DecisionMaker_FirstName',
        'DecisionMaker_LastName',
        'DecisionMaker_Role',
        'Verification_Source',
        'BuyerAddress',
        'BuyerCity',
        'BuyerState',
        'BuyerZIP',
        'BuyerMailingAddress',
        'BuyerMailingCity',
        'BuyerMailingState',
        'BuyerMailingZIP'
    ]

    base_cols = df.columns.tolist()
    final_cols = [col for col in priority_order if col in base_cols]
    for col in base_cols:
        if col not in final_cols:
            final_cols.append(col)
    return df[final_cols]


def save_multi_tab_excel(df, output_file):
    """Save dataframe to multi-tab Excel with Found/Not Found splits."""
    found_mask = (
        df['DecisionMaker_FullName'].notna() &
        (df['DecisionMaker_FullName'] != '') &
        (df['DecisionMaker_FullName'].str.upper() != 'NOT FOUND')
    )

    df_found = df[found_mask].copy()
    df_not_found = df[~found_mask].copy()

    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='All Records', index=False)
        df_found.to_excel(writer, sheet_name='Found', index=False)
        df_not_found.to_excel(writer, sheet_name='Not Found', index=False)

    return len(df_found), len(df_not_found)


def filter_by_county(df, county_name, state_abbrev):
    """
    Filter the nationwide dataset by county and state.
    Uses fuzzy matching on county name (case-insensitive, partial match).
    State must be an exact 2-letter abbreviation match.
    """
    state_abbrev = state_abbrev.upper().strip()
    county_name = county_name.strip().lower()

    # Remove "county" suffix if user included it
    county_name = county_name.replace(' county', '').strip()

    # Filter by state first
    state_mask = df['County State'].str.upper().str.strip() == state_abbrev
    state_df = df[state_mask]

    if len(state_df) == 0:
        print(f"Error: No data found for state '{state_abbrev}'")
        print(f"Available states: {sorted(df['County State'].unique())}")
        return pd.DataFrame()

    # Try exact match first, fall back to contains match
    county_col_lower = state_df['County Name'].str.lower().str.strip()
    exact_mask = county_col_lower == county_name
    result = state_df[exact_mask]

    # If no exact match, try contains match
    if len(result) == 0:
        contains_mask = county_col_lower.str.contains(county_name, na=False)
        result = state_df[contains_mask]

    if len(result) == 0:
        available = sorted(state_df['County Name'].unique())
        print(f"Error: No data found for county containing '{county_name}' in {state_abbrev}")
        print(f"Available counties in {state_abbrev}: {', '.join(available[:20])}")
        if len(available) > 20:
            print(f"  ... and {len(available) - 20} more")
        return pd.DataFrame()

    # Show matched counties (in case of partial match hitting multiple)
    matched_counties = result['County Name'].unique()
    if len(matched_counties) > 1:
        print(f"Note: Multiple counties matched '{county_name}': {', '.join(matched_counties)}")
        print(f"  Using exact match if available, otherwise all matches included.")

    return result


def analyze_buyers(df):
    """Run entity categorization and add research columns."""
    df = df.copy()
    df['EntityType'] = df['BuyerFullName'].apply(categorize_entity)

    research_needed = ['LLC', 'TRUST', 'CORPORATION', 'ESTATE', 'LIMITED PARTNERSHIP', 'OTHER ENTITY']
    df['ResearchPriority'] = df['EntityType'].apply(lambda x: 'High' if x in research_needed else 'Low')

    df['DecisionMaker_FullName'] = ''
    df['DecisionMaker_FirstName'] = ''
    df['DecisionMaker_LastName'] = ''
    df['DecisionMaker_Role'] = ''
    df['Verification_Source'] = ''

    # Auto-populate individuals — they can be skip traced directly
    individual_mask = df['EntityType'] == 'INDIVIDUAL'
    df.loc[individual_mask, 'DecisionMaker_FullName'] = df.loc[individual_mask, 'BuyerFullName']
    df.loc[individual_mask, 'DecisionMaker_Role'] = 'Individual (Skip Trace Directly)'
    df.loc[individual_mask, 'Verification_Source'] = 'N/A - Individual'

    # Parse individual names into first/last
    for idx in df[individual_mask].index:
        name = str(df.at[idx, 'BuyerFullName']).strip()
        parts = name.split()
        if len(parts) >= 2:
            # Handle "LASTNAME FIRSTNAME" format (common in deed records)
            df.at[idx, 'DecisionMaker_FirstName'] = ' '.join(parts[1:]).title()
            df.at[idx, 'DecisionMaker_LastName'] = parts[0].title()
        elif len(parts) == 1:
            df.at[idx, 'DecisionMaker_LastName'] = parts[0].title()

    df['BuyerMailingAddress'] = df['BuyerAddress'] if 'BuyerAddress' in df.columns else ''
    df['BuyerMailingCity'] = df['BuyerCity'] if 'BuyerCity' in df.columns else ''
    df['BuyerMailingState'] = df['BuyerState'] if 'BuyerState' in df.columns else ''
    df['BuyerMailingZIP'] = df['BuyerZIP'] if 'BuyerZIP' in df.columns else ''

    # Sort by purchase volume descending — most active buyers first
    df = df.sort_values('BuyerPurchases6MSum', ascending=False).reset_index(drop=True)

    df = reorder_columns(df)
    return df


def main():
    parser = argparse.ArgumentParser(description='Buyer Prospector: Analyze buyer data by county')
    parser.add_argument('input_file', help='Path to the nationwide buyer CSV')
    parser.add_argument('--county', required=True, help='County name to filter')
    parser.add_argument('--state', required=True, help='State abbreviation (e.g., TN, FL, TX)')
    parser.add_argument('--output', default=None, help='Output filename (default: auto-generated)')
    parser.add_argument('--min-purchases', type=int, default=2, help='Minimum purchases to include (default: 2)')

    args = parser.parse_args()

    # Load the data
    print(f"Loading nationwide buyer data from {args.input_file}...")
    try:
        if args.input_file.endswith('.xlsx'):
            df = pd.read_excel(args.input_file)
        else:
            df = pd.read_csv(args.input_file)
    except Exception as e:
        print(f"Error loading file: {e}")
        return

    print(f"Loaded {len(df):,} total records across {df['County Name'].nunique()} counties")

    # Filter by county/state
    print(f"\nFiltering for {args.county} County, {args.state}...")
    filtered = filter_by_county(df, args.county, args.state)

    if len(filtered) == 0:
        return

    # Apply minimum purchase filter
    if args.min_purchases > 0:
        before = len(filtered)
        filtered = filtered[filtered['BuyerPurchases6MSum'] >= args.min_purchases]
        if before != len(filtered):
            print(f"  Filtered from {before} to {len(filtered)} records (min {args.min_purchases} purchases)")

    print(f"\nFound {len(filtered):,} buyers in {filtered['County Name'].iloc[0]} County, {args.state}")

    # Analyze
    analyzed = analyze_buyers(filtered)

    # Generate output filename
    county_clean = filtered['County Name'].iloc[0].replace(' ', '_')
    if args.output:
        output_file = args.output
    else:
        output_file = f"{county_clean}_{args.state}_Buyer_Analysis.xlsx"

    # Save
    found_count, not_found_count = save_multi_tab_excel(analyzed, output_file)

    # Report
    print(f"\nAnalysis complete! Saved to: {output_file}")
    print(f"\n--- Entity Type Distribution ---")
    entity_dist = analyzed['EntityType'].value_counts()
    for etype, count in entity_dist.items():
        print(f"  {etype}: {count}")

    print(f"\n--- Research Priority ---")
    priority_dist = analyzed['ResearchPriority'].value_counts()
    for priority, count in priority_dist.items():
        print(f"  {priority}: {count}")

    high_priority = len(analyzed[analyzed['ResearchPriority'] == 'High'])
    low_priority = len(analyzed[analyzed['ResearchPriority'] == 'Low'])

    print(f"\n--- Output Tabs ---")
    print(f"  All Records: {len(analyzed)} total")
    print(f"  Found: {found_count} (decision-makers pre-identified)")
    print(f"  Not Found: {not_found_count} (need research)")

    print(f"\n--- Top 10 Most Active Buyers ---")
    top10 = analyzed.head(10)[['BuyerFullName', 'BuyerPurchases6MSum', 'EntityType', 'BuyerState']].to_string(index=False)
    print(top10)

    return output_file


if __name__ == "__main__":
    main()
