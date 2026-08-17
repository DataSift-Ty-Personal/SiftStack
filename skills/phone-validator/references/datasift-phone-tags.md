# DataSift / REISift Phone Tags Reference

## What Are Phone Tags?

Phone tags are customizable labels applied to individual phone numbers within records.
They're separate from Phone Status (Correct, Wrong, No Answer, DNC, Dead) and Phone
Type (Mobile, Landline, VoIP, Unknown).

Tags are used to categorize numbers by source, quality, relationship, or priority tier.

## CSV Format for Phone Tag Upload

When uploading phone tags to DataSift/REISift, the CSV needs exactly two columns:

```csv
Phone Number,Phone Tag
3108972688,Dial First
3106066831,Mail Only
8053439329,Drop
```

### Column Requirements

- **Phone Number**: 10-digit US phone number (no dashes, no country code)
- **Phone Tag**: The tag string to apply (any text — tags are fully customizable)

### Upload Process

1. Go to **Upload** → **Update Data**
2. Select **"Tag phones by phone number"**
3. Upload the CSV
4. Map `Phone Number` → Phone Number field
5. Map `Phone Tag` → Phone Tag field
6. Complete the upload

### Key Behaviors

- Tags apply to ALL records sharing that phone number across your account
- Uploading ADDS tags — it does NOT replace existing tags
- To remove tags: separate upload with "Remove phone tags by phone number"
- Each phone number can have multiple tags
- Tags are visible in click-to-call interface (hover to see tags)

## Phone Type Values (for reference)

If also uploading phone types, acceptable values are:

- `Unknown`
- `Landline`
- `Mobile`
- `VoIP`

"Residential" must be changed to "Landline" and "Cell" to "Mobile" before uploading.

## Phone Status Values (for reference)

- Unknown (default)
- Correct
- Correct DNC
- Wrong
- Wrong DNC
- Dead
- No Answer
- DNC

## Dialer Integration Considerations

When sending tagged phones to an integrated dialer:

- You can filter by specific phone tags in the Send To interface
- **Important**: When selecting multiple phone tags, the filter requires the number to
  have ALL selected tags. So send one tier at a time.
- Example: Send "Dial First" in one batch, "Dial Second" in a separate batch

For non-integrated dialers, use Export → filter by phone tag.

## Creating Phone Tags

New tags can be created:
1. Via **Tags → Phone Tags** page → "Add New Phone Tag"
2. Automatically when uploaded via CSV (new tag names are created on upload)

## Tag Naming Best Practices

For phone validation tiers, keep names simple and action-oriented:

- **Good**: "Dial First", "Dial Second", "Mail Only", "Drop"
- **Good**: "Priority", "Standard", "Low Quality", "Dead"
- **Avoid**: Long names with scores (they clutter the interface)
- **Optional**: Include line type for extra context: "Dial First - Mobile"
