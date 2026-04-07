#!/usr/bin/env python3
"""
NeilJesaniTaxResolution.com — Daily Drip Publisher
===================================================
Each day: picks the next unpublished city, generates its unique paragraphs
via Claude API (1 city only), renders the page, publishes to WordPress,
and sends an email notification via Resend.

Usage:
  python daily_publish.py --status        # Show queue
  python daily_publish.py --dry-run       # Preview next page (no API calls)
  python daily_publish.py                 # Generate 1 + publish + email
  python daily_publish.py --slug tax-attorney-boca-raton-fl  # Force specific page
"""

import os, sys, json, logging, argparse
from pathlib import Path
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("ERROR: pip install requests")

try:
    import openpyxl
except ImportError:
    sys.exit("ERROR: pip install openpyxl")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Import from existing scripts ────────────────────────────────────────
from generate_pages import (
    load_location_data,
    render_location_page,
    WordPressPublisher,
    DATA_MODEL_PATH,
    TEMPLATE_PATH,
    WP_BASE_URL,
    CTA_PHONE,
)
from generate_unique_paragraphs import (
    generate_paragraphs,
    STATE_CONTEXT_NOTES,
    SYSTEM_PROMPT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM     = "Neil Jesani Publisher <bot@neiljesanitaxresolution.com>"
EMAIL_TO       = ["aryan@neiljesani.com", "stevek@neiljesani.com"]
FRONTEND_URL   = "https://neiljesanitaxresolution.com"
PUBLISH_LEDGER = Path("./publish_ledger.json")


# ═════════════════════════════════════════════════════════════════════════
# LEDGER
# ═════════════════════════════════════════════════════════════════════════
def load_ledger() -> dict:
    if PUBLISH_LEDGER.exists():
        return json.loads(PUBLISH_LEDGER.read_text())
    return {"published": [], "failed": []}

def save_ledger(ledger: dict):
    ledger["last_run"] = datetime.now(timezone.utc).isoformat()
    PUBLISH_LEDGER.write_text(json.dumps(ledger, indent=2))


# ═════════════════════════════════════════════════════════════════════════
# SYNC — pull already-published pages from WordPress into the ledger
# ═════════════════════════════════════════════════════════════════════════
def sync_ledger_with_wp(ledger: dict):
    log.info("Syncing ledger with existing WordPress pages...")
    published_slugs = {e["slug"] for e in ledger["published"]}
    session = requests.Session()
    session.auth = (os.getenv("WP_USERNAME", ""), os.getenv("WP_APP_PASSWORD", ""))

    page_num = 1
    added = 0
    while True:
        resp = session.get(
            f"{WP_BASE_URL.rstrip('/')}/wp-json/wp/v2/pages",
            params={"per_page": 100, "page": page_num, "status": "publish"},
        )
        if resp.status_code != 200:
            break
        pages = resp.json()
        if not pages:
            break
        for p in pages:
            slug = p.get("slug", "")
            if slug.startswith("tax-attorney-") and slug not in published_slugs:
                ledger["published"].append({
                    "slug": slug,
                    "city": slug.replace("tax-attorney-", "").rsplit("-", 1)[0].replace("-", " ").title(),
                    "state": slug.rsplit("-", 1)[-1].upper(),
                    "date": p.get("date", "synced"),
                    "wp_id": p.get("id"),
                    "url": p.get("link", ""),
                    "synced": True,
                })
                added += 1
        page_num += 1
    log.info(f"  Synced: {added} new entries added to ledger")


def _parse_priority(val) -> int:
    """Convert priority column to int. Handles 'High', 'Medium', 'Low', numbers, or empty."""
    if not val:
        return 99
    val_str = str(val).strip().lower()
    mapping = {"high": 1, "medium": 2, "low": 3}
    if val_str in mapping:
        return mapping[val_str]
    try:
        return int(float(val_str))
    except (ValueError, TypeError):
        return 99


# ═════════════════════════════════════════════════════════════════════════
# QUEUE — reads ALL rows from Excel (including placeholder ones)
# ═════════════════════════════════════════════════════════════════════════
def build_full_queue(ledger: dict) -> list[dict]:
    """
    Read Excel directly (not via load_location_data which skips placeholders).
    Returns all cities with their raw data, filtering out already-published.
    """
    published_slugs = {e["slug"] for e in ledger["published"]}

    wb = openpyxl.load_workbook(DATA_MODEL_PATH, data_only=True)
    ws = wb["Location Pages — Data Model"]

    queue = []
    for row in ws.iter_rows(min_row=4, values_only=True):
        if not row[2]:  # no city
            continue

        num, wave, city, state_abbr, metro, irs_addr, irs_phone, tax_rate, \
        agency, agency_url, pop, income, nearby, unique1, unique2, court, \
        primary_kw, vol, priority, gen_status, pub_status = row[:21]

        city_str = str(city)
        state_str = str(state_abbr)
        city_slug = city_str.lower().replace(" ", "-").replace(".", "")
        slug = f"tax-attorney-{city_slug}-{state_str.lower()}"

        if slug in published_slugs:
            continue

        # Check if paragraphs need generating
        needs_generation = (
            not unique1 or str(unique1).startswith("[WRITE:") or
            not unique2 or str(unique2).startswith("[WRITE:")
        )

        queue.append({
            "slug": slug,
            "city": city_str,
            "state_abbreviation": state_str,
            "metro": str(metro or ""),
            "irs_addr": str(irs_addr or ""),
            "irs_phone": str(irs_phone or ""),
            "tax_rate": str(tax_rate or ""),
            "agency": str(agency or ""),
            "agency_url": str(agency_url or ""),
            "pop": str(pop or ""),
            "income": str(income or ""),
            "nearby": str(nearby or ""),
            "unique1": str(unique1 or ""),
            "unique2": str(unique2 or ""),
            "court": str(court or ""),
            "wave": str(wave or ""),
            "priority": _parse_priority(priority),
            "needs_generation": needs_generation,
        })

    queue.sort(key=lambda x: (x["priority"], x["city"]))
    return queue


# ═════════════════════════════════════════════════════════════════════════
# GENERATE PARAGRAPHS FOR ONE CITY (calls Claude API)
# ═════════════════════════════════════════════════════════════════════════
def generate_for_one_city(city_data: dict) -> tuple[str, str]:
    """Generate unique paragraphs for a single city using Claude API."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    state_names = {
        "FL":"Florida","NV":"Nevada","CA":"California","NY":"New York","TX":"Texas",
        "IL":"Illinois","MA":"Massachusetts","NJ":"New Jersey","CT":"Connecticut",
        "PA":"Pennsylvania","CO":"Colorado","WA":"Washington","NC":"North Carolina",
        "TN":"Tennessee","MN":"Minnesota","OR":"Oregon",
    }
    state_name = state_names.get(city_data["state_abbreviation"], city_data["state_abbreviation"])

    para1, para2 = generate_paragraphs(
        client,
        city_data["city"],
        state_name,
        city_data["state_abbreviation"],
        city_data["metro"],
        city_data["tax_rate"],
        city_data["agency"],
        city_data["nearby"],
    )

    log.info(f"  Generated paragraphs: {len(para1)} + {len(para2)} chars")
    return para1, para2


# ═════════════════════════════════════════════════════════════════════════
# PUBLISH ONE PAGE
# ═════════════════════════════════════════════════════════════════════════
def publish_one_page(city_data: dict, para1: str, para2: str, template: str) -> dict:
    """Build row dict, render, and publish via WordPress."""
    from generate_pages import STATE_TAX_CONTEXT, get_state_name, build_location_schema

    state_abbr = city_data["state_abbreviation"]
    state_ctx = STATE_TAX_CONTEXT.get(state_abbr, {})

    row = {
        "city": city_data["city"],
        "state_abbreviation": state_abbr,
        "state_name": get_state_name(state_abbr),
        "metro_area": city_data["metro"],
        "local_irs_office_address": city_data["irs_addr"] or "[VERIFY: IRS.gov]",
        "local_irs_phone": city_data["irs_phone"] or "[VERIFY: IRS.gov]",
        "state_income_tax_rate": city_data["tax_rate"],
        "state_tax_agency_name": city_data["agency"],
        "state_tax_agency_url": city_data["agency_url"],
        "population_est": city_data["pop"],
        "median_hhi_est": city_data["income"],
        "nearby_cities": city_data["nearby"],
        "unique_local_paragraph_1": para1,
        "unique_local_paragraph_2": para2,
        "local_court_info": city_data["court"],
        "state_tax_context_sentence": state_ctx.get("sentence", ""),
        "state_tax_context_detail": state_ctx.get("detail", ""),
        "cta_phone": CTA_PHONE,
    }

    slug = city_data["slug"]
    title = f"Tax Attorney in {row['city']}, {row['state_name']}: IRS Audit & Tax Resolution Help"
    meta_desc = (
        f"Facing the IRS in {row['city']}? Neil Jesani's team of Tax Court attorneys, CPAs, "
        f"and Enrolled Agents helps {row['city']} residents resolve IRS audits, tax debt, "
        f"and collections. Free consultation: {CTA_PHONE}."
    )

    content = render_location_page(template, row)

    publisher = WordPressPublisher()
    result = publisher.publish_page(slug=slug, title=title, content=content, meta_description=meta_desc)

    if result.get("skipped"):
        return {"status": "skipped", "slug": slug}
    elif result.get("success"):
        return {"status": "published", "slug": slug, "id": result.get("id"), "url": f"{WP_BASE_URL}/{slug}/"}
    else:
        return {"status": "failed", "slug": slug, "error": result.get("error", "Unknown")}


# ═════════════════════════════════════════════════════════════════════════
# EMAIL via Resend
# ═════════════════════════════════════════════════════════════════════════
def send_email(result: dict, queue_remaining: int):
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set — skipping email.")
        return

    now = datetime.now(timezone.utc).strftime("%b %d, %Y at %H:%M UTC")
    slug = result["slug"]
    frontend_url = f"{FRONTEND_URL}/{slug}/"
    cms_url = result.get("url", f"{WP_BASE_URL}/{slug}/")
    status = result["status"]

    if status == "published":
        subject = f"✅ Page Published: /{slug}/"
        body = f"""
        <div style="font-family: -apple-system, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #e8f5e9; border-left: 4px solid #2e7d32; padding: 16px; margin-bottom: 20px;">
                <h2 style="margin: 0; color: #2e7d32;">Page Published</h2>
            </div>
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 10px 0; font-weight: bold; width: 140px;">Live URL</td>
                    <td style="padding: 10px 0;"><a href="{frontend_url}" style="color: #1565c0;">{frontend_url}</a></td></tr>
                <tr><td style="padding: 10px 0; font-weight: bold;">CMS URL</td>
                    <td style="padding: 10px 0;"><a href="{cms_url}" style="color: #888;">{cms_url}</a></td></tr>
                <tr><td style="padding: 10px 0; font-weight: bold;">WP Page ID</td>
                    <td style="padding: 10px 0;">{result.get("id", "—")}</td></tr>
                <tr><td style="padding: 10px 0; font-weight: bold;">Published</td>
                    <td style="padding: 10px 0;">{now}</td></tr>
                <tr><td style="padding: 10px 0; font-weight: bold;">Queue remaining</td>
                    <td style="padding: 10px 0;"><strong>{queue_remaining}</strong> pages left</td></tr>
            </table>
            <div style="background: #f5f5f5; padding: 14px; margin-top: 20px; border-radius: 6px;">
                <strong>To-do:</strong><br>
                • Verify the page at the URL above<br>
                • Submit to <a href="https://search.google.com/search-console">Google Search Console</a><br>
                • Next page auto-publishes tomorrow
            </div>
            <p style="color: #999; font-size: 11px; margin-top: 24px;">daily_publish.py via GitHub Actions</p>
        </div>"""
    elif status == "skipped":
        subject = f"⏭️ Skipped: /{slug}/ (already exists)"
        body = f"""<div style="font-family: -apple-system, Arial, sans-serif; max-width: 600px;">
            <div style="background: #fff3e0; border-left: 4px solid #f57c00; padding: 16px;">
                <h2 style="margin: 0; color: #e65100;">Page Skipped</h2></div>
            <p><code>{slug}</code> already exists. Next page publishes tomorrow.</p>
            <p>Queue remaining: <strong>{queue_remaining}</strong></p></div>"""
    else:
        subject = f"❌ FAILED: /{slug}/"
        body = f"""<div style="font-family: -apple-system, Arial, sans-serif; max-width: 600px;">
            <div style="background: #ffebee; border-left: 4px solid #c62828; padding: 16px;">
                <h2 style="margin: 0; color: #c62828;">Publish Failed</h2></div>
            <p><strong>Slug:</strong> <code>{slug}</code></p>
            <pre style="background:#f5f5f5;padding:12px;font-size:12px;">{result.get("error","Unknown")}</pre>
            <p style="color:#c62828;">Check WP credentials. Re-run from GitHub Actions.</p></div>"""

    try:
        resp = requests.post("https://api.resend.com/emails", headers={
            "Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json",
        }, json={"from": EMAIL_FROM, "to": [EMAIL_TO], "subject": subject, "html": body})
        if resp.status_code == 200:
            log.info(f"✉️  Email sent to {EMAIL_TO}")
        else:
            log.error(f"Resend error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ═════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Daily 1-page publisher")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--slug", help="Force a specific slug")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()

    ledger = load_ledger()

    if args.sync:
        sync_ledger_with_wp(ledger)
        save_ledger(ledger)
        return

    queue = build_full_queue(ledger)

    # ── Status ───────────────────────────────────────────────────────
    if args.status:
        print(f"\n📊 Publish Status")
        print(f"{'─'*45}")
        print(f"  Published     : {len(ledger['published'])}")
        print(f"  Failed        : {len(ledger['failed'])}")
        print(f"  In queue      : {len(queue)}")
        print(f"  Last run      : {ledger.get('last_run', 'never')}")
        if queue:
            print(f"\n  Next 5 in queue:")
            for i, p in enumerate(queue[:5]):
                gen = "⚡ needs AI" if p["needs_generation"] else "✅ ready"
                print(f"    {i+1}. /{p['slug']}/  (P{p['priority']}) [{gen}]")
            if len(queue) > 5:
                print(f"    ... +{len(queue) - 5} more")
        else:
            print(f"\n  ✅ All pages published!")
        return

    # ── Pick target ──────────────────────────────────────────────────
    if args.slug:
        target = next((p for p in queue if p["slug"] == args.slug), None)
        if not target:
            sys.exit(f"ERROR: '{args.slug}' not in queue")
    else:
        if not queue:
            log.info("✅ Queue empty — all pages published.")
            return
        target = queue[0]

    log.info(f"{'[DRY RUN] ' if args.dry_run else ''}Target: /{target['slug']}/")
    log.info(f"  City: {target['city']}, {target['state_abbreviation']}")
    log.info(f"  Needs AI generation: {target['needs_generation']}")

    if args.dry_run:
        print(f"\n{'='*55}")
        print(f"  DRY RUN — would publish:")
        print(f"  Slug   : {target['slug']}")
        print(f"  City   : {target['city']}, {target['state_abbreviation']}")
        print(f"  AI gen : {'Yes' if target['needs_generation'] else 'No (paragraphs exist)'}")
        print(f"  After  : {len(queue) - 1} pages remain")
        print(f"{'='*55}")
        return

    # ── Generate paragraphs if needed (1 city only) ──────────────────
    if target["needs_generation"]:
        log.info("  Generating unique paragraphs via Claude API...")
        para1, para2 = generate_for_one_city(target)
    else:
        para1 = target["unique1"]
        para2 = target["unique2"]

    # ── Load template ────────────────────────────────────────────────
    tpl = Path(TEMPLATE_PATH)
    if not tpl.exists():
        sys.exit(f"ERROR: Template not found: {TEMPLATE_PATH}")
    template = tpl.read_text(encoding="utf-8")

    # ── Publish ──────────────────────────────────────────────────────
    log.info("  Publishing to WordPress...")
    result = publish_one_page(target, para1, para2, template)
    log.info(f"  → {result['status'].upper()}")

    # ── Update ledger ────────────────────────────────────────────────
    entry = {
        "slug": target["slug"],
        "city": target["city"],
        "state": target["state_abbreviation"],
        "date": datetime.now(timezone.utc).isoformat(),
    }
    if result["status"] == "published":
        entry["wp_id"] = result.get("id")
        entry["url"] = result.get("url")
        ledger["published"].append(entry)
    elif result["status"] == "failed":
        entry["error"] = result.get("error", "")[:200]
        ledger["failed"].append(entry)

    save_ledger(ledger)
    log.info(f"  Ledger: {len(ledger['published'])} published total")

    # ── Email ────────────────────────────────────────────────────────
    remaining = len(queue) - (1 if result["status"] == "published" else 0)
    send_email(result, remaining)
    log.info("🏁 Done.")


if __name__ == "__main__":
    main()