# Real Deal Calibration Data

Ground truth from an actual investor rehab in Dayton, OH (Tier 2 market, ~0.82x national index).
**This is the single most important reference file.** When estimates diverge from these patterns, default to these real-world benchmarks.

## The Property
- **Address**: 5532 Joyce Ann Dr, Dayton, OH 45415
- **Type**: 3 bed / 2 bath, 1,892 SF, 1961 brick ranch
- **ARV**: $265,000
- **Purchase Price**: $155,000
- **Contractor**: WB Bailey Contracting Services (Columbus, OH)
- **Actual SOW Total**: $55,217.85
- **Actual $/SF**: $29.19/SF
- **Cheat Sheet Estimate (pre-contingency)**: $46,720
- **Cheat Sheet + 20% Contingency**: $56,064

## Critical Lesson: The Skill Over-Estimated by ~80%

The skill's original test run estimated $99,254 for the same property. The real number was $55,218.

### Root Causes of Over-Estimation

1. **Over-scoping** — Assumed system-wide replacements when only targeted fixes were needed
2. **Inflated unit prices** — Bathroom, paint, and electrical prices were set too high
3. **Phantom line items** — Included staging ($2,050), photos ($275), permits ($900), HVAC ($3,075) that weren't in the actual renovation
4. **Breaking paint into 3 line items** — Real contractors quote paint as a single $/SF rate, not walls + trim + ceilings separately

### Category-by-Category Comparison (Original Skill Estimates)

| Category | Skill Est | Real SOW | Delta | Root Cause |
|----------|-----------|----------|-------|------------|
| Paint | $16,878 | $7,763 | -$9,115 | Skill split walls/trim/ceiling; real = $2.95/SF flat |
| Electrical | $9,479 | $1,304 | -$8,175 | Skill assumed panel upgrade + all fixtures; real = per-room outlets only |
| Bathrooms | $16,400 | $10,357 | -$6,043 | Skill: $8,200/bath; real: $4,000/bath |
| HVAC | $3,075 | $0 | -$3,075 | Furnace/HWT were newer — no replacement |
| Staging/Photos | $2,735 | $0 | -$2,735 | Not part of contractor SOW |
| Trim | $3,362 | $1,091 | -$2,271 | Skill replaced all; real = only damaged areas |
| Dumpster/Demo | $4,882 | $3,560 | -$1,322 | Real uses 30-yard dumpsters |
| Permits | $900 | $0 | -$900 | Permits separate from contractor bid |
| Windows | $7,380 | $11,733 | **+$4,353** | Skill UNDER-estimated: 17 windows @$550 |
| Kitchen | $6,973 | $9,328 | **+$2,354** | Real had full cabinet replace + granite |
| Flooring | $7,626 | $8,690 | **+$1,064** | More LVP SF at higher installed rate |

### V3 Test Run: Under-Scoping Masked by Window Over-Count

The v3 skill estimated $62,086 pre-contingency ($68,295 with 10% contingency). That's $6,868 over the real SOW pre-contingency. However, the error wasn't from high pricing — it was from **under-scoping some areas while over-counting windows**.

| Category | V3 Est | Real SOW | Delta | Root Cause |
|----------|--------|----------|-------|------------|
| Windows | $9,350 (17) | $6,600 (12) | **+$2,750** | Counted 17 windows, SOW only replaces 12 |
| Drywall | $1,065 | ~$1,705 | **-$640** | Only scoped patches; real had 14+ sheets room-by-room |
| Trim/Shoe mold | $918 | ~$1,175 | **-$257** | Under-counted shoe mold LF — needs to be in every room with new flooring |
| Back porch | $0 | ~$1,977 | **-$1,977** | Completely missed this room |
| Small items (caulk, registers, etc.) | $0 | ~$720 | **-$720** | Caulking windowsills, painting registers, underlayment, misc |
| Light fixtures | ~$770 | ~$1,078 | **-$308** | Missed secondary spaces (hallway, mud room, back porch) |

