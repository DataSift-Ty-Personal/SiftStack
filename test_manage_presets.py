"""Live headed-browser test for DataSift filter preset management.

Usage:
    python test_manage_presets.py --discover                # List all presets and sequences
    python test_manage_presets.py --add-sold-exclusion       # Update presets to exclude Sold
    python test_manage_presets.py --create-sequence          # Create Sold cleanup sequence
    python test_manage_presets.py --all                      # Discovery + update + sequence
    python test_manage_presets.py --preset-folders "00. Niche Sequential"  # Target specific folder

Runs in headed mode so you can watch the browser and identify selector issues.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

# Add src/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv

load_dotenv()


async def main():
    parser = argparse.ArgumentParser(description="Test DataSift preset management")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover and list all preset folders, presets, and sequences",
    )
    parser.add_argument(
        "--add-sold-exclusion",
        action="store_true",
        help="Update existing presets to exclude Sold status/tag",
    )
    parser.add_argument(
        "--create-sequence",
        action="store_true",
        help="Create Sold Property Cleanup sequence",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all: discover + update presets + create sequence",
    )
    parser.add_argument(
        "--preset-folders",
        type=str,
        default=None,
        help='Comma-separated folder names to target (default: all discovered)',
    )
    args = parser.parse_args()

    # Default to --discover if no flags specified
    if not (args.discover or args.add_sold_exclusion or args.create_sequence or args.all):
        args.discover = True

    if args.all:
        args.discover = True
        args.add_sold_exclusion = True
        args.create_sequence = True

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(
                open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
            ),
        ],
    )

    preset_folders = None
    if args.preset_folders:
        preset_folders = [f.strip() for f in args.preset_folders.split(",")]

    from datasift_uploader import run_manage_presets_workflow

    result = await run_manage_presets_workflow(
        discover=args.discover,
        add_sold_exclusion=args.add_sold_exclusion,
        create_sequence=args.create_sequence,
        preset_folders=preset_folders,
        headless=False,
    )

    print("\n" + "=" * 60)
    print("RESULT:")
    print(f"  Success: {result.get('success')}")
    print(f"  Message: {result.get('message')}")

    if result.get("discovery"):
        disc = result["discovery"]
        print(f"\n  DISCOVERY:")
        print(f"    Preset folders: {list(disc.get('preset_folders', {}).keys())}")
        for folder, presets in disc.get("preset_folders", {}).items():
            print(f"      {folder}: {presets}")
        print(f"    Sequences: {disc.get('sequences', [])}")
        print(f"    Raw text elements: {disc.get('_raw_text_count', 0)}")

    if result.get("presets"):
        presets = result["presets"]
        print(f"\n  PRESET UPDATES:")
        print(f"    Updated: {presets.get('updated', [])}")
        print(f"    Failed: {presets.get('failed', [])}")

    if result.get("sequence"):
        seq = result["sequence"]
        print(f"\n  SEQUENCE:")
        print(f"    Name: {seq.get('sequence_name')}")
        print(f"    Success: {seq.get('success')}")
        print(f"    Message: {seq.get('message')}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
