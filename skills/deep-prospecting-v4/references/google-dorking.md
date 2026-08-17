# Google Dorking for Deep Prospecting

Google dorking uses advanced search operators to find specific information about property owners. Use these queries during L1/L2 research but **do not list the queries in the deliverable**.

## Core Operators

| Operator | Purpose | Example |
|----------|---------|---------|
| `site:` | Search specific website | `site:legacy.com "John Smith"` |
| `intitle:` | Words in page title | `intitle:obituary "John Smith"` |
| `filetype:` | Specific file types | `filetype:pdf "123 Main St"` |
| `"exact phrase"` | Exact match | `"John A Smith" Knoxville` |
| `-exclude` | Remove results | `"John Smith" -facebook` |

## Owner Name Searches

**Basic owner search:**
```
"John A Smith" Knoxville TN
```

**Obituary search:**
```
site:legacy.com "John Smith" Knoxville
site:newspapers.com "John Smith" obituary
intitle:obituary "John Smith" Tennessee
```

**Property records search:**
```
"John Smith" "123 Main Street" deed
site:tn.gov "John Smith" property
filetype:pdf "John Smith" deed Knox County
```

## Address-Based Searches

**Property address search:**
```
"123 Main Street" Knoxville owner
"123 Main St" Knox County deed
```

**Tax records:**
```
site:kgis.org "123 Main Street"
"parcel" "123 Main" Knox County
```

## Entity/LLC Searches

**Secretary of State filings:**
```
site:sos.tn.gov "ABC Holdings LLC"
"ABC Holdings" registered agent Tennessee
```

**Business records:**
```
"ABC Holdings LLC" member manager
"ABC Holdings" annual report Tennessee
```

## Court/Legal Searches

**Probate/Estate:**
```
site:tncourts.gov "John Smith" probate
"Estate of John Smith" Knox County
"John Smith" executor administrator
```

**Civil records:**
```
"John Smith" Knox County civil docket
site:tncourts.gov "John Smith" case
```

## Genealogy Searches

**Family connections:**
```
"John Smith" "Mary Smith" married Knoxville
"John Smith" children obituary
site:findagrave.com "John Smith" Tennessee
```

**Historical records:**
```
site:ancestry.com "John Smith" Knox County
"John Smith" city directory Knoxville 1990
```

## Tips for Effective Dorking

1. **Start broad, then narrow** - Begin with name + city, add specifics as needed
2. **Try name variants** - John, Johnny, J., middle initials
3. **Use quotes for exact phrases** - Reduces noise significantly
4. **Combine operators** - `site:legacy.com intitle:obituary "John Smith"`
5. **Check multiple years** - Historical records may use different addresses
6. **Search maiden names** - For female owners, try both married and maiden
7. **Include middle initials** - Helps distinguish common names
