"""Send run summary notifications to Slack or Discord via webhook.

Works with both Slack incoming webhooks and Discord webhooks (using the
/slack compatibility endpoint). Set SLACK_WEBHOOK_URL in .env.

Discord webhook URLs should use the /slack suffix:
  https://discord.com/api/webhooks/{id}/{token}/slack
"""

import json
import logging
import os
import time
from datetime import datetime

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Error & Warning Notifications ────────────────────────────────────


def _is_discord(webhook_url: str) -> bool:
    return "discord.com" in (webhook_url or "")


def _send_webhook(text: str, webhook_url: str | None = None, blocks: list | None = None) -> bool:
    """Send a message to the configured Slack/Discord webhook.

    blocks (Slack Block Kit, e.g. inline image blocks) are attached when provided
    and the target is Slack. Discord's /slack compatibility endpoint ignores
    blocks, so callers fall back to text links there.
    """
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return False
    payload: dict = {"text": text}
    if blocks and not _is_discord(webhook_url):
        payload["blocks"] = blocks
    for attempt in range(2):
        try:
            resp = requests.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            # Honor Slack/Discord rate limiting once before giving up.
            if resp.status_code == 429 and attempt == 0:
                try:
                    wait = float(resp.headers.get("Retry-After", "1") or 1)
                except (TypeError, ValueError):
                    wait = 1.0
                time.sleep(min(wait, 5))
                continue
            if resp.status_code not in (200, 204):
                logger.warning("Webhook post failed: HTTP %s", resp.status_code)
                return False
            return True
        except Exception:
            return False
    return False