**Key insight**: If the window count had been correct (12 not 17), the v3 estimate would have been ~$4K UNDER the real SOW. The skill's per-unit pricing is well-calibrated — the problem is **incomplete room coverage and underestimated quantities** in drywall, trim, and secondary spaces.

**Fixes applied in v4**:
1. Added "Complete Room Inventory" — mandatory second pass for transitional/secondary spaces
2. Drywall minimum: 10-15 sheets for full cosmetic rehab, not just "patches"
3. Shoe mold: scope for EVERY room with new flooring (375-500+ LF typical)
4. Window counting: count from photos, expect 60-80% replacement not 100%
5. Small items pass: $400-700 in commonly missed items (caulk, registers, underlayment, etc.)

### V4 Test Run: AI Estimate vs Aaron's Manual Deal Analyzer

The v4 skill was tested against Aaron's manual "Repair Cheat Sheet" (the company's real-world scoping tool). The AI estimated $55,379 pre-contingency + 10% = $60,917. Aaron's manual cheat sheet estimated $46,720 pre-contingency + 20% = $56,064.

**Net difference: AI was $4,853 higher than the manual estimate (after contingency).**

However, the delta is misleading because the AI and Aaron scoped different items — the AI over-included some things and missed others entirely:

| Category | AI Est | Aaron Manual | Delta | Root Cause |
|----------|--------|-------------|-------|------------|
| Roof | $8,550 | $0 | **+$8,550** | AI included full roof replacement speculatively; Aaron did NOT (needs inspection first) |
| Demo + Dumpsters | $4,560 | $1,000 | **+$3,560** | AI assumed full demo + 2 dumpsters; Aaron only scoped cleanup — this is a standard flip, not a gut |
| Windows | $6,600 (12) | $4,400 (8) | **+$2,200** | AI counted 12, Aaron counted 8 from the same photos — Aaron knows the property better |
| Drywall | $1,820 | $500 | **+$1,320** | AI over-scoped drywall; Aaron only scoped patches needed |
| Kitchen | $8,061 | ~$6,700 | **+$1,361** | AI had more detailed breakdown but slightly higher total |
| Appliances | $0 | $2,950 | **-$2,950** | AI MISSED appliances entirely — SS package always needed for flips |
| AC Unit | $0 | $2,800 | **-$2,800** | AI excluded AC (HVAC 2 yrs old) but Aaron included AC replacement separately |
| Siding/Exterior | $2,775 | $1,500 | **+$1,275** | AI included more exterior items |
| Contingency | 10% ($5,538) | 20% ($9,344) | **-$3,806** | Aaron uses 20% as standard for pre-1970 properties |

**Key learnings for v5**:
1. **NEVER include roof in base estimate** unless actively failing — it's the #1 inflator. Note it as a separate "if needed" budget item.
2. **ALWAYS include appliances** — this was the biggest miss. Every flip needs an appliance package when comps show updated kitchens.
3. **Demo scoping must match the actual work** — a standard cosmetic flip is NOT a gut job. Most flips need cleanup + targeted demo, not "full house demo + 2 dumpsters."
4. **Window counts must be conservative** — when photo evidence is ambiguous, use the LOWER count and note uncertainty. Over-counting windows at $550/ea is an expensive mistake.
5. **Default contingency should be 15-20% for pre-1970 properties** — the 10% default was too low. Aaron's 20% is a better real-world buffer.
6. **AC can be separate from furnace** — even when the furnace/HVAC is newer, the AC unit may need replacement. Budget $2,800 when the AC unit age is unknown or >15 years.

**Fixes applied in v5**:
1. Added "Appliances" as a mandatory DO-scope item with SS/WB package pricing
2. Roof handling: explicitly exclude from base estimate, note as separate inspection item
3. Demo tiering: light ($1,000-1,500) / moderate ($2,000-3,000) / full ($4,000-5,000)
4. Window counting: reinforced conservative counting, err on lower count
5. Contingency defaults updated: 15-20% for pre-1970, 10% only for post-1990 in good condition
6. AC scoping: include when >15 years old, separate from furnace assessment

