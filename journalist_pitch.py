"""
Journalist Pitch Pipeline
Searches for IRS/tax news + journalist queries, drafts pitches,
and writes them to a Google Sheet for review.
"""

import os
import json
import time
from datetime import datetime
import anthropic
from googleapiclient.discovery import build
import google.oauth2.credentials

# ── Config ──────────────────────────────────────────────
SHEET_ID = os.environ.get("PITCH_SHEET_ID")
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")
MODEL = "claude-sonnet-4-20250514"

NEIL_BIO = """Neil Jesani, CPA with 15+ years experience. Specializes in US-India cross-border tax, IRS compliance, tax resolution, small business tax strategy. Based in Fort Lauderdale & Las Vegas. Website: neiljesanitaxresolution.com"""

client = anthropic.Anthropic()


# ── Retry wrapper ───────────────────────────────────────
def call_claude(messages, tools=None, max_retries=3):
    """Call Claude API with automatic retry on rate limit errors."""
    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": MODEL,
                "max_tokens": 2048,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            wait = 60 * (attempt + 1)  # 60s, 120s, 180s
            print(f"   ⏳ Rate limited. Waiting {wait}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)
    print("   ❌ Failed after all retries.")
    return None


# ── Step 1: Find opportunities ──────────────────────────
def find_opportunities():
    print("🔍 Searching for journalist opportunities...")

    response = call_claude(
        messages=[{
            "role": "user",
            "content": f"""PR research assistant for {NEIL_BIO}

Search for:
1. Breaking IRS/tax news from last 48 hours
2. Journalist queries on Connectively, Qwoted, SourceBottle related to tax/IRS/accounting
3. Trending tax stories needing a CPA expert source

Return JSON array only (no fences). Each item:
{{"source":"...","headline":"...","angle":"...","urgency":"high|medium|low","journalist_name":null,"outlet":null}}

Find 3-5 opportunities.""",
        }],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    if not response:
        return []

    text = "".join(b.text for b in response.content if b.type == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        opportunities = json.loads(text)
        print(f"   Found {len(opportunities)} opportunities")
        return opportunities
    except json.JSONDecodeError as e:
        print(f"   ❌ Failed to parse: {e}")
        print(f"   Raw: {text[:300]}")
        return []


# ── Step 2: Draft pitches ───────────────────────────────
def draft_pitches(opportunities):
    print("✍️  Drafting pitches...")

    order = {"high": 0, "medium": 1, "low": 2}
    top = sorted(opportunities, key=lambda x: order.get(x.get("urgency", "low"), 2))[:3]

    response = call_claude(
        messages=[{
            "role": "user",
            "content": f"""Draft journalist pitch emails for {NEIL_BIO}

Opportunities:
{json.dumps(top)}

For each, draft a pitch email under 120 words with subject line. Professional, warm, not salesy. Close with availability for interview.

Return JSON array only (no fences). Each item:
{{"opportunity_headline":"...","subject_line":"...","body":"...","journalist_name":null,"outlet":null}}""",
        }],
    )

    if not response:
        return []

    text = "".join(b.text for b in response.content if b.type == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        pitches = json.loads(text)
        print(f"   Drafted {len(pitches)} pitches")
        return pitches
    except json.JSONDecodeError as e:
        print(f"   ❌ Failed to parse: {e}")
        print(f"   Raw: {text[:300]}")
        return []


# ── Step 3: Write to Google Sheet ───────────────────────
def write_to_sheet(pitches):
    if not SHEET_ID or not REFRESH_TOKEN:
        print("⚠️  No Google Sheets credentials — printing to console:\n")
        for i, p in enumerate(pitches, 1):
            print(f"{'='*60}")
            print(f"PITCH {i}")
            print(f"Opportunity: {p.get('opportunity_headline', 'N/A')}")
            print(f"Journalist:  {p.get('journalist_name', 'N/A')}")
            print(f"Outlet:      {p.get('outlet', 'N/A')}")
            print(f"Subject:     {p.get('subject_line', 'N/A')}")
            print(f"\n{p.get('body', 'N/A')}")
            print()
        return

    print("📊 Writing to Google Sheet...")

    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    service = build("sheets", "v4", credentials=creds)
    sheet = service.spreadsheets()

    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for p in pitches:
        rows.append([
            today,
            p.get("opportunity_headline", ""),
            p.get("journalist_name", ""),
            p.get("outlet", ""),
            p.get("subject_line", ""),
            p.get("body", ""),
            "DRAFT",
        ])

    sheet.values().append(
        spreadsheetId=SHEET_ID,
        range="Pitches!A:G",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    print(f"   ✅ Wrote {len(rows)} pitches to sheet")


# ── Main ────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("JOURNALIST PITCH PIPELINE")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    opportunities = find_opportunities()
    if not opportunities:
        print("\n❌ No opportunities found today. Exiting.")
        return

    print("⏳ Waiting 90s before drafting (rate limit cooldown)...")
    time.sleep(90)

    pitches = draft_pitches(opportunities)
    if not pitches:
        print("\n❌ Failed to draft pitches. Exiting.")
        return

    write_to_sheet(pitches)

    print("\n" + "=" * 50)
    print("✅ Pipeline complete!")
    print("=" * 50)


if __name__ == "__main__":
    main()