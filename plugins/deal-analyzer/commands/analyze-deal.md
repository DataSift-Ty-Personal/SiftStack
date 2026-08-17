---
description: Run full deal analysis on an investment property
argument-hint: [address or "use uploaded photos"]
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, WebSearch, WebFetch, Task
---

Run the complete deal-analyzer skill workflow for the property specified by $ARGUMENTS.

Load the deal-analyzer skill from ${CLAUDE_PLUGIN_ROOT}/skills/deal-analyzer/SKILL.md and follow its pipeline:

1. **Intake** — Gather property details from the argument, uploaded photos, and any context the user has provided. If only an address is given, research the property online (Zillow, Redfin, county records) to get GLA, bed/bath, year built, and any available photos.

2. **Photo Analysis** — If the user has uploaded property photos, analyze them room-by-room following the condition assessment checklist. If no photos are available, note this and widen contingency.

3. **Comp Analysis** — Execute the full comping workflow using the real-estate-comping skill methodology. Generate comp Excel workbook.

4. **Rehab Estimate** — Execute the full rehab estimation using the rehab-estimator skill methodology. Feed in comp findings (Bucket B finishes, renovation premium). Generate rehab Excel workbook.

5. **Deal Math** — Calculate all-in costs for both flip and wholetail exits including financing, holding, buying, and selling costs. Use default cost assumptions from SKILL.md unless the user provides specific numbers. Calculate hold time dynamically: Rehab Duration + Bucket B median DOM + 30 days closing.

6. **Offer Strategy** — Calculate MAO using both the 75% Rule and 70% Rule. Compare exits using the Profit Per Month tiebreaker: if both exits are profitable, recommend whichever has higher profit/month (with $20K absolute profit override for full rehab). Present both options — the investor decides.

7. **Deliverables** — Present the in-context summary (including ARV confidence level with every ARV figure), then link all generated files. The output MUST end with the disclaimer: "This analysis is for educational and estimation purposes only. It does not constitute investment, financial, or legal advice. Actual costs, values, and returns may differ materially. Verify all assumptions with local professionals before making investment decisions."

If the user hasn't provided a purchase price, ask for it before running deal math. If they don't have one yet, still run comping + rehab and provide both MAO figures (75% and 70%) as their target offer range.