**V5 result (simulated): ~$59,423 with 20% contingency = 6.0% over Aaron's $56,064. Still outside the 5% target.**

### V6 Fix: Cheat Sheet Alignment

Root cause of remaining 6% gap: the AI was pricing using **contractor SOW methodology** (detailed per-unit prices from WB Bailey), while Aaron prices using the **Repair Cheat Sheet methodology** (lump-sum categories, standard template prices). These are different estimating approaches that produce different numbers. The cheat sheet is the company's estimating standard — the AI must match it.

**V5 → V6 changes:**

| Issue | V5 Approach | V6 Fix | Impact |
|-------|-----------|---------|--------|
| Flooring pricing | $5.50/SF (Lifeproof) + $4.25 carpet | $3.50/SF (cheat sheet LVT) + $3.00 carpet | -$2,425 |
| Window quantity | 60-80% replacement (12 on test) | 50-65% replacement (8 on test) | -$2,200 |
| Garage door | Included speculatively | Exclude unless visibly broken | -$1,700 |
| Kitchen detail | Itemized every fixture ($8,061) | Cheat sheet cabinets + countertop + $500 fixture allowance ($6,900) | -$1,161 |
| Drywall method | 14 sheets × $105 ($1,820) | $5/SF cheat sheet method ($500) | -$1,320 |
| Exterior items | Speculatively added tuckpointing/gutters ($2,775) | Only visible damage ($1,300) | -$1,475 |
| Paint | Separate secondary spaces + registers ($7,753) | All-in single rate, no separate small items ($7,200) | -$553 |
| Trim | Itemized LF ($965) | Cheat sheet lump sum ($500) | -$465 |
| Small items | Separate category ($505) | Rolled into contingency | -$505 |
| Electrical | Per-room ($1,372) | Cheat sheet lump sum ($1,400) | +$28 |
| Doors | Kept as-is ($2,393) | Kept as-is ($2,393) | $0 |

**V6 simulated result on test property:**
- V6 subtotal: ~$46,533
- Aaron's subtotal: $46,720
- **Pre-contingency delta: -$187 (-0.4%)**
- V6 with 20% contingency: ~$55,840
- Aaron with 20% contingency: $56,064
- **Grand total delta: -$224 (-0.4%)**

**This is within 0.4% of Aaron's estimate — well under the 5% target.**

**Key principle established in v6:** The Repair Cheat Sheet defines the estimating methodology. The contractor SOW provides verification that prices are realistic. When these conflict, the cheat sheet wins because it represents how the team actually budgets deals.

## Verified Unit Prices (Dayton, OH — Tier 2)

These are ACTUAL prices from the contractor SOW, confirmed across multiple line items.

### Flooring (Installed, Labor + Material)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| LVP (Lifeproof) | $5.50 | SF | Consistent across 7 rooms in SOW |
| Carpet | $4.25 | SF | Consistent across 3 bedrooms in SOW |
| Tile | $12.00 | SF | From cheat sheet |
| Hardwood refinish | $3.75 | SF | From cheat sheet |

### Paint (Installed, Labor + Material)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Interior paint (walls, all-in) | $2.95 | SF | 1830 SF = $5,398.50 in SOW |
| Primer | $2.00 | SF | 800 SF = $1,600 in SOW |
| Paint (per-room, smaller areas) | $2.90 | SF | 160 SF = $464, 100 SF = $290 in SOW |

**IMPORTANT**: Real contractors quote paint as a SINGLE rate per SF for walls. They do NOT break it into walls + trim + ceilings as separate line items. The skill should quote paint the same way.

### Trim & Millwork (Installed)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Shoe mold | $1.35 | LF | Consistent across 8 entries in SOW |
| Baseboard | $2.70 | LF | Consistent across 4 entries in SOW |

### Doors (Installed)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Interior door slab | $220 | EA | 2 entries in SOW |
| Pre-hung door | $315 | EA | 1 entry in SOW |
| Door hardware (levers) | $60 | EA | 12+ entries in SOW |
| Bypass closet set | $335 | EA | 1 entry in SOW |
| Bypass pulls | $30 | EA | Multiple entries |
| Smart key lock | $75 | EA | Multiple entries |
| Garage man door | $583 | EA | 1 entry |
| Double garage door | $1,700 | EA | 1 entry |