def notify_error(
    step: str,
    error: Exception | str,
    *,
    context: str = "",
    webhook_url: str | None = None,
) -> bool:
    """Send an error alert to Slack/Discord.

    Args:
        step: Pipeline step that failed (e.g., "Smarty Standardization").
        error: The exception or error message.
        context: Optional extra context (run_id, record count, etc.).
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":rotating_light: *SiftStack Pipeline Error*",
        f"*Step:* {step}",
        f"*Error:* {error}",
    ]
    if context:
        lines.append(f"*Context:* {context}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    text = "\n".join(lines)
    sent = _send_webhook(text, webhook_url)
    if sent:
        logger.info("Error notification sent to Slack: %s — %s", step, error)
    else:
        logger.warning("Could not send error notification (no webhook or send failed)")
    return sent


def notify_warning(
    message: str,
    *,
    context: str = "",
    webhook_url: str | None = None,
) -> bool:
    """Send a warning alert to Slack/Discord.

    Args:
        message: Warning description.
        context: Optional extra context.
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":warning: *SiftStack Warning*",
        f"{message}",
    ]
    if context:
        lines.append(f"*Context:* {context}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    return _send_webhook("\n".join(lines), webhook_url)


def notify_preflight_failure(
    failures: list[str],
    *,
    webhook_url: str | None = None,
) -> bool:
    """Send a preflight check failure alert.

    Args:
        failures: List of failed check descriptions.
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":no_entry: *SiftStack Preflight Failed*",
        f"*{len(failures)} check(s) failed:*",
    ]
    for f in failures:
        lines.append(f"  - {f}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("Pipeline did not start. Fix the above and re-run.")

    return _send_webhook("\n".join(lines), webhook_url)


def _count_by_field(notices: list[NoticeData], field: str) -> dict[str, int]:
    """Count notices grouped by a field value."""
    counts: dict[str, int] = {}
    for n in notices:
        val = getattr(n, field, "") or "unknown"
        counts[val] = counts.get(val, 0) + 1
    return counts


def _upcoming_auctions(notices: list[NoticeData], days: int = 7) -> list[dict]:
    """Find notices with auction dates in the next N days."""
    now = datetime.now()
    upcoming = []
    for n in notices:
        if not n.auction_date:
            continue
        try:
            auction_dt = datetime.strptime(n.auction_date, "%Y-%m-%d")
            delta = (auction_dt - now).days
            if 0 <= delta <= days:
                upcoming.append({
                    "address": n.address,
                    "city": n.city,
                    "date": n.auction_date,
                    "days_out": delta,
                    "type": n.notice_type,
                })
        except ValueError:
            continue
    return sorted(upcoming, key=lambda x: x["days_out"])


def build_summary(
    notices: list[NoticeData],
    *,
    upload_result: dict | None = None,
    elapsed_min: float = 0,
    api_cost: float = 0,
    cost_breakdown: dict | None = None,
    csv_link: str | None = None,
    pdf_links: list[tuple[str, str]] | None = None,
) -> str:
    """Build a plain-text run summary for Slack/Discord.

    Args:
        notices: All notices from this run.
        upload_result: DataSift upload result dict (optional).
        elapsed_min: Pipeline elapsed time in minutes.
        api_cost: Estimated Haiku API cost for this run (legacy, use cost_breakdown).
        cost_breakdown: Dict of service -> cost, e.g. {"2Captcha": 0.09, "Tracerfy": 0.26}.
    """
    total = len(notices)
    by_county = _count_by_field(notices, "county")
    by_type = _count_by_field(notices, "notice_type")

    deceased = [n for n in notices if n.owner_deceased == "yes"]
    deceased_count = len(deceased)
    high_conf = sum(1 for n in deceased if n.dm_confidence == "high")
    med_conf = sum(1 for n in deceased if n.dm_confidence == "medium")
    low_conf = sum(1 for n in deceased if n.dm_confidence == "low")
    estate = sum(
        1 for n in deceased
        if n.decision_maker_relationship
        and "estate" in n.decision_maker_relationship.lower()
    )

    upcoming = _upcoming_auctions(notices)

    lines = [
        f"*SiftStack - Daily Report ({datetime.now().strftime('%Y-%m-%d')})*",
        "",
        f"*New notices scraped:* {total}",
    ]

    # County breakdown
    county_parts = [f"{v.title()}: {c}" for v, c in sorted(by_county.items())]
    if county_parts:
        lines.append(f"  {' | '.join(county_parts)}")

    # Type breakdown
    type_parts = [f"{t}: {c}" for t, c in sorted(by_type.items())]
    if type_parts:
        lines.append(f"  {' | '.join(type_parts)}")

    lines.append("")

    # Deceased owners
    if deceased_count > 0:
        pct = round(deceased_count / total * 100) if total else 0
        lines.append(f"*Deceased owners found:* {deceased_count} ({pct}%)")
        lines.append(f"  High confidence DM: {high_conf}")
        lines.append(f"  Medium confidence: {med_conf}")
        if low_conf:
            lines.append(f"  Low confidence: {low_conf}")
        if estate:
            lines.append(f"  Estate fallback: {estate}")
    # When deep prospecting is off (lean daily), deceased_count is 0 and this
    # block is simply omitted — no "Deceased owners: 0" noise.

    # Upload result
    if upload_result:
        lines.append("")
        if upload_result.get("success"):
            lines.append(
                f"*Uploaded to DataSift:* {upload_result.get('records_uploaded', total)} records"
            )
        else:
            lines.append(
                f"*DataSift upload FAILED:* {upload_result.get('message', 'unknown error')}"
            )

    # Upcoming auctions
    if upcoming:
        lines.append("")
        lines.append(f"*Upcoming auctions (next 7 days):* {len(upcoming)}")
        for a in upcoming[:5]:
            lines.append(f"  {a['address']}, {a['city']} - {a['date']} ({a['days_out']}d)")
        if len(upcoming) > 5:
            lines.append(f"  ... and {len(upcoming) - 5} more")

    # Pipeline stats
    lines.append("")
    stats = []
    if elapsed_min > 0:
        stats.append(f"Pipeline: {elapsed_min:.0f} min")
    if api_cost > 0 and not cost_breakdown:
        stats.append(f"Haiku API: ${api_cost:.2f}")
    if stats:
        lines.append(" | ".join(stats))

    # File links (CSV + deep-prospecting PDFs)
    if csv_link or pdf_links:
        lines.append("")
        lines.append("*Files*")
        if csv_link:
            lines.append(f"  CSV: <{csv_link}|Download>")
        if pdf_links:
            lines.append(f"  PDFs ({len(pdf_links)}):")
            for addr, url in pdf_links[:10]:
                lines.append(f"    <{url}|{addr}>")
            if len(pdf_links) > 10:
                lines.append(f"    ... and {len(pdf_links) - 10} more")

    # Cost breakdown
    if cost_breakdown:
        total_cost = sum(cost_breakdown.values())
        lines.append("")
        lines.append(f"*Estimated run cost:* ${total_cost:.2f}")
        for service, cost in cost_breakdown.items():
            if cost > 0:
                lines.append(f"  {service}: ${cost:.2f}")

    return "\n".join(lines)


def send_slack_notification(
    notices: list[NoticeData],
    *,
    webhook_url: str | None = None,
    upload_result: dict | None = None,
    elapsed_min: float = 0,
    api_cost: float = 0,
    cost_breakdown: dict | None = None,
    csv_link: str | None = None,
    pdf_links: list[tuple[str, str]] | None = None,
) -> bool:
    """Send a run summary to Slack/Discord webhook.

    Args:
        notices: All notices from this run.
        webhook_url: Slack/Discord webhook URL (defaults to SLACK_WEBHOOK_URL env).
        upload_result: DataSift upload result dict.
        elapsed_min: Pipeline time in minutes.
        api_cost: Estimated API cost (legacy, use cost_breakdown).
        cost_breakdown: Dict of service -> cost for itemized cost reporting.

    Returns:
        True if notification sent successfully.
    """
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("No SLACK_WEBHOOK_URL set, skipping notification")
        return False

    text = build_summary(
        notices,
        upload_result=upload_result,
        elapsed_min=elapsed_min,
        api_cost=api_cost,
        cost_breakdown=cost_breakdown,
        csv_link=csv_link,
        pdf_links=pdf_links,
    )

    sent = _send_webhook(text, webhook_url)
    if sent:
        logger.info("Slack notification sent successfully")
    else:
        logger.error("Failed to send Slack notification")
    return sent


# ── Per-record Sift upload package (details + inline notice screenshots) ──


def _fmt_date(d: str) -> str:
    """Normalize a date string to M/D/YYYY (no leading zeros), platform-safe."""
    s = (d or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return f"{dt.month}/{dt.day}/{dt.year}"
        except ValueError:
            continue
    return s


def _record_detail_text(n: NoticeData) -> str:
    """The Sift-upload details for one record, as Slack mrkdwn."""
    # Slack <url|label> links break on <, >, | in the label — sanitize the address.
    label = (n.address or "").replace("<", " ").replace(">", " ").replace("|", "-").strip()
    title = f"<{n.source_url}|{label}>" if (n.source_url and label) else (label or "(no address)")
    loc = " ".join(p for p in [n.city, n.state, n.zip] if p)
    head = f"*{title}*" + (f"\n{loc}" if loc else "")
    meta = " | ".join(p for p in [
        (n.notice_type or "").replace("_", " ").title() or None,
        n.county or None,
        f"Owner: {n.owner_name}" if n.owner_name else None,
    ] if p)
    dates = " | ".join(p for p in [
        f"Added {_fmt_date(n.date_added)}" if n.date_added else None,
        f"Sale {_fmt_date(n.auction_date)}" if n.auction_date else None,
    ] if p)
    return "\n".join(p for p in [head, meta, dates] if p)


def build_record_blocks(notices: list[NoticeData]) -> list[dict]:
    """Slack Block Kit blocks: per record, a details section plus an inline
    notice-screenshot image when the screenshot is hosted at a public https URL.
    """
    blocks: list[dict] = []
    for n in notices:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _record_detail_text(n)}})
        url = getattr(n, "notice_screenshot_url", "") or ""
        if url.startswith("https://"):
            blocks.append({
                "type": "image",
                "image_url": url,
                "alt_text": (n.address or "notice")[:150],
            })
        blocks.append({"type": "divider"})
    return blocks


def send_record_package(
    notices: list[NoticeData],
    *,
    webhook_url: str | None = None,
    batch_size: int = 8,
    max_records: int = 64,
) -> bool:
    """Post the per-record Sift upload package (details + inline notice
    screenshots) to Slack, chunked to stay under Block Kit / message limits. On
    Discord (no Block Kit support) it falls back to text lines with clickable
    screenshot links. Records without a hosted screenshot still appear (details
    only).

    Caps the number of records posted (max_records) so a large/historical run
    can't spam the channel; the full list is always in the CSV. Paces sends ~1/s
    to respect Slack webhook rate limits. Returns True if every batch sent.
    """
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url or not notices:
        return False
    recs = list(notices)
    total = len(recs)
    overflow = max(0, total - max_records)
    recs = recs[:max_records]
    discord = _is_discord(webhook_url)
    ok = True
    sent_any = False
    for i in range(0, len(recs), batch_size):
        if sent_any:
            time.sleep(1)  # stay under the ~1 msg/sec webhook rate limit
        batch = recs[i:i + batch_size]
        header = f"*Records for Sift upload ({i + 1}-{min(i + batch_size, len(recs))} of {total})*"
        if discord:
            lines = [header]
            for n in batch:
                lines.append(_record_detail_text(n))
                url = getattr(n, "notice_screenshot_url", "") or ""
                if url.startswith("https://"):
                    lines.append(f"Notice screenshot: {url}")
                lines.append("")
            ok = _send_webhook("\n".join(lines), webhook_url) and ok
        else:
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": header}},
                {"type": "divider"},
            ] + build_record_blocks(batch)
            ok = _send_webhook(header, webhook_url, blocks=blocks) and ok
        sent_any = True
    if overflow:
        time.sleep(1)
        _send_webhook(
            f"_...and {overflow} more record(s) not shown here; see the CSV for the full list._",
            webhook_url,
        )
    return ok
