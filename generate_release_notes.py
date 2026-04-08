#!/usr/bin/env python3
"""
Monthly Release Notes Agent

Finds the latest versioned release notes on Confluence, increments the version,
queries Jira for issues with the next release label, generates release notes
using Claude, and emails a draft to the configured recipient.
"""

import os
import re
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

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
REF_PAGE_ID       = os.environ.get("CONFLUENCE_REF_PAGE_ID", "1388937217")

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
    """Fetch the rendered body of a Confluence page as plain text."""
    resp = requests.get(
        f"{CONF_BASE}/content/{page_id}",
        auth=AUTH,
        params={"expand": "body.view"},
    )
    resp.raise_for_status()
    html = resp.json()["body"]["view"]["value"]
    # Strip HTML tags to give Claude clean readable text
    from html.parser import HTMLParser
    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
        def handle_data(self, d):
            self.parts.append(d)
    s = _Stripper()
    s.feed(html)
    return " ".join(s.parts)


# ── Jira helpers ──────────────────────────────────────────────────────────────

def get_jira_issues(label: str) -> list[dict]:
    """Return all Jira issues tagged with the given label, with retries."""
    jql = f'labels = "{label}"'
    fields = ["summary", "description", "issuetype", "status", "priority", "labels"]
    issues = []

    for attempt in range(3):
        resp = requests.post(
            f"{JIRA_BASE}/search/jql",
            auth=AUTH,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"jql": jql, "fields": fields, "maxResults": 100},
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        if issues:
            break
        if attempt < 2:
            print(f"  Jira returned 0 results, retrying in 3s... (attempt {attempt + 1}/3)")
            time.sleep(3)

    return [
        {
            "key":         i["key"],
            "summary":     i["fields"]["summary"],
            "type":        i["fields"]["issuetype"]["name"],
            "status":      i["fields"]["status"]["name"],
            "description": _extract_description(i["fields"].get("description")),
            "url":         f"https://{DOMAIN}/browse/{i['key']}",
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

Here are the previous release notes to use as your tone and structure reference:
<previous_release_notes>
{previous_content[:8000]}
</previous_release_notes>

Write release notes for version {version} following this exact structure:

# v{version} Release Notes
---
## EXTERNAL FACING
<one paragraph summary of the release themes>
### Enhancements
<bullet list — each item is bold-titled: **Feature Name:** description>
### Bug Fixes
<bullet list — short, plain sentences>
### What This Release Means for You
<one paragraph closing summary>
---
## INTERNAL FACING
### Digital OnRamp
<bullets or "No Digital OnRamp changes in this release.">
### High Side Tool
<bullets with bold-titled items>
### Template Generator
<bullets with bold-titled items>
### Compliance
<bullets with bold-titled items>
### Low Side Tool
<bullets with bold-titled items>

Tone rules:
- External section: customer-facing, benefit-oriented, no jargon
- Internal section: engineering-facing, precise, includes implementation detail
- Match the sentence length, voice, and level of detail from the reference exactly
- Every bullet in BOTH sections must start with the Jira ticket number: PRCR-1234 **Feature name:** description

Compliance rule:
- Compliance items belong ONLY in the ### Compliance subsection under INTERNAL FACING
- NEVER include compliance-related content in the EXTERNAL FACING section

Output rules:
- Clean Markdown only — no preamble, no commentary
- Only include content supported by the Jira issues listed — do not invent features
- No Jira ticket hyperlinks in either section — ticket numbers only (e.g. PRCR-1234), no markdown link syntax""",
            }
        ],
    )

    return message.content[0].text


# ── PDF generation ────────────────────────────────────────────────────────────

def _to_latin1(text: str) -> str:
    """Replace non-latin-1 characters so fpdf core fonts can render them."""
    replacements = {
        "\u2014": "--",   # em dash
        "\u2013": "-",    # en dash
        "\u2018": "'",    # left single quote
        "\u2019": "'",    # right single quote
        "\u201c": '"',    # left double quote
        "\u201d": '"',    # right double quote
        "\u2026": "...",  # ellipsis
        "\u2022": "*",    # bullet (replaced separately in PDF bullets)
        "\u00a0": " ",    # non-breaking space
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    # Encode to latin-1, dropping anything still unrepresentable
    return text.encode("latin-1", errors="replace").decode("latin-1")


def generate_pdf(release_notes: str, version: str) -> bytes:
    """Convert markdown release notes to a formatted PDF."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(20, 20, 20)

    def strip_inline_bold(text: str) -> str:
        return re.sub(r'\*\*(.*?)\*\*', r'\1', text)

    def strip_links(text: str) -> str:
        return re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    def write_bold_line(pdf, text: str, font_size: int = 10):
        """Write a line that may contain **bold** segments."""
        parts = re.split(r'(\*\*.*?\*\*)', text)
        pdf.set_font("Helvetica", "", font_size)
        for part in parts:
            if part.startswith("**") and part.endswith("**"):
                pdf.set_font("Helvetica", "B", font_size)
                pdf.write(5, _to_latin1(part[2:-2]))
                pdf.set_font("Helvetica", "", font_size)
            else:
                pdf.write(5, _to_latin1(part))
        pdf.ln()

    for line in release_notes.split("\n"):
        stripped = line.rstrip()

        if stripped.startswith("# "):
            pdf.set_font("Helvetica", "B", 18)
            pdf.cell(0, 12, _to_latin1(strip_links(stripped[2:])),
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)

        elif stripped.startswith("## "):
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_fill_color(240, 240, 240)
            pdf.cell(0, 9, _to_latin1(strip_links(stripped[3:])),
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
            pdf.ln(2)

        elif stripped.startswith("### "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, _to_latin1(strip_links(stripped[4:])),
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        elif stripped == "---":
            pdf.ln(2)
            pdf.set_draw_color(180, 180, 180)
            pdf.line(pdf.get_x(), pdf.get_y(), pdf.w - 20, pdf.get_y())
            pdf.ln(3)

        elif stripped.startswith("- ") or stripped.startswith("* "):
            content = strip_links(stripped[2:])
            pdf.set_x(25)
            pdf.set_font("Helvetica", "", 10)
            pdf.write(5, "- ")
            write_bold_line(pdf, content, font_size=10)
            pdf.set_x(20)

        elif stripped:
            pdf.set_font("Helvetica", "", 10)
            clean = _to_latin1(strip_inline_bold(strip_links(stripped)))
            pdf.multi_cell(0, 5, clean)
            pdf.ln(1)

        else:
            pdf.ln(3)

    return bytes(pdf.output())


# ── Attachment helpers ────────────────────────────────────────────────────────

def _clean_punctuation_artifacts(text: str) -> str:
    """Clean up leftover punctuation after ticket refs are removed."""
    text = re.sub(r'\(\s*[,;\s]*\s*\)', '', text)
    text = re.sub(r'\(\s*,\s*', '(', text)
    text = re.sub(r',\s*\)', ')', text)
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\s+([,;.])', r'\1', text)
    return text


def _strip_all_ticket_refs(text: str) -> str:
    """Remove ticket links AND bare ticket IDs (used for external section)."""
    text = re.sub(r'\[[A-Z]+-\d+\]\([^)]+\)', '', text)
    text = re.sub(r'\b[A-Z]+-\d+\b', '', text)
    return _clean_punctuation_artifacts(text)


def _strip_ticket_links_only(text: str) -> str:
    """Remove ticket markdown links but keep bare ticket IDs (used for internal section)."""
    # Convert [PRCR-1234](url) → PRCR-1234 (keep the ID, drop the link)
    text = re.sub(r'\[([A-Z]+-\d+)\]\([^)]+\)', r'\1', text)
    # Strip any other markdown links that aren't ticket IDs
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    return _clean_punctuation_artifacts(text)


def strip_ticket_refs(text: str) -> str:
    """
    Keep ticket IDs (PRCR-1234) in both sections; remove hyperlinks only.
    """
    return _strip_ticket_links_only(text).strip()


# ── Email ─────────────────────────────────────────────────────────────────────

def send_draft_email(release_notes: str, version: str):
    """Send the draft release notes via Gmail SMTP with PDF and Markdown attachments."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"[DRAFT] UnifiedAcquire {version} Release Notes — Review Needed"
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT

    # ── Body (plain + HTML) ───────────────────────────────────────────────────
    body_part = MIMEMultipart("alternative")

    plain_body = (
        f"Draft release notes for UnifiedAcquire {version}.\n"
        f"Attached: PDF for sharing, Markdown (.md) for Confluence copy-paste.\n\n"
        f"{'=' * 60}\n\n"
        f"{release_notes}"
    )

    html_body = f"""<html><body style="font-family: Arial, sans-serif; max-width: 800px; margin: auto;">
<p><strong>Draft Release Notes — UnifiedAcquire {version}</strong></p>
<p>Two attachments included:</p>
<ul>
  <li><strong>PDF</strong> — formatted version for sharing/review</li>
  <li><strong>Markdown (.md)</strong> — paste directly into Confluence</li>
</ul>
<hr>
<pre style="background:#f6f8fa; padding:16px; border-radius:6px;
            font-family: monospace; white-space: pre-wrap; font-size:13px;">
{release_notes}
</pre>
</body></html>"""

    body_part.attach(MIMEText(plain_body, "plain"))
    body_part.attach(MIMEText(html_body, "html"))
    msg.attach(body_part)

    # ── PDF attachment ────────────────────────────────────────────────────────
    print("  Generating PDF...")
    clean_notes = strip_ticket_refs(release_notes)
    pdf_bytes = generate_pdf(clean_notes, version)
    pdf_part = MIMEBase("application", "pdf")
    pdf_part.set_payload(pdf_bytes)
    encoders.encode_base64(pdf_part)
    pdf_filename = f"UnifiedAcquire_{version}_Release_Notes.pdf"
    pdf_part.add_header("Content-Disposition", "attachment", filename=pdf_filename)
    msg.attach(pdf_part)

    # ── Markdown attachment ───────────────────────────────────────────────────
    md_part = MIMEBase("text", "markdown")
    md_part.set_payload(clean_notes.encode("utf-8"))
    encoders.encode_base64(md_part)
    md_filename = f"UnifiedAcquire_{version}_Release_Notes.md"
    md_part.add_header("Content-Disposition", "attachment", filename=md_filename)
    msg.attach(md_part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())

    print(f"  Draft emailed to {RECIPIENT} (PDF + Markdown attached)")


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