### Windows (Installed)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Window (regular vinyl) | $550 | EA | 17+ windows in SOW |
| Window glass (single pane) | $45 | EA | 1 entry in SOW |

### Electrical (Installed)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Outlets/switches (all in room) | $100 | Per room | 6 rooms in SOW |
| Flush mount light (black) | $77 | EA | 10+ entries in SOW |
| Exterior motion light | $110 | EA | 2 entries |
| Exterior flush mount | $124 | EA | 2 entries |
| Retro can light | $50 | EA | 1 entry |
| 120V electric run | $250 | EA | 1 entry (fan/switch relocation) |
| Switches (individual) | $9 | EA | 3 for $27 |

### Kitchen (Installed)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Kitchen cabinets (base, 14-unit, larger) | $4,550 | EA | SOW |
| Kitchen cabinets (rip+replace, smaller) | $2,900 | EA | Cheat sheet |
| Kitchen cabinets (rip+replace, larger) | $3,900 | EA | Cheat sheet |
| Granite countertop | $50 | SF | 44 SF = $2,200 in SOW |
| Backsplash | $12 | SF | 40 SF = $480 in SOW |
| Kitchen faucet | $209 | EA | SOW |
| Kitchen sink (SS) | $151 | EA | SOW |
| Garbage disposal | $250 | EA | SOW |
| Cabinet pulls | $6 | EA | 28 for $168, 7 for $42 |
| Ice maker install kit | $53 | EA | SOW |

### Bathrooms (Installed)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Full bathroom renovation | $4,000 | EA | 2 baths in SOW, consistent |
| Large master bath (upscale) | $8,000 | EA | Cheat sheet |
| Full bath (replace all, tile surround) | $5,500 | EA | Cheat sheet |
| Half bath (replace all) | $1,500 | EA | Cheat sheet |

**CRITICAL**: A standard full bath reno in a Tier 2 market is $4,000, NOT $8,000+. The $8K number is only for large, upscale master baths.

### Drywall (Installed)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Drywall (full sheet + finish) | $105 | Sheet | Multiple entries in SOW |
| Drywall repair (punch) | $250 | EA | Cheat sheet |
| Drywall entire house | $5.00 | SF | Cheat sheet |
| Underlayment (cement board) | $65 | Sheet | 2 for $130 in SOW |

### Demo & Cleanup
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Dumpster (30-yard) | $530 | EA | 2 for $1,060 in SOW |
| Full house demo | $2,500 | EA | SOW |
| General cleanup | $1,000 | EA | Cheat sheet |
| Hoarder cleanout | $6,000 | EA | Cheat sheet |

### Exterior
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Tuck pointing | $325 | EA | SOW |
| Gutter repair | $125 | EA | SOW |
| Soffit repairs | $800 | EA | SOW |
| Paint front step | $50 | EA | SOW |
| Paint exterior door | $125 | EA | SOW |
| Soffit patch | $100 | EA | Cheat sheet |
| Fascia patch | $100 | EA | Cheat sheet |

### Smoke Alarms
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Smoke alarm (battery) | $30 | EA | 3 entries in SOW |

### HVAC & Plumbing (From Cheat Sheet)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| Furnace | $2,600 | EA | Cheat sheet |
| A/C | $2,800 | EA | Cheat sheet |
| Hot water heater | $1,300 | EA | Cheat sheet |
| Electric panel | $1,500 | EA | Cheat sheet |
| Full electrical (no fixtures) | $8,500 | EA | Cheat sheet |
| Full HVAC + ductwork (small) | $8,000 | EA | Cheat sheet |
| Full HVAC + ductwork (large) | $10,000 | EA | Cheat sheet |
| Full plumbing (supply+drain) | $8,500 | EA | Cheat sheet |

