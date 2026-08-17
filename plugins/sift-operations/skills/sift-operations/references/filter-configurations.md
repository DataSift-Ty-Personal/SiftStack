# Filter Block Configurations

Quick reference for exact filter block settings used across both Niche and Bulk Sequential presets. Use this when building any filter preset.

## Filter Block Types

### Any Lists (OR)
- **Purpose**: Include records that are on ANY of the selected lists
- **Logic**: OR — record matches if on at least one selected list
- **Common Use**: Select first-to-market or bulk lists by name

### Any Tags (OR)
- **Purpose**: Include records with ANY of the selected tags
- **Logic**: OR — record matches if it has at least one selected tag
- **Common Use**: `courthouse data`, `dataflik`, `probate`, `foreclosure`

### All Tags (AND)
- **Purpose**: Include/exclude records with ALL specified tags
- **Logic**: AND — all conditions must be true
- **Common Use with Do Not Include**: Exclude `return mail` tagged records

### Property Status
- **Purpose**: Filter by current property status
- **Include**: Show only records with selected statuses
- **Do Not Include**: Exclude records with selected statuses
- **Do Not Include → Any Statuses**: Exclude ALL records that have ANY status (shows only unstatused records)

### Call Attempts
- **Purpose**: Filter by number of call attempts
- **Min/Max**: Set range (e.g., Min: 0, Max: 0 = zero calls; Min: 1, Max: 3 = 1-3 calls)
- **Critical for**: Sequential preset progression

### Direct Mail Attempts
- **Purpose**: Filter by number of mail pieces sent
- **Min/Max**: Set range (e.g., Min: 0, Max: 0 = never mailed)
- **Critical for**: Mail stage presets

### Last Direct Mailed
- **Purpose**: Filter by when the last mail piece was sent
- **Prior to Date**: Records mailed MORE than X months/days ago
- **Common Use**: "Prior to Date → 1 month ago" for monthly mailing

### Last Called
- **Purpose**: Filter by when the last call was made
- **Prior to Date**: Records called MORE than X days ago
- **Common Use**: Ensuring enough time between call attempts

### Phone Statuses
- **Purpose**: Filter by phone number disposition
- **Include**: Show records with specific phone statuses
- **Do Not Include**: Exclude records with specific phone statuses
- **Do Not Include at least one phone**: Exclude if ANY phone matches
- **Values**: Correct, Correct DNC, Wrong, Wrong DNC, Dead, DNC

### Params & Others
- **Purpose**: Multi-field filter
- **Numbers**: Yes (has phones) / No (no phones)
- **Skiptraced**: Yes (been skip traced) / No (not skip traced)
- **Vacant Mailing**: Yes (vacant address) / No (occupied)

### Last Updated Field
- **Purpose**: Filter by when a specific field was last changed
- **Field**: Select which field (e.g., Status)
- **Date**: "Prior to X months ago"
- **Common Use**: Quarterly re-engagement (Prior to 3 months ago)

## Quick Reference: Niche Sequential Filter Summary

| Preset | Key Filters |
|---|---|
| 00. Needs Skipped | Lists + Tags + No Status + 0 Calls + No Numbers + Not Skiptraced |
| 01. Skipped No Numbers | Lists + Tags + No Numbers + Skiptraced |
| 02. Ready to Call | Lists + Tags + Exclude Statuses + 0 Calls + Has Numbers |
| 03-05. Follow Ups | Lists + Tags + Call Attempts X-X + Exclude Correct Phones |
| 06. Needs 1st Mail | 4+ Calls + 0 Mail + Not Vacant + Not Return Mail |
| 07. Mail Monthly | 1-12 Mail + Last Mailed 1+ Month Ago + Not Vacant + Not Return |
| 08. Vacant → DP | Vacant Mailing + Exclude Correct Phones |
| 09. Return Mail → DP | Return Mail Tag + 1+ Mail |
| 10. No Response → DP | 4+ Calls + 6-12 Mail + Exclude Correct Phones |
| 11. Not Interested | Status: Not Interested + Updated 3+ Months Ago + Has Numbers |

## Quick Reference: Bulk Sequential Filter Summary

| Preset | Key Filters |
|---|---|
| 00. Bulk Needs Skipped | Lists + No Status + 0 Calls + No Numbers + Not Skiptraced |
| 01. Bulk Skipped NN | Lists + No Numbers + Skiptraced |
| 02. Bulk Ready to Call | Lists + No Status + 0 Calls + Has Numbers |
| 03. Bulk Call Follow Up | Lists + No Status + 1-6 Calls + Has Numbers |
| 04. Bulk Needs 1st Mail | 7+ Calls + 0 Mail + Not Vacant |
| 05. Bulk Mail Monthly | 1-12 Mail + Last Mailed 1+ Month Ago + Not Vacant + Not Return |
| 06. Bulk Not Interested | Status: Not Interested + Updated 3+ Months Ago |
| 07. Exhausted CC → DP | No Status + Phone Status: Wrong/Dead/DNC |
| 08. Bulk Return Mail → DP | Return Mail Tag + 1+ Mail + Exclude Correct Phones |
