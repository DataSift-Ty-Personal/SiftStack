# Telnyx API Contract (verified) - caller-reputation build

Verified 2026-07-06 against Telnyx's live OpenAPI spec (team-telnyx/openapi spec3)
plus developers.telnyx.com, with an adversarial cross-check pass. This is the exact
contract the L1 (CDR) and L2 (Number Reputation + Remediation) clients code against.
ASCII hyphens only. If a field access fails in production, validate against a live
sample and update this doc - do NOT re-research from scratch.

Base URL: `https://api.telnyx.com/v2`. Auth on every call: header
`Authorization: Bearer <TELNYX_API_KEY>` (one v2 key for all surfaces).

---

## L1 - Detail Records (CDR) : own-traffic ASR / ALOC / short-call%

`GET /v2/detail_records`

Query params (JSON:API `filter[...]` style):
- `filter[record_type]` REQUIRED enum. Use `call-control` (Voice API) and/or
  `sip-trunking` (SIP trunk). Outbound dialer traffic (SmrtPhone) is one of these,
  NOT `webrtc`. If unsure which, query both and union. Wrong record_type = 0 rows.
- `filter[direction]` = `outbound`
- `filter[created_at][gte]` = `2026-07-05T00:00:00Z` AND `filter[created_at][lt]` =
  `2026-07-06T00:00:00Z` - explicit UTC window (preferred over `filter[date_range]=yesterday`,
  whose timezone is not pinned).
- `sort` = `-created_at`
- `page[number]` (>=1, default 1), `page[size]` (default 20, **MAX 50**)

Response (`data[]` rows + `meta`):
- `cli` = ORIGINATING / from number (our DID)
- `cld` = dialed / to number
- `call_sec` = connected/answered duration, INTEGER seconds, **0 for unanswered**
- `billed_sec` = billed duration, rounded UP to the billing increment (do NOT use for ALOC)
- `started_at`, `finished_at` = ISO 8601 Z timestamps
- `direction`, `record_type`, `connection_id`, `cost`, `rate`, `currency`
- `meta.total_pages`, `meta.page_number`, `meta.page_size`, `meta.total_results`

Metric math:
- answered = `call_sec > 0` (no explicit answer flag on this JSON surface)
- **ASR** = answered / total outbound rows
- **ALOC** = mean(`finished_at` - `started_at`) over answered rows (fractional seconds;
  do not trust `billed_sec`, it overstates short calls)
- **short%** = share of answered rows whose (`finished_at` - `started_at`) < 6s

Gotchas (from adversarial verify):
- Voice records are returned via `additionalProperties` and are NOT formally typed in
  the spec oneOf. Read every field with `.get()`; validate one live sample before trusting.
- `filter[cli]` (server-side filter by originating number) is UNCONFIRMED. **Group
  client-side by `cli`** instead of relying on the server filter.
- Pagination cap is 50/page; a full day spans many pages - loop until
  `page_number == total_pages` to get correct denominators.
- STIR/SHAKEN attestation per call is NOT on this JSON API. It is only a Mission
  Control CDR **CSV** column ("Stir Shaken" = A/B/C/Unavailable/Invalid). Treat as a gap.
- Records appear within a few minutes of call end. Run the "yesterday" pull after
  ~00:15 local, or lag the window an hour.
- No published numeric rate limit; add 429 backoff.

Alt surface (more rigorous, async CSV): `POST /v2/legacy/reporting/batch_detail_records/voice`
-> poll `/{id}` -> download `report_url`. Only surface with explicit `answer_timestamp`
+ `hangup_code`. Field catalog is UNCONFIRMED (came only from the OpenAPI spec); call
the `/voice/fields` helper at runtime rather than hardcoding. Not needed for v1.

---

## L2 - Number Reputation : carrier-grade spam score

Enterprise-scoped. One-time setup, then a cheap daily read.

### One-time setup (telnyx_setup.py)
1. `POST /v2/enterprises` (body: legal_name 3-64, doing_business_as, organization_type
   commercial|government|non_profit, organization_legal_type corporation|llc|partnership|nonprofit|other,
   country_code, jurisdiction_of_incorporation, website, fein, industry, number_of_employees,
   organization_contact{first_name,last_name,email,job_title,phone_number}, billing_contact,
   organization_physical_address, billing_address) -> `data.id` = **enterprise_id**. FREE.
2. `POST /v2/enterprises/{id}/reputation/loa` (optional body `signature{image_base64, signer_name}`)
   -> returns **application/pdf** (binary, not JSON). Sign it. FREE.
3. `POST /v2/documents` (multipart `file` = signed LOA PDF) -> `data.id` = **loa_document_id**. FREE.
4. `POST /v2/enterprises/{id}/reputation` (body: `loa_document_id`, `check_frequency` enum
   business_daily|daily|weekly|biweekly|monthly|never) -> **BILLABLE: triggers $100/mo base**.
5. Poll `GET /v2/enterprises/{id}/reputation` until **BOTH** `status == approved`
   AND `loa_status == approved` (two gates, not one). Vetting takes minutes.
   Re-upload a rejected LOA: `PATCH .../reputation/loa` {loa_document_id} (only while pending/rejected).