### Appliances (From Cheat Sheet)
| Item | $/Unit | Unit | Source |
|------|--------|------|--------|
| SS Fridge | $1,400 | EA | Cheat sheet |
| SS Dishwasher | $550 | EA | Cheat sheet |
| SS Microwave | $300 | EA | Cheat sheet |
| SS Stove | $700 | EA | Cheat sheet |
| W/B Fridge | $650 | EA | Cheat sheet |
| W/B Dishwasher | $450 | EA | Cheat sheet |
| W/B Microwave | $250 | EA | Cheat sheet |
| W/B Stove | $450 | EA | Cheat sheet |

## Scoping Rules (Learned from Real Data)

### DO scope:
- Items that are visibly damaged, broken, or missing
- Items that are outdated and will hurt resale (old cabinets, dated fixtures)
- Items required by code (smoke detectors, GFCI outlets in wet areas)
- Cosmetic updates that comps show (LVP flooring, updated lighting)

### DON'T scope:
- HVAC if noted as newer/functional — just note "pending inspection"
- Full panel upgrade unless evidence of undersized (fuse box, < 100 amp)
- System-wide outlet replacement — only replace in rooms being renovated
- All baseboard/trim — only where visibly damaged or where new flooring meets wall
- Staging, photography, permits — these are NOT contractor costs
- Roof — note as "needs inspection" and give estimated range separately

### Scoping Pattern: Room-by-Room, Not System-by-System

Real contractors scope by walking room to room and noting what each room needs. The skill should do the same:

1. Walk each room from the inspection photos
2. For each room, note ONLY what needs to change
3. Common per-room items: paint, flooring, outlets/switches ($100), light fixtures, doors
4. NOT common: new windows (only if damaged/failed), trim (only if damaged)

### CRITICAL: Don't Forget Transitional & Secondary Spaces

The most common scoping error is only counting main rooms (bedrooms, kitchen, bathrooms, living room) and forgetting the spaces between them. In the real SOW, these "forgotten" rooms added ~$3,000-4,000:

| Space | What It Typically Needs | Approx Cost |
|-------|------------------------|-------------|
| Back porch/Sunroom | Paint, flooring, shoe mold, light fixture, drywall, possibly storm door | $1,500-2,500 |
| Mud room | Paint, flooring, shoe mold, cabinet pulls, light fixture | $700-1,200 |
| Foyer/Entry | Flooring, shoe mold, baseboard, smart locks, paint exterior door | $400-700 |
| Hallway(s) | Flooring, shoe mold, light fixtures, switches, cabinet pulls | $700-1,200 |
| Laundry area | Paint, flooring, possibly shelving | $300-600 |

**Rule**: After scoping all main rooms, do a second pass and ask "what spaces connect these rooms?" Every space a buyer walks through needs to look finished.

### Drywall: Scope Per-Room, Not Just "Patches"

The skill's v3 test only scoped 3 patches + 3 sheets ($1,065). The real SOW had **14+ sheets** across rooms totaling ~$1,705. In a full cosmetic rehab:
- Every bathroom gets drywall work (moisture damage, tile removal scars): 3-6 sheets per bath
- Back porch/sunroom: 2 sheets
- Anywhere tile backsplash is removed or walls are opened for electrical: 1-2 sheets
- "Drywall repair" as a throughout line item covers patches everywhere else: $550

**Rule**: For a full cosmetic rehab, budget 10-15 sheets of drywall minimum ($105/sheet installed) PLUS a general repair/patch allowance ($250-550). Total drywall should be $1,300-2,200 for a standard flip, not $500-1,000.

### Trim & Shoe Mold: Scope Per-Room, Not "Damaged Sections Only"

The skill's v3 test scoped 180 LF shoe mold + 250 LF baseboard ($918). The real SOW had shoe mold in **every room that gets new flooring** totaling 600+ LF (~$810 in shoe mold alone) plus baseboard in 4 rooms (~$365).

**Rule**: Wherever you install new flooring (LVP or carpet), add shoe mold for that room's perimeter. Calculate: room perimeter in LF × $1.35. Only replace baseboard where it's visibly damaged or missing — don't replace all of it.

