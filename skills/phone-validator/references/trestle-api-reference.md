# Trestle Phone Validation API Reference

## Endpoint

```
GET https://api.trestleiq.com/3.0/phone_intel
```

## Authentication

Header: `x-api-key: YOUR_API_KEY`

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `phone` | Yes | Phone number to validate (US 10-digit, 11-digit with country code, or E.164) |
| `add_ons` | No | Comma-separated add-ons. Currently: `litigator_checks` |

## Example Request

```bash
curl --location --request GET \
  'https://api.trestleiq.com/3.0/phone_intel?phone=2069735100&add_ons=litigator_checks' \
  --header 'x-api-key: YOUR_API_KEY' \
  --header 'Accept: application/json'
```

## Example Response

```json
{
  "id": "Phone.3dbb6fef-a2df-4b08-cfe3-bc7128b6f5b4",
  "phone_number": "2069735100",
  "is_valid": true,
  "activity_score": 57,
  "country_calling_code": "1",
  "country_code": "US",
  "country_name": "United States",
  "line_type": "NonFixedVOIP",
  "carrier": "Level 3 Communications, LLC",
  "is_prepaid": false,
  "add_ons": {
    "litigator_checks": {
      "phone.is_litigator_risk": true
    }
  },
  "error": null,
  "warnings": null
}
```

## Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique identifier for this query |
| `phone_number` | string | The queried phone number |
| `is_valid` | boolean | Whether the number is a valid phone format |
| `activity_score` | integer (0-100) | Phone activity/connectivity score |
| `country_calling_code` | string | International dialing code (e.g., "1" for US) |
| `country_code` | string | ISO alpha-2 country code |
| `country_name` | string | Full country name |
| `line_type` | string | Phone line classification (see below) |
| `carrier` | string | Telecom carrier/provider name |
| `is_prepaid` | boolean | Whether the phone is prepaid |
| `add_ons` | object | Results from requested add-ons |
| `error` | object/null | Error details if partial failure |
| `warnings` | string/null | Warning messages |

## Line Type Values

| Value | Description |
|-------|-------------|
| `Mobile` | Cell phone |
| `Landline` | Traditional landline |
| `FixedVOIP` | Fixed Voice over IP (cable phone, business VOIP) |
| `NonFixedVOIP` | Non-fixed VOIP (Google Voice, TextNow, etc.) |
| `Tollfree` | 800/888/877/etc. toll-free number |
| `Premium` | Premium-rate number |
| `Voicemail` | Voicemail-only service |

## Activity Score Interpretation

| Score Range | Meaning |
|-------------|---------|
| 70-100 | High confidence the phone is connected, assigned, and actively used |
| 50-69 | Insufficient data to predict — could go either way |
| 50 | Trestle has no signals for this number |
| 30-49 | Inconsistent activity — may be disconnected |
| 0-29 | High confidence disconnected or completely inactive |

Score is based on 12 months of data including carrier network signals, cross-carrier
connectivity data, and call frequency patterns. Updates within 15 minutes of carrier changes.

## Rate Limits

HTTP 429 = rate limited. Back off and retry with exponential delay.

## Error Codes

| HTTP Code | Meaning |
|-----------|---------|
| 200 | Success |
| 400 | Bad request (invalid phone format) |
| 403 | Invalid or deactivated API key |
| 429 | Rate limit exceeded |

## Free Trial

New signups at https://trestleiq.com get 25 free queries per product within 14 days.
