"""
Slack file uploader for SiftStack weekly CSVs.

Uses Slack's new 3-step upload API (files.upload was sunset Nov 2025):
  1. files.getUploadURLExternal — get a presigned upload URL
  2. POST file bytes to that URL
  3. files.completeUploadExternal — finalize + share to channel

Requirements:
  - Slack Bot Token with `files:write` and `chat:write` scopes
  - Channel ID where the bot is a member
  - Bot must be invited to the channel first (/invite @YourBotName)

Environment variables (in Modal secret "siftstack-secrets"):
  - SLACK_BOT_TOKEN: xoxb-... bot token (NOT the webhook URL)
  - SLACK_CHANNEL_ID: C0XXXXXXX channel ID for file uploads
  - SLACK_WEBHOOK_URL: existing webhook for summary messages (unchanged)
"""

import os
import logging
import requests
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


def _get_token() -> str:
    """Get Slack bot token from environment."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise ValueError(
            "SLACK_BOT_TOKEN not set. Add it to your Modal secret "
            "'siftstack-secrets'. Needs a bot token (xoxb-...) with "
            "files:write and chat:write scopes."
        )
    return token


def _get_channel_id() -> str:
    """Get target Slack channel ID from environment."""
    channel_id = os.environ.get("SLACK_CHANNEL_ID", "")
    if not channel_id:
        raise ValueError(
            "SLACK_CHANNEL_ID not set. Add the channel ID (e.g. C07XXXXXX) "
            "to your Modal secret 'siftstack-secrets'."
        )
    return channel_id


def upload_file_to_slack(
    file_path: str,
    filename: Optional[str] = None,
    initial_comment: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> dict:
    """
    Upload a single file to Slack using the 3-step API.

    Args:
        file_path: Local path to the file to upload.
        filename: Display name in Slack (defaults to basename of file_path).
        initial_comment: Message posted alongside the file.
        thread_ts: If provided, uploads as a thread reply to this message ts.

    Returns:
        Slack API response dict from completeUploadExternal.
    """
    token = _get_token()
    channel_id = _get_channel_id()
    headers = {"Authorization": f"Bearer {token}"}

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    file_bytes = path.read_bytes()
    display_name = filename or path.name

    # ── Step 1: Get upload URL ──────────────────────────────────────────
    resp = requests.post(
        f"{SLACK_API_BASE}/files.getUploadURLExternal",
        headers=headers,
        data={
            "filename": display_name,
            "length": len(file_bytes),
        },
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("ok"):
        error = data.get("error", "unknown")
        raise RuntimeError(f"files.getUploadURLExternal failed: {error}")

    upload_url = data["upload_url"]
    file_id = data["file_id"]
    logger.info(f"Got upload URL for {display_name} (file_id={file_id})")

    # ── Step 2: Upload file bytes ───────────────────────────────────────
    upload_resp = requests.post(upload_url, data=file_bytes)
    upload_resp.raise_for_status()
    logger.info(f"Uploaded {len(file_bytes):,} bytes for {display_name}")

    # ── Step 3: Complete upload + share to channel ──────────────────────
    complete_payload = {
        "files": [{"id": file_id}],
        "channel_id": channel_id,
    }
    if initial_comment:
        complete_payload["initial_comment"] = initial_comment
    if thread_ts:
        complete_payload["thread_ts"] = thread_ts

    complete_resp = requests.post(
        f"{SLACK_API_BASE}/files.completeUploadExternal",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=complete_payload,
    )
    complete_resp.raise_for_status()
    result = complete_resp.json()

    if not result.get("ok"):
        error = result.get("error", "unknown")
        raise RuntimeError(f"files.completeUploadExternal failed: {error}")

    logger.info(f"Shared {display_name} to channel {channel_id}")
    return result


def upload_weekly_csvs(
    csv_paths: dict[str, str],
    held_paths: dict[str, str],
    run_summary: str,
    thread_ts: Optional[str] = None,
) -> dict:
    """
    Upload all weekly CSVs to Slack after the summary message.

    Args:
        csv_paths: Dict of {list_name: file_path} for upload-ready CSVs.
                   e.g. {"Sheriff Sale": "/tmp/upload_ready_Sheriff_Sale.csv"}
        held_paths: Dict of {list_name: file_path} for held/cleaning CSVs.
                    e.g. {"Probate": "/tmp/HELD_Probate.csv"}
        run_summary: The summary text already posted to Slack (for context).
        thread_ts: Message timestamp of the summary message to thread under.
                   If None, files are posted as standalone messages.

    Returns:
        Dict with upload results: {"uploaded": [...], "held": [...], "errors": [...]}
    """
    results = {"uploaded": [], "held": [], "errors": []}

    # Upload ready-to-go CSVs
    for list_name, fpath in csv_paths.items():
        try:
            upload_file_to_slack(
                file_path=fpath,
                initial_comment=f"📋 *{list_name}* — upload-ready CSV",
                thread_ts=thread_ts,
            )
            results["uploaded"].append(list_name)
            logger.info(f"✓ Uploaded {list_name} CSV")
        except Exception as e:
            logger.error(f"✗ Failed to upload {list_name}: {e}")
            results["errors"].append({"list": list_name, "error": str(e)})

    # Upload held CSVs (tagged differently so Rick knows they need cleaning)
    for list_name, fpath in held_paths.items():
        try:
            upload_file_to_slack(
                file_path=fpath,
                initial_comment=f"⏸️ *{list_name}* — held for cleaning (not auto-uploaded to DataSift)",
                thread_ts=thread_ts,
            )
            results["held"].append(list_name)
            logger.info(f"✓ Uploaded held {list_name} CSV")
        except Exception as e:
            logger.error(f"✗ Failed to upload held {list_name}: {e}")
            results["errors"].append({"list": list_name, "error": str(e)})

    return results


def post_summary_and_get_ts(webhook_url: str, summary_text: str) -> Optional[str]:
    """
    Post the weekly summary via webhook and return the message timestamp
    for threading file uploads underneath it.

    NOTE: Standard incoming webhooks do NOT return a message timestamp.
    To get thread_ts, you need to use chat.postMessage with a bot token instead.
    This function uses the bot token if available, falling back to webhook.

    Args:
        webhook_url: Existing Slack webhook URL (fallback).
        summary_text: The formatted summary message.

    Returns:
        Message timestamp (ts) if posted via bot token, None if via webhook.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel_id = os.environ.get("SLACK_CHANNEL_ID", "")

    # Prefer bot token — gives us thread_ts back
    if token and channel_id:
        resp = requests.post(
            f"{SLACK_API_BASE}/chat.postMessage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "channel": channel_id,
                "text": summary_text,
                "mrkdwn": True,
            },
        )
        data = resp.json()
        if data.get("ok"):
            ts = data.get("ts")
            logger.info(f"Posted summary via bot token (ts={ts})")
            return ts
        else:
            logger.warning(
                f"chat.postMessage failed ({data.get('error')}), "
                f"falling back to webhook"
            )

    # Fallback: webhook (no thread_ts available)
    resp = requests.post(webhook_url, json={"text": summary_text})
    if resp.status_code == 200:
        logger.info("Posted summary via webhook (no thread_ts)")
    else:
        logger.error(f"Webhook post failed: {resp.status_code} {resp.text}")

    return None
