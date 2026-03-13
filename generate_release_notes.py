#!/usr/bin/env python3
"""
Monthly Release Notes Agent

Finds the latest versioned release notes on Confluence, increments the version,
queries Jira for issues with the next release label, generates release notes
using Claude, and emails a draft to the configured recipient.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from requests.auth import HTTPBasicAuth
import anthropic

# ── Configuration (all from environment variables / GitHub Secrets) ───────────
DOMAIN            = os.environ["ATLASSIAN_DOMAIN"]       # e.g. rohirrim.atlassian.net
ATLASSIAN_EMAIL   = os.environ["ATLASSIAN_EMAIL"]        # your Atlassian account email
ATLASSIAN_TOKEN   = os.environ["ATLASSIAN_API_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER        = os.environ["GMAIL_USER"]             # sender gmail address
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]   # Gmail app password (not account password)
RECIPIENT         = os.environ.get("RECIPIENT_EMAIL", "kevin.jackson@rohirrim.ai")
SPACE_KEY         = os.environ.get("CONFLUENCE_SPACE_KEY", "RohanProcure")
CURRENT_VERSION   = os.environ.get("CURRENT_VERSION", "4.4")   # e.g. "4.4" → next will be 4.5
REF_PAGE_ID       = os.environ.get("CONFLUENCE_REF_PAGE_ID", "1388937217")  # 4.4.0 Release Notes

AUTH       = HTTPBasicAuth(ATLASSIAN_EMAIL, ATLASSIAN_TOKEN)
JIRA_BASE  = f"https://{DOMAIN}/rest/api/3"
CONF_BASE  = f"https://{DOMAIN}/wiki/rest/api"


# ── Confluence helpers ────────────────────────────────────────────────────────

def get_latest_release_version() -> tuple[str, str, int, int]:
    """
    Return the current release version using REF_PAGE_ID and CURRENT_VERSION.
    Update these in the .command file each month before running.
    """
    parts = CURRENT_VERSION.split(".")
    major, minor = int(parts[0]), int(parts[1])
    title = f"Release {major}.{minor} Internal + External"
    print(f"  Using configured reference page (id: {REF_PAGE_ID}, v{major}.{minor})")
    return REF_PAGE_ID, title, major, minor


def get_confluence_page_content(page_id: str) -> str:
    """Fetch the storage-format body of a Confluence page."""
    resp = requests.get(
        f"{CONF_BASE}/content/{page_id}",
        auth=AUTH,
        params={"expand": "body.storage"},
    )
    resp.raise_for_status()
    return resp.json()["body"]["storage"]["value"]


# ── Jira helpers ──────────────────────────────────────────────────────────────

def get_jira_issues(label: str) -> list[dict]:
    """Return all Jira issues tagged with the given label."""
    resp = requests.post(
        f"{JIRA_BASE}/issue/search/jql",
        auth=AUTH,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        json={
            "jql": f'labels = "{label}"',
            "fields": ["summary", "description", "issuetype", "status", "priority", "labels"],
            "maxResults": 100,
        },
    )
    resp.raise_for_status()
    issues = resp.json().get("issues", [])

    return [
        {
            "key": i["key"],
            "summary": i["fields"]["summary"],
            "type": i["fields"]["issuetype"]["name"],
            "status": i["fields"]["status"]["name"],
            "description": _extract_description(i["fields"].get("description")),
            "url": f"https://{DOMAIN}/browse/{i['key']}",
        }
        for i in issues
    ]


def _extract_description(desc) -> str:
    """Pull plain text from Jira's Atlassian Document Format description field."""
    if not desc:
        return ""
    if isinstance(desc, str):
        return desc
    # ADF format — walk content nodes
    texts = []
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node.get("text", ""))
            for child in node.get("content", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(desc)
    return " ".join(texts).strip()


# ── Claude generation ─────────────────────────────────────────────────────────

def generate_release_notes(
    issues: list[dict],
    previous_content: str,
    version: str,
    label: str,
) -> str:
    """Use Claude to draft release notes matching the previous format and tone."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    issues_text = "\n".join(
        f"- [{i['key']}]({i['url']}) ({i['type']}, {i['status']}): {i['summary']}"
        + (f"\n  Description: {i['description']}" if i["description"] else "")
        for i in issues
    )

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": f"""You are writing release notes for UnifiedAcquire version {version}.

Here are the Jira issues completed for this release (label: {label}):
{issues_text}

Here are the previous release notes to use as a tone and structure reference:
<previous_release_notes>
{previous_content[:8000]}
</previous_release_notes>

Write release notes for version {version} that are a continuation of the previous release notes above.

The previous release notes are your single source of truth for tone, style, structure, and formatting.
Replicate every section exactly as it appears — same headers, same bullet style, same sentence length,
same level of detail, same voice. A reader should not be able to tell these were written by a different person.

The EXTERNAL FACING section is written for customers and end users.
The INTERNAL FACING section is written for the internal engineering and product team.
Study how each of those sections is written in the reference and copy that style precisely for each.

Rules:
- Output clean Markdown only — no preamble, no commentary
- Only include content supported by the Jira issues listed above — do not invent features
- Include Jira ticket links in the internal section only, matching the format used in the reference""",
            }
        ],
    )

    return message.content[0].text


# ── Email ─────────────────────────────────────────────────────────────────────

def send_draft_email(release_notes: str, version: str):
    """Send the draft release notes via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[DRAFT] UnifiedAcquire {version} Release Notes — Review Needed"
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT

    plain_body = (
        f"Draft release notes for UnifiedAcquire {version}.\n"
        f"Review below and post to Confluence when ready.\n\n"
        f"{'=' * 60}\n\n"
        f"{release_notes}"
    )

    html_body = f"""<html><body style="font-family: Arial, sans-serif; max-width: 800px; margin: auto;">
<p><strong>📋 Draft Release Notes — UnifiedAcquire {version}</strong></p>
<p>Review the notes below. When approved, post them to the
<a href="https://{DOMAIN}/wiki/spaces/{SPACE_KEY}">UnifiedAcquire Confluence space</a>.</p>
<hr>
<pre style="background:#f6f8fa; padding:16px; border-radius:6px;
            font-family: monospace; white-space: pre-wrap; font-size:13px;">
{release_notes}
</pre>
</body></html>"""

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())

    print(f"  Draft emailed to {RECIPIENT}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Step 1: Finding latest release version on Confluence...")
    page_id, title, major, minor = get_latest_release_version()

    next_minor   = minor + 1
    next_version = f"{major}.{next_minor}.0"
    label        = f"UA{major}.{next_minor}"

    print(f"Step 2: Querying Jira for issues with label '{label}'...")
    issues = get_jira_issues(label)

    if not issues:
        print(f"  No issues found for label '{label}'. Nothing to release this month.")
        return

    print(f"  Found {len(issues)} issue(s):")
    for i in issues:
        print(f"    {i['key']}: {i['summary']}")

    print("Step 3: Fetching previous release notes for tone reference...")
    previous_content = get_confluence_page_content(page_id)

    print("Step 4: Generating release notes with Claude...")
    notes = generate_release_notes(issues, previous_content, next_version, label)

    print("Step 5: Sending draft email...")
    send_draft_email(notes, next_version)

    print(f"\nDone! Draft for v{next_version} sent to {RECIPIENT}.")


if __name__ == "__main__":
    main()
