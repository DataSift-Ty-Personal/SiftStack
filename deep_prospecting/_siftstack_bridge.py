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

Slice 1 keeps this file empty. Phase 1 (MOD-IV title lookup) will be the
first real consumer — likely importing nj_taxrecords helpers.
"""

# Intentionally empty for Slice 1.
