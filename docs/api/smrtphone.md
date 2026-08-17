# smrtPhone

Dialer and SMS. It has an API for some things, a web session for others, and
the split is not where you would guess.

- **Key:** `SMRTPHONE_API_KEY`, from Admin > API Tokens
- **Used by:** the three coach skills, the SMS agent
- **Verified:** 2026-08-10

## What has an API and what does not

| | API? |
|---|---|
| Send SMS (text) | yes |
| Send MMS (image) | **no**, browser only |
| Call logs and recordings | yes, via a DataTables endpoint |
| Phone numbers list | **no**, returns the SPA shell to a key |
| Webhooks in | yes |
| DNT/DNC write | undocumented |

## Sending text

```
POST phone.smrt.studio/sms/send
  header X-Auth-smrtPhone: <key>
  from, to, message
```

### The clean credential check

`POST /sms/send` with **no parameters**:

- valid key: `400 Missing required parameter(s): from, to, message`
- bogus key: `403`

That distinguishes a good key from a bad one and cannot send anything. Use it
as your health check.

**Do not probe `GET /dialerConfigs`.** It 405s on GET and serves the web app
HTML on POST regardless of key, so it proves nothing either way. That is an
hour you can skip.

### MMS has no API

Images go through the web app's compose UI, driven by Playwright. If you only
ever send text, you never need the browser path.

## Call logs and recordings

```
POST /logs/calls/filtered      (DataTables form post, cookie session)
```

Returns duration, disposition, caller, the CRM record link, and a **direct
recording URL on `rec.smrtphone.io`**. That URL is public once known and needs
no auth, which is what makes the coaching pipeline cheap.

Filter to calls over about 60 seconds with a recording. Voicemails and
wrong numbers are not calls, and grading them drags every average down.

**Logs purge after 30 days.** If you want a coaching history, pull and store.

## Finding the endpoints yourself

`/phoneNumbers` and `/callerIds` return the SPA shell to an API key. The route
that works is `POST /phoneNumbers/filtered`, another DataTables endpoint, and
its fields come back as HTML fragments that need parsing.

The trick that found it, and that works on the whole app:

```
GET /js/routing?callback=fos.Router.setData
```

That dumps roughly 1,187 named routes. It is a FOSJsRouting bundle and it is
the fastest way to discover what this app can actually do.

## Webhooks in

`smsIncoming`, `smsOutgoing`, `smsDeliveryCallback`, `addNumberToDNT`,
`addNumberToDNC`.

`smsIncoming` carries `smsId, from, to, message, date, callerIdName, userName,
contactName, source`.

**They are unsigned.** Use a secret URL path and an IP allowlist, and keep your
own event log, because their logs purge at 30 days.

### The finding that changes how you route replies

On a real send, **5 of 9 replies came from a different number than the one we
texted.** People answer from whichever phone is in their hand.

If you map only the number you sent to, most replies are unroutable. Map every
phone on a record before the send, not just the target.

## Sticky senders

A conversation must keep the same sending number. Switching mid-thread reads as
a spam farm to carriers and to the person.

Caller ID persists and does not auto-rotate, so rotation is your job. Keep
per-number daily volume low and never let one number carry a campaign.

## Without a smrtPhone account

The coach skills only need recordings and a rubric. Any dialer's export works.
See [coaching without a dialer
API](../setup/no-api-playbook.md#coaching-without-a-dialer-api).
