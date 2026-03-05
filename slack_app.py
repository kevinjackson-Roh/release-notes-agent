#!/usr/bin/env python3
"""
Slack Slash Command Handler — Release Notes Agent

Handles /release-notes slash commands from Slack.
Generates release notes and posts the draft back to the channel.

Usage in Slack:
  /release-notes          — auto-detect next version from Confluence
  /release-notes 4.6      — specify version manually
"""

import os
import hmac
import hashlib
import threading
import time

from flask import Flask, request, jsonify, abort
import requests as http_requests

from generate_release_notes import (
    get_latest_release_version,
    get_confluence_page_content,
    get_jira_issues,
    generate_release_notes,
    send_draft_email,
    RECIPIENT,
)

app = Flask(__name__)

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]


# ── Security ──────────────────────────────────────────────────────────────────

def verify_slack_signature(req) -> bool:
    """Verify the request genuinely came from Slack."""
    timestamp  = req.headers.get("X-Slack-Request-Timestamp", "")
    slack_sig  = req.headers.get("X-Slack-Signature", "")

    if not timestamp or not slack_sig:
        return False

    # Reject requests older than 5 minutes (replay attack protection)
    if abs(time.time() - int(timestamp)) > 300:
        return False

    body           = req.get_data(as_text=True)
    sig_basestring = f"v0:{timestamp}:{body}"
    computed       = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, slack_sig)


# ── Slack messaging ───────────────────────────────────────────────────────────

def post_to_slack(response_url: str, text: str, broadcast: bool = True):
    """Post a message back to Slack via the response URL."""
    http_requests.post(
        response_url,
        json={
            "response_type": "in_channel" if broadcast else "ephemeral",
            "text": text,
        },
        timeout=10,
    )


# ── Background generation ─────────────────────────────────────────────────────

def generate_and_post(response_url: str, version_arg: str):
    """
    Runs in a background thread.
    Generates release notes and posts them back to Slack.
    """
    try:
        # Determine version and label
        if version_arg:
            clean = version_arg.replace("UA", "").replace("v", "").strip()
            parts = clean.split(".")
            major, minor = int(parts[0]), int(parts[1])
            next_version  = f"{major}.{minor}.0"
            label         = f"UA{major}.{minor}"
            page_id, _, _, _ = get_latest_release_version()
        else:
            page_id, _, major, minor = get_latest_release_version()
            next_minor   = minor + 1
            next_version = f"{major}.{next_minor}.0"
            label        = f"UA{major}.{next_minor}"

        print(f"Generating notes for {next_version} (label: {label})")

        # Fetch Jira issues
        issues = get_jira_issues(label)
        if not issues:
            post_to_slack(
                response_url,
                f"⚠️ No Jira issues found with label `{label}`. "
                f"Make sure tickets are labeled before running.",
            )
            return

        # Generate release notes with Claude
        previous_content = get_confluence_page_content(page_id)
        notes = generate_release_notes(issues, previous_content, next_version, label)

        # Also send full draft via email
        send_draft_email(notes, next_version)

        # Split into external / internal for Slack display
        parts     = notes.split("## INTERNAL FACING")
        external  = parts[0].strip()
        internal  = ("## INTERNAL FACING" + parts[1].strip()) if len(parts) > 1 else ""

        # Post header
        post_to_slack(
            response_url,
            f"✅ *Draft Release Notes — v{next_version}* (`{label}`)\n"
            f"_{len(issues)} Jira issue(s) included · Full draft also emailed to {RECIPIENT}_",
        )

        # Post external section
        post_to_slack(
            response_url,
            f"*🌐 External Facing:*\n```{external[:2800]}```",
        )

        # Post internal section if present
        if internal:
            post_to_slack(
                response_url,
                f"*🔒 Internal Facing:*\n```{internal[:2800]}```",
            )

        post_to_slack(
            response_url,
            "👆 Review the draft above. When ready, post it to the "
            "<https://rohirrim.atlassian.net/wiki/spaces/RohanProcure|UnifiedAcquire Confluence space>.",
        )

    except Exception as e:
        post_to_slack(response_url, f"❌ Error generating release notes: {str(e)}")
        raise


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/slack/release-notes", methods=["POST"])
def slash_release_notes():
    """Entry point for the /release-notes Slack slash command."""
    if not verify_slack_signature(request):
        abort(403)

    response_url = request.form.get("response_url", "")
    version_arg  = request.form.get("text", "").strip()

    # Spin up background thread — Slack requires a response within 3 seconds
    thread = threading.Thread(
        target=generate_and_post,
        args=(response_url, version_arg),
        daemon=True,
    )
    thread.start()

    version_label = f"v{version_arg}" if version_arg else "the next version"
    return jsonify({
        "response_type": "ephemeral",
        "text": f"⏳ Generating release notes for {version_label}… "
                f"I'll post the draft here in about 30 seconds!",
    })


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint for Render."""
    return "OK", 200


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting Release Notes Slack bot on port {port}")
    app.run(host="0.0.0.0", port=port)