6. `POST /v2/enterprises/{id}/reputation/numbers` (body `phone_numbers[]`, MAX 100,
   US E.164, must be in your Telnyx inventory; atomic all-or-nothing). BILLABLE (adds).

### Daily read (telnyx_reputation.py)
- Free cached sweep: `GET /v2/enterprises/{id}/reputation/numbers?page[size]=250` (paginate).
- Single cached read: `GET /v2/enterprises/{id}/reputation/numbers/%2B18653241736`
  (URL-encode leading `+` as `%2B`).
- Fresh (BILLABLE): `GET .../numbers/%2B1...?fresh=true` OR batch
  `POST .../numbers/refresh` {phone_numbers[] up to 100} -> per-item success/error.
- Single-enterprise shorthand (drops the id segment): `/v2/reputation/numbers[/%2B1...]`.

Response `data.reputation_data`:
- `spam_risk` enum `low | medium | high | null` (docs use **medium**, not "med"; null = not scored)
- `spam_category` string|null (**null = clean**; non-null = a label is present)
- `maturity_score`, `connection_score`, `engagement_score`, `sentiment_score` (0-100|null)
- `last_refreshed_at` (ISO 8601|null)

Gotchas (from adversarial verify):
- **NO per-carrier / per-engine breakdown.** There are NO Hiya / First Orion / TNS fields.
  Telnyx surfaces only the AGGREGATE spam_risk + spam_category + 4 scores. (The handover's
  per-engine data model is aspirational; per-carrier status can only come from L4/manual.)
- **The FIRST GET on a never-scored DID auto-bills a fresh check.** Only reads AFTER the
  cache is populated are free. Cost order: associate -> let check_frequency populate cache
  -> then read. Do not assume the first cached GET is free.
- Two approval gates: `status` AND `loa_status` both == approved before numbers accept.
- `spam_risk` medium/high or `spam_category` non-null = degraded (trigger fresh recheck).
- URL-encode every leading `+` as `%2B` in the path.
- `check_frequency` set at enable time; a post-enable PATCH to change it is UNCONFIRMED - set it right the first time.
- Pricing: $100/mo per Enterprise base (on enable) + billed-per-query fresh checks; exact
  unit price not published (pay-as-you-go or contract). Telnyx cannot guarantee a label won't appear.

---

## L2 - Remediation : dispute / clear a flagged number

Async submit + poll. Enterprise-scoped.

- Submit: `POST /v2/enterprises/{id}/reputation/remediation`
  Body (additionalProperties:false - unknown keys rejected):
  - `phone_numbers` array E.164, **minItems 1, maxItems 2000**, each must belong to enterprise
  - `call_purpose` REQUIRED string, 1-2000 chars (e.g. "Outbound real estate offers to property owners.")
  - `contact_email` optional (<=255), `webhook_url` optional https (<=2048)
  -> **202 Accepted**, body `{ data: {...} }`:
  - `data.id` = remediation request id (poll with this)
  - `data.status` starts `pending`
  - `data.phone_numbers_count` / `phone_numbers_submitted` / `phone_numbers_ineligible`
  - `data.results` = null while pending
- Poll: `GET /v2/enterprises/{id}/reputation/remediation/{request_id}` -> `{ data: {...} }`
  - `data.status` enum: `pending | in_progress | completed | failed | cancelled`
  - `data.results` (null until ready; then all 5 buckets present, each an array<E.164>):
    `remediated` (cleared) | `not_flagged` (no action needed) | `requires_review` (still
    flagged, manual) | `ineligible` (rejected pre-submit, e.g. in cooldown) | `refused`
    (re-eval declined by the analytics network)
  - `data.tier1_completed_at`, `data.tier2_completed_at`
- List: `GET .../reputation/remediation` (slim items; use GET-by-id for full results).

Gotchas (from adversarial verify):
- **409 Conflict** if ANY submitted number already has an in-flight (not-completed) request.
  Poll/complete or wait before resubmitting that number.
- **14-day per-number cooldown** (from Telnyx Supplemental Terms, NOT the API). A number
  in cooldown does NOT error - it lands in `phone_numbers_ineligible` / the `ineligible`
  bucket. There is no day-count field; reconcile via those counters.
- `422` for bad E.164 or a number not owned by the enterprise; `404` enterprise/id not found.
- Webhook payload shape is UNDOCUMENTED - treat `webhook_url` as a wake-up trigger only;
  the GET poll is the authoritative result source.
- `phone_numbers_count` can exceed the sum of the 5 buckets (buckets omit cancelled).
- `requires_review` and `refused` are normal completed outcomes, not errors. No relabel guarantee.

---

## Sources
- Live OpenAPI: https://raw.githubusercontent.com/team-telnyx/openapi/master/openapi/spec3.json (and spec3.yml)
- CDR search: https://developers.telnyx.com/api/detail-records/search-detail-records
- Number Reputation: https://developers.telnyx.com/docs/branded-calling/number-reputation
- Remediation: https://developers.telnyx.com/docs/branded-calling/number-reputation/remediation
- Reputation Services Terms (14-day cooldown, no-guarantee): https://telnyx.com/terms/reputation-services
