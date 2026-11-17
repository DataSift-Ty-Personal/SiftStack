# Config schema for `build_directory.py`

The builder reads one JSON object. Only `records` is strictly required; everything else is
optional and degrades gracefully (skip `top_picks` and you get no Top Picks tab, etc.).
Run it with `python scripts/build_directory.py <config.json> <output.xlsx>`. See
`example_config.json` for a complete, runnable example.

## Top level

| Key | Type | Purpose |
|---|---|---|
| `title` | string | Workbook/deliverable title (used in tab headers). |
| `geo_label` | string | The target market, e.g. `"Knox + Blount"`. Fills the "Serves \<geo\>" column header and the summary labels. |
| `top_picks_note` | string | Italic subtitle on the Top Picks tab. |
| `records` | array | **Required.** One object per provider, see below. |
| `top_picks` | array | Best-per-category rows for the Top Picks tab. |
| `methodology` | array | Content blocks for the "How to Vet + Method" tab. |
| `reference_tabs` | array | Extra reference tabs (utility districts, permit offices, etc.). |

## `records[]`: one provider per object

Use these exact keys (any omitted key renders blank). The order of columns in the sheet is
fixed; you don't control it from the config.

| Key | Meaning / convention |
|---|---|
| `trade` | Category label (e.g. `"Plumbing"`, `"Excavation / Underground Utility"`). Group rows by this; the Directory tab has a filter so the user can sort by it. |
| `company` | Company or provider name (bolded in the sheet). |
| `contact` | Named contact, or `"-"`. |
| `phone` | Best phone, or `"not found"`. |
| `phone_status` | `"Verified"` (matches an independent listing), `"From site"`, `"No"`, `"Confirm"`, `"UNVERIFIED"`. Centered column. |
| `email` | Email or `"not found"` / `"site form"`. |
| `website` | Domain or FB/listing reference. |
| `service_area` | What area they actually cover, in their words. |
| `serves_geo` | Drives color + the summary counts: `"Yes"` (green, confirmed/based in market), `"Confirm <area>"` (amber, metro-local, verify on call), `"Unverified"`/`"No"` (red). |
| `rating` | Rating **with count and source**, e.g. `"4.8★ (43) Google"`. Never stars alone. |
| `license` | License #/status on the relevant board, or `"not found, verify"` / `"likely not required"`. |
| `bbb` | BBB accreditation/rating, or `"not found"`. |
| `why` | Strengths / fit: why they're worth calling. |
| `source` | How they surfaced. Start public-source additions with `"ADDED"` so they're counted separately (e.g. `"ADDED (public, septic not in group)"`). |
| `cautions` | The one thing to verify / watch before relying on them. |
| `confidence` | `"High"`, `"Med-High"`, `"Medium"`, `"Low"`, `"Low fit"`; drives color. |
| `top_pick` | `"★"` marks it a top pick (gold row highlight + counted); `""` otherwise. |

## `top_picks[]`: best per category

| Key | Meaning |
|---|---|
| `category` | The category this is the pick for. |
| `pick` | The chosen provider. |
| `phone` | Their phone (so the tab is call-ready). |
| `why` | One line on why it's the pick. |
| `runner_up` | The next-best option. |
| `note` | A market/coverage note (e.g. `"Serves Maryville ✓"`). |

## `methodology[]`: content blocks for the method tab

Each block is `{"type": ..., "text": ..., "color": ..., "bold": ...}`.

| `type` | Renders as |
|---|---|
| `"title"` | Big navy title. |
| `"subtitle"` | Small grey italic line. |
| `"h1"` | Navy section banner. |
| `"h2"` | Teal sub-heading. |
| `"p"` | Body paragraph. `"bold": true` and `"color": "red"` (or `"navy"`, hex, etc.) optional. |
| `"gap"` | A blank spacer line (no text needed). |

## `reference_tabs[]`: extra reference tabs

For the "obscure but critical" layer: utility districts, permit offices, licensing boards.

| Key | Meaning |
|---|---|
| `name` | Tab name (≤31 chars). |
| `title` / `subtitle` | Header rows. |
| `headers` | Array of column headers. |
| `widths` | Optional array of column widths. |
| `rows` | Array of rows. A normal row is an **array of cell strings** matching `headers`. A section band is `{"band": "SECTION NAME"}`; it renders as a full-width grey divider. |
| `footnote` | Optional italic note under the table. |
| `row_height` | Optional per-row height (default 52). |

## Tips

- Keep `serves_geo` values consistent (`"Yes"` / `"Confirm ..."`) so the summary counts and
  colors are meaningful.
- Prefix every public-source addition's `source` with `"ADDED"`; the summary block counts
  those so the user can see how much came from the community vs. filled in.
- Counts on the Top Picks tab are computed at build time and written as values, so the file
  is correct on open with no recalc step. (If you later add live formulas, run the xlsx
  skill's `recalc.py`.)
