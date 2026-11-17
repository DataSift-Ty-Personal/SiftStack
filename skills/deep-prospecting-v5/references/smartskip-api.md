# SmartSkip API (app.smartskip.io): verified contract

Reverse-engineered from the live SPA and verified against the real account 2026-07-29.
`scripts/smartskip_trace.py` implements all of it. Base URL: `https://api.smartskip.io`.

## Auth
- `POST /auth/signin` `{email, password}` -> `{accessToken, refreshToken}`.
- **Access token lives ~15 minutes.** Refresh: `GET /auth/refresh` with
  `Authorization: Bearer <refreshToken>` -> new token pair. Refresh token lives ~30 days.
- All other calls: `Authorization: Bearer <accessToken>`.
- Session cache: `$SKIPTRACE_RUN_DIR/smartskip_session.json` (the script auto-refreshes,
  falls back to re-signin). Creds: `SMARTSKIP_EMAIL`/`SMARTSKIP_PASSWORD` env or
  `smartskip.config.json` (copy the `.example`).

## Bulk skip lifecycle (in order)
| Step | Call | Notes |
|---|---|---|
| 1. Upload | `POST /bulk-skip/mapping` multipart `file=<csv>` | -> `{bulkSkipId, parsed:{header: first-row sample}}` |
| 2. Fields | `GET /bulk-skip/fields` | -> `{required:{firstName,lastName,mailingAddress}, optional:{middleName, mailingCity/State/Zip, propertyAddress/City/State/Zip}}` |
| 3. Map | `POST /bulk-skip/fields/{bulkSkipId}` `{"schema":{apiField:"CSV Header",...}}` | -> preview rows. All three required fields must map |
| 4. Calculate | `POST /bulk-skip/calculate/{bulkSkipId}` | FREE. -> `{_id, entities, duplicates, fileName, status:"pending"}` |
| 5. Pay | `POST /bulk-skip/payment-intent` `{bulkSkipId, paymentMethodId}` | **BILLS the card.** -> `{status, paymentIntentId}` or `{clientSecret}` (3DS: must be completed in the browser). Billed per row that returns data ("charged less if no data is found") |
| 6. Poll | `GET /bulk-skip?sortField=createdAt&sortOrder=desc` | `{items:[{_id, status, ...}], count}`. **Unpaid orders do NOT appear in this list**, so keep the bulkSkipId from step 1. Statuses seen in the app: Processing / Completed / error / failed |
| 7. Download | `GET /bulk-skip/download/{bulkSkipId}?type=vertical\|horizontal` | CSV bytes; filename in Content-Disposition |

## Download formats = exactly what `parse_smartskip.py` already reads
- `vertical` = "Campaign Format" = parser Format 1 (one row per person, `Input Name` groups, up to 18 phones).
- `horizontal` = "CRM Format" = parser Format 2 (one row per property, `RELATIVE 1..50:` columns, 5 phones/relative).

## Payment / account plumbing
- `GET /payment/payment-method` -> saved cards `[{id: pm_..., brand, last4, isDefault}]`.
- `GET /wallet/balance` -> `{balance}` (wallet exists but bulk-skip pays by card payment-intent).
- `GET /user/profile` -> plan, wallet, stripeId. Stripe publishable key in the app bundle:
  `pk_live_51OtyHoHaBtbdBvHv...` (only needed if a 3DS confirm ever has to be driven outside the browser).

## Gotchas
- Steps 1-4 are free and idempotent to redo (each upload makes a NEW bulkSkipId; orphaned
  unpaid uploads are harmless and invisible in the list).
- A payment-intent `status` of `requires_payment_method` / `stripe_error` = card declined; fix in the app.
- Rate/row: $0.15 per hit (per cost-model.md); `entities` from calculate = the max billable rows.
