"""
Journalist Pitch Pipeline
Searches for IRS/tax news + journalist queries, drafts pitches,
and writes them to a Google Sheet for review.
"""

import os
import json
from datetime import datetime
import anthropic
from googleapiclient.discovery import build
from google.oauth2 import service_account

# ── Config ──────────────────────────────────────────────
SHEET_ID = os.environ.get("PITCH_SHEET_ID")
CREDS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
MODEL = "claude-sonnet-4-20250514"

NEIL_BIO = """
Neil Jesani is a CPA and tax strategist with 15+ years of experience.
He specializes in US-India cross-border tax planning, IRS compliance,
tax resolution, and small business tax strategy. He advises
high-net-worth individuals and business owners on complex IRS matters
including audits, liens, levies, and installment agreements.
Based in Fort Lauderdale, FL and Las Vegas, NV.
Website: neiljesanitaxresolution.com
"""

client = anthropic.Anthropic()


# ── Step 1: Find opportunities ──────────────────────────
def find_opportunities():
    print("🔍 Searching for journalist opportunities...")

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[
            {
                "role": "user",
                "content": f"""You are a PR research assistant for Neil Jesani, a CPA and tax strategist.

Search for ALL of these (do multiple searches):
1. Breaking IRS news or tax policy changes from the last 24-48 hours
2. Journalist queries on HARO, Connectively, Qwoted, SourceBottle, or Help a B2B Writer related to tax, IRS, accounting, or small business finance
3. Trending tax-related stories where a CPA expert source would add value
4. Any upcoming tax deadlines or IRS announcements that journalists might cover

Neil's expertise: {NEIL_BIO}

Return a JSON array of opportunities. Each item must have:
- "source": where you found it (publication name or platform)
- "headline": the news item or journalist query
- "angle": how Neil could contribute as an expert (1-2 sentences)
- "urgency": "high" or "medium" or "low"
- "journalist_name": name if available, otherwise null
- "outlet": publication/outlet if available, otherwise null

Find at least 3-5 opportunities. Return ONLY the JSON array, no markdown fences, no explanation.""",
            }
        ],
    )

    text = "".join(
        block.text for block in response.content if block.type == "text"
    )

    # Clean up potential markdown fences
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        opportunities = json.loads(text)
        print(f"   Found {len(opportunities)} opportunities")
        return opportunities
    except json.JSONDecodeError as e:
        print(f"   ❌ Failed to parse opportunities: {e}")
        print(f"   Raw response: {text[:500]}")
        return []


# ── Step 2: Draft pitches ───────────────────────────────
def draft_pitches(opportunities):
    print("✍️  Drafting pitches...")

    # Sort by urgency, take top 3
    order = {"high": 0, "medium": 1, "low": 2}
    top = sorted(opportunities, key=lambda x: order.get(x.get("urgency", "low"), 2))[:3]

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": f"""You are drafting journalist pitch emails on behalf of Neil Jesani, CPA.

Neil's background:
{NEIL_BIO}

Opportunities to pitch on:
{json.dumps(top, indent=2)}

For each opportunity, draft a short pitch email (under 150 words). The pitch should:
- Have a compelling subject line
- Open with a timely hook referencing the news/query
- Position Neil as a credible source in 1-2 sentences
- Offer a specific angle or talking point Neil could provide
- Close with availability (available for phone/email/video interview)
- Sign off as Neil Jesani, CPA

Tone: professional but warm, not salesy. Like a helpful expert reaching out.

Return a JSON array where each item has:
- "opportunity_headline": string
- "subject_line": string
- "body": string (the full email body)
- "journalist_name": string or null
- "outlet": string or null

Return ONLY the JSON array, no markdown fences.""",
            }
        ],
    )

    text = "".join(
        block.text for block in response.content if block.type == "text"
    )

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        pitches = json.loads(text)
        print(f"   Drafted {len(pitches)} pitches")
        return pitches
    except json.JSONDecodeError as e:
        print(f"   ❌ Failed to parse pitches: {e}")
        print(f"   Raw response: {text[:500]}")
        return []


# ── Step 3: Write to Google Sheet ───────────────────────
def write_to_sheet(pitches):
    if not SHEET_ID or not CREDS_JSON:
        print("⚠️  No Google Sheets credentials — printing pitches to console instead:\n")
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

    creds_dict = json.loads(CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
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
            "DRAFT",  # Status column — you review and change to APPROVED/SENT
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