# Comp package workbook format

One Excel workbook per subject. File name: `{Address}_Comp_Package.xlsx`. Zero em or en dashes anywhere. Flat headers (navy fill, white bold text), no decorative styling, tabular numerals via number formats (`$#,##0`).

## Sheets

1. **Summary**: subject specs (county card as truth), the four numbers (base ARV with band + comp count, upside ARV labeled with its verification condition, full gut, mid reno, cosmetic), wholesale MAO table off the BASE track, seller story (deed type, basis, distress signals), contract target.
2. **Sold Comps**: boundary-filtered, one row per sale: address, sold date, price, beds, baths, sqft, $/sf, condition bucket (color: green = renovated/retail, gold = distressed), Zestimate, listing URL. Renovated first, then average, then distressed. Flag any row needing county verification (LOT type at house price, new construction with missing sqft).
3. **Active**: current competition inside the boundary with DOM.
4. **Rehab Scenarios**: the three scenarios with totals and $/sf; note the tier, regional multiplier, and soft-cost assumption (3% permits + 10% contingency).
5. **ARV + Deal Math**: show the dual-track logic explicitly (which comps fed each track, the clamp applied, the flag if the same-bed set was thin).
6. **Buyer Targets**: ranked; boundary-active investors first (with the evidence: what they bought, when, for how much), then zip-level repeat buyers, then citywide names, then dispo marketplaces. Include the landlord-fallback rent math when relevant.

## Quality bar

- Every comp carries a source URL; anything surprising is verified against the county card before it ships
- The base ARV is always the conservative same-bed track; the upside is always labeled with what unlocks it
- State what was excluded and why (out-of-boundary backups clearly labeled)
