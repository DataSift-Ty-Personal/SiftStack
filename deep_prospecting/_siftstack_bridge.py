"""SiftStack ↔ deep_prospecting import boundary.

Every `from src.X import Y` in this package goes here, and nowhere else.
Other modules in `deep_prospecting/` import from THIS file, never directly
from `src/`. Two reasons:

  1. Splittability. This module is designed to graduate into its own
     repo eventually. When it does, only this file needs to be
     replaced — every other deep_prospecting/ module stays as-is.
  2. Paradigm boundary. SiftStack's NoticeData is a `@dataclass`.
     deep_prospecting uses Pydantic v2. Conversion + adaptation lives
     here, not scattered across phase code.

How to add a new SiftStack import:

  1. Import the SiftStack symbol at the top of this file.
  2. Re-export it (or wrap it in an adapter function) with a name that
     reflects deep_prospecting's vocabulary, not SiftStack's.
  3. Document the conversion contract in a docstring.

What NOT to put here:

  - Generic Python stdlib imports.
  - Pure third-party imports (anthropic, playwright, etc.).
  - Anything that could equally live in `_utils.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make src/ importable when running from the project root. SiftStack's
# convention is "PYTHONPATH=src + cwd=project root"; replicate that here
# so deep_prospecting works in both layouts (CLI, REPL, pytest).
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ── NJ MOD-IV (taxrecords-nj.com) — three of our four counties ──────────
# Used by Phase 1 (title lookup) to resolve owner of record + parcel ID +
# mailing address for a target property. The vendor (Vital Communications)
# covers Middlesex / Somerset / Union. Essex is on a different vendor with
# reCAPTCHA, so phase 1 records SourceStatus=SKIPPED for Essex inputs
# rather than blowing up.
from nj_taxrecords import (  # noqa: E402
    Parcel as ModIVParcel,
    lookup_by_address as modiv_lookup_by_address,
    lookup_by_owner_name as modiv_lookup_by_owner,
)

# ── Owner-name death-indicator classifier ───────────────────────────────
# Pure string→string function. Returns one of {"personal_rep","life_estate",
# "care_of","et_al","trustee",""}. Same logic used by SiftStack's Knox
# enrichment — no adapter needed, just re-export under a name that doesn't
# leak the SiftStack module path.
from tax_enricher import detect_deceased_indicator as classify_owner_death_indicator  # noqa: E402

__all__ = [
    "ModIVParcel",
    "modiv_lookup_by_address",
    "modiv_lookup_by_owner",
    "classify_owner_death_indicator",
]