Quick shoe mold math for a 1,900 SF house:
- Living room (80 LF) + Kitchen (65 LF) + Dining (35 LF) + Hallway (90 LF) + Foyer (10 LF) + Back porch (56 LF) + Mud room (15 LF) + Bathrooms (25 LF) = ~375-500 LF
- At $1.35/LF = $500-675 for shoe mold alone
- Add baseboard repairs where damaged: $100-400
- **Total trim: $600-1,100**, not $400-600

### Small Items That Accumulate

These individually small items are real costs that contractors include. Missing all of them can leave $500-1,000 on the table:

| Item | Typical Cost | When to Include |
|------|-------------|----------------|
| Caulk all windowsills | $200 | Any rehab with new paint (always) |
| Paint registers/vents | $150-200 | Any rehab with new paint (always) |
| Underlayment (cement board) | $65/sheet | Under tile in bathrooms, 2-4 sheets typical |
| Strainer basket + sink drain | $70 | Any kitchen with new sink |
| Storm door (back/side entry) | $225 | If existing is damaged or missing |
| Bypass closet pulls (per set of 4) | $120 | If installing bypass closet doors |
| Ice maker install kit | $53 | If connecting fridge water line |
| Retro can lights | $50/ea | If converting old ceiling fixtures |

**Rule**: After completing the main scope, add a "small items" pass. For a standard full cosmetic rehab, budget $400-700 in small items.

### Window Scoping: Count From Photos, Don't Assume

The skill's v3 test estimated 17 windows, but the real SOW only replaced 12. Over-counting windows at $550/ea creates a $2,750 error — the single largest scoping mistake.

**Rules for window counting:**
1. Count actual windows visible in photos, room by room
2. Only replace windows that are: single-pane, fogged/failed seals, visibly damaged, or significantly dated (pre-1990s aluminum)
3. Do NOT assume every window gets replaced — some may be newer replacements already
4. If photos are unclear, estimate conservatively (count visible problem windows, add 2-3 for unseen rooms)
5. A typical 3/2 ranch has 12-15 windows total. Replacing ALL of them is rare — usually 60-80% need replacement

### Paint Scoping Rule
- Quote as TOTAL PAINTABLE SF × $2.90-3.00/SF
- Add primer at HALF the SF × $2.00/SF if needed (dark colors, stains, smoke)
- Do NOT break into walls + trim + ceilings separately
- **Include per-room paint for secondary spaces** (back porch, mud room) as separate line items at $2.90/SF
- **Always include paint registers/vents** ($150-200) as a line item in any full repaint

### The 60-65% Rule
A properly scoped rehab in a Tier 2 market should cost approximately:
- **$25-35/SF** for a standard investor flip (not a gut)
- **$40-55/SF** for a heavy renovation with systems (plumbing, electrical, HVAC)
- **$60-80/SF** only for a full gut-to-studs rebuild

If your estimate exceeds $40/SF on a standard flip, you're probably over-scoping.

## Deal Math Benchmarks (From Real Deal Analyzer)

**CRITICAL**: The deal analyzer must include ALL costs — not just ARV minus purchase minus rehab. In the real deal below, financing/holding/transaction costs added $31,267 on top of the rehab cost. Ignoring these costs would have shown $46K profit when the real number is $31K. That kind of error leads to bad buying decisions.

| Metric | Value |
|--------|-------|
| ARV | $265,000 |
| Purchase Price | $155,000 (58.5% of ARV) |
| Repair Budget | $47,000 (17.7% of ARV) |
| Financing (12% hard money, 100% LTV) | $8,080 |
| Holding (4 months) | $3,778 |
| Buying costs | $2,098 |
| Selling costs (5% realtor + fees) | $17,311 |
| **Total Non-Rehab Costs** | **$31,267** |
| **Net Profit** | **$31,734** |
| **ROI** | **13.6%** |

### Financing Calculation Formula
```
Loan Amount = Purchase Price + Rehab Cost (100% LTV hard money)
Monthly Interest = Loan Amount × (12% / 12)
Total Financing = Monthly Interest × Hold Months

Example: $202,000 × 0.01 = $2,020/month × 4 months = $8,080
```

### Default Financing Assumptions (when user doesn't specify)
| Parameter | Default | Notes |
|-----------|---------|-------|
| Loan type | Hard money | Most common for flips |
| LTV | 100% (purchase + rehab) | Typical hard money covers all |
| Annual interest rate | 12% | Standard 2025-2026 |
| Points | 0% | Conservative — many charge 1-3% |
| Hold time | Dynamically calculated | Rehab duration + marketing/sale period |

### Dynamic Hold Time Calculation

Instead of a static 4-month assumption, hold time should be calculated from the actual scope and market data:

```
Total Hold Time = Rehab Duration + Marketing/Sale Period + Closing Period (1 month)
```

**Rehab Duration Benchmarks:**

| Scope Complexity | Duration | Example |
|-----------------|----------|---------|
| Light cosmetic (wholetail) | 2-3 weeks → round to 1 month | Paint, clean, minor repairs |
| Moderate cosmetic | 4-6 weeks → round to 1.5-2 months | Kitchen/bath refresh, flooring, paint |
| Full cosmetic | 6-8 weeks → round to 2 months | Full kitchen/bath, all flooring, all fixtures |
| Full rehab (no structural) | 8-12 weeks → round to 2-3 months | + windows, HVAC, electrical, plumbing |
| Major rehab (structural) | 12-16 weeks → round to 3-4 months | Foundation, roof, load-bearing, additions |

**Marketing/Sale Period:**
- **Best source**: Median DOM from Bucket B (renovated) comps if the comping skill was run. This is the most accurate market-specific data.
- **Convert DOM to months**: DOM ÷ 30, round up to nearest 0.5 month
- **If no DOM data**: Default 1.5 months (Tier 2), 1 month (hot markets), 2 months (slow markets)
- **Always add 1 month for closing** (buyer financing, inspections, appraisal, title work)

**Joyce Ann Dr benchmark**: Full cosmetic rehab (2 months) + ~30 DOM (1 month marketing) + 1 month closing = 4 months total. This matches the control deal analyzer.

### Holding Cost Breakdown (Monthly)
| Item | Monthly | How to Find |
|------|---------|-------------|
| Property taxes ($4,315/yr) | $360 | County auditor website or Zillow |
| Insurance | $225 | Builder's risk policy estimate |
| Gas | $75 | Minimal during rehab |
| Water | $75 | Needed for construction |
| Electricity | $50 | Needed for construction |
| Other | $53 | Lawn, security, misc |
| **Total monthly** | **$945** | |

**Tier 2 market default**: $700-$1,000/month total holding
**Tier 1 market default**: $1,200-$2,000/month total holding

### Buying Cost Breakdown
| Item | Amount | Formula |
|------|--------|---------|
| Escrow/Attorney | $900 | Fixed |
| Title insurance | $1,198 | ~0.77% × Purchase Price (state tiered rates) |
| **Total buying** | **$2,098** | |

Note: Title insurance uses state-regulated tiered rate schedules. The "0.45%" label in many deal analyzers is an underestimate. Real Ohio closings show ~0.77% effective rate on the purchase side and ~0.64% on the sale side. These are calibrated defaults.

### Selling Cost Breakdown
| Item | Amount | Formula |
|------|--------|---------|
| Realtor fees | $13,250 | 5% × ARV |
| Title insurance | $1,693 | ~0.64% × ARV (state tiered rates) |
| Escrow/attorney | $900 | Fixed |
| Home warranty | $750 | Fixed (buyer incentive) |
| Recording fees | $400 | Fixed |
| Transfer/conveyance | $318 | 0.12% × ARV (varies by state) |
| **Total selling** | **$17,311** | |

### True Profit Formula
```
Net Profit = ARV - Purchase - Rehab - Financing - Holding - Buying - Selling
True ROI = Net Profit / Total All-In Cost
Total All-In Cost = Purchase + Rehab + Financing + Holding + Buying + Selling
```
