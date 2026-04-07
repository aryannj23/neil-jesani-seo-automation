#!/usr/bin/env python3
"""
NeilJesaniTaxResolution.com — Daily Drip Publisher
===================================================
Publishes exactly 1 page per day from the generated queue,
sends an email summary to aryan@neiljesani.com via Gmail SMTP.

Designed to run via GitHub Actions on a daily cron schedule.

Usage:
  python daily_publish.py --status        # Show queue + what's published
  python daily_publish.py --dry-run       # Preview next page (no publish, no email)
  python daily_publish.py                 # Publish 1 page + send email
  python daily_publish.py --slug tax-attorney-miami-fl   # Force a specific page
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

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config (all from environment / GitHub Secrets) ───────────────────────
WP_BASE_URL     = os.getenv("WP_BASE_URL", "https://cms.neiljesanitaxresolution.com")
WP_USERNAME     = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
DATA_MODEL_PATH = os.getenv("DATA_MODEL_PATH", "./NeilJesani_Programmatic_DataModel.xlsx")
TEMPLATE_PATH   = os.getenv("TEMPLATE_PATH", "./location_page_template.html")

RESEND_API_KEY  = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM      = "Neil Jesani Publisher <bot@neiljesanitaxresolution.com>"
EMAIL_TO        = "aryan@neiljesani.com"

PUBLISH_LEDGER  = Path("./publish_ledger.json")
CTA_PHONE       = "(800) 758-3101"

STATE_NAMES = {
    "FL":"Florida","NV":"Nevada","CA":"California","NY":"New York","TX":"Texas",
    "IL":"Illinois","MA":"Massachusetts","NJ":"New Jersey","CT":"Connecticut",
    "PA":"Pennsylvania","CO":"Colorado","WA":"Washington","NC":"North Carolina",
    "TN":"Tennessee","MN":"Minnesota","OR":"Oregon",
}


# ═════════════════════════════════════════════════════════════════════════
# LEDGER — tracks published pages as a JSON file committed to the repo
# ═════════════════════════════════════════════════════════════════════════
def load_ledger() -> dict:
    if PUBLISH_LEDGER.exists():
        return json.loads(PUBLISH_LEDGER.read_text())
    return {"published": [], "failed": []}


def save_ledger(ledger: dict):
    ledger["last_run"] = datetime.now(timezone.utc).isoformat()
    PUBLISH_LEDGER.write_text(json.dumps(ledger, indent=2))


# ═════════════════════════════════════════════════════════════════════════
# QUEUE — reads Excel, returns unpublished pages sorted by priority
# ═════════════════════════════════════════════════════════════════════════
def build_queue(ledger: dict) -> list[dict]:
    published_slugs = {e["slug"] for e in ledger["published"]}

    wb = openpyxl.load_workbook(DATA_MODEL_PATH, data_only=True)
    ws = wb["Location Pages — Data Model"]

    queue = []
    for row in ws.iter_rows(min_row=4, values_only=True):
        if not row[2]:
            continue

        num, wave, city, state_abbr, metro, irs_addr, irs_phone, tax_rate, \
        agency, agency_url, pop, income, nearby, unique1, unique2, court, \
        primary_kw, vol, priority, gen_status, pub_status = row[:21]

        # Skip rows where unique paragraphs aren't written yet
        if not unique1 or str(unique1).startswith("[WRITE:"):
            continue
        if not unique2 or str(unique2).startswith("[WRITE:"):
            continue

        city_str = str(city)
        state_str = str(state_abbr)
        city_slug = city_str.lower().replace(" ", "-").replace(".", "")
        slug = f"tax-attorney-{city_slug}-{state_str.lower()}"

        if slug in published_slugs:
            continue

        queue.append({
            "slug": slug,
            "city": city_str,
            "state_abbreviation": state_str,
            "state_name": STATE_NAMES.get(state_str, state_str),
            "metro_area": str(metro or ""),
            "local_irs_office_address": str(irs_addr or ""),
            "local_irs_phone": str(irs_phone or ""),
            "state_income_tax_rate": str(tax_rate or ""),
            "state_tax_agency_name": str(agency or ""),
            "state_tax_agency_url": str(agency_url or ""),
            "population_est": str(pop or ""),
            "median_hhi_est": str(income or ""),
            "nearby_cities": str(nearby or ""),
            "unique_local_paragraph_1": str(unique1),
            "unique_local_paragraph_2": str(unique2),
            "local_court_info": str(court or ""),
            "cta_phone": CTA_PHONE,
            "wave": str(wave or ""),
            "priority": int(priority) if priority else 99,
        })

    queue.sort(key=lambda x: (x["priority"], x["wave"], x["city"]))
    return queue


# ═════════════════════════════════════════════════════════════════════════
# RENDER — inject data into HTML template
# ═════════════════════════════════════════════════════════════════════════
def render_page(template: str, row: dict) -> tuple[str, str, str]:
    """Returns (html_content, title, meta_description)."""
    try:
        from generate_pages import build_location_schema, render_location_page, STATE_TAX_CONTEXT
        state_ctx = STATE_TAX_CONTEXT.get(row["state_abbreviation"], {})
        row.setdefault("state_tax_context_sentence", state_ctx.get("sentence", ""))
        row.setdefault("state_tax_context_detail", state_ctx.get("detail", ""))
        content = render_location_page(template, row)
    except ImportError:
        # Fallback: direct variable substitution
        log.warning("generate_pages.py not importable — using fallback renderer")
        content = template
        for key, val in row.items():
            content = content.replace(f"{{{{{key}}}}}", str(val))

    title = f"Tax Attorney in {row['city']}, {row['state_name']}: IRS Audit & Tax Resolution Help"
    meta_desc = (
        f"Facing the IRS in {row['city']}? Neil Jesani's team of Tax Court attorneys, CPAs, "
        f"and Enrolled Agents helps {row['city']} residents resolve IRS audits, tax debt, "
        f"and collections. Free consultation: {CTA_PHONE}."
    )
    return content, title, meta_desc


# ═════════════════════════════════════════════════════════════════════════
# WORDPRESS PUBLISH
# ═════════════════════════════════════════════════════════════════════════
def wp_publish(slug: str, title: str, content: str, meta_desc: str) -> dict:
    session = requests.Session()
    session.auth = (WP_USERNAME, WP_APP_PASSWORD)
    session.headers.update({"Content-Type": "application/json"})
    base = WP_BASE_URL.rstrip("/")

    # Check if page already exists
    resp = session.get(f"{base}/wp-json/wp/v2/pages", params={"slug": slug, "per_page": 1})
    if resp.status_code == 200 and len(resp.json()) > 0:
        return {"status": "skipped", "reason": "already exists on WP", "slug": slug}

    payload = {
        "title": title,
        "content": content,
        "slug": slug,
        "status": "publish",
        "type": "page",
        "meta": {
            "rank_math_description": meta_desc,
            "rank_math_focus_keyword": slug.replace("-", " ").replace("tax attorney ", ""),
            "rank_math_robots": ["index", "follow"],
        },
    }

    resp = session.post(f"{base}/wp-json/wp/v2/pages", data=json.dumps(payload))

    if resp.status_code in (200, 201):
        page_id = resp.json().get("id")
        return {
            "status": "published",
            "slug": slug,
            "id": page_id,
            "url": f"{WP_BASE_URL}/{slug}/",
        }
    else:
        return {
            "status": "failed",
            "slug": slug,
            "error": resp.text[:500],
            "http_code": resp.status_code,
        }


# ═════════════════════════════════════════════════════════════════════════
# EMAIL via Gmail SMTP
# ═════════════════════════════════════════════════════════════════════════
def send_email(result: dict, queue_remaining: int):
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set — skipping email.")
        return False

    now = datetime.now(timezone.utc).strftime("%b %d, %Y at %H:%M UTC")
    slug = result["slug"]
    url = result.get("url", f"{WP_BASE_URL}/{slug}/")
    status = result["status"]

    # ── Build email based on status ──────────────────────────────────
    if status == "published":
        subject = f"✅ Page Published: /{slug}/"
        body = f"""
        <div style="font-family: -apple-system, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #e8f5e9; border-left: 4px solid #2e7d32; padding: 16px; margin-bottom: 20px;">
                <h2 style="margin: 0; color: #2e7d32;">Page Published</h2>
            </div>
            <table style="width: 100%; border-collapse: collapse;">
                <tr>
                    <td style="padding: 10px 0; font-weight: bold; width: 140px;">Live URL</td>
                    <td style="padding: 10px 0;"><a href="{url}" style="color: #1565c0;">{url}</a></td>
                </tr>
                <tr>
                    <td style="padding: 10px 0; font-weight: bold;">WP Page ID</td>
                    <td style="padding: 10px 0;">{result.get("id", "—")}</td>
                </tr>
                <tr>
                    <td style="padding: 10px 0; font-weight: bold;">Published</td>
                    <td style="padding: 10px 0;">{now}</td>
                </tr>
                <tr>
                    <td style="padding: 10px 0; font-weight: bold;">Queue remaining</td>
                    <td style="padding: 10px 0;"><strong>{queue_remaining}</strong> pages left</td>
                </tr>
            </table>
            <div style="background: #f5f5f5; padding: 14px; margin-top: 20px; border-radius: 6px;">
                <strong>To-do:</strong><br>
                • Open the URL above and verify it looks correct<br>
                • Submit to <a href="https://search.google.com/search-console">Google Search Console</a> for indexing<br>
                • Next page auto-publishes tomorrow
            </div>
            <p style="color: #999; font-size: 11px; margin-top: 24px;">
                Sent automatically by daily_publish.py via GitHub Actions
            </p>
        </div>"""

    elif status == "skipped":
        subject = f"⏭️ Skipped: /{slug}/ (already exists)"
        body = f"""
        <div style="font-family: -apple-system, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #fff3e0; border-left: 4px solid #f57c00; padding: 16px;">
                <h2 style="margin: 0; color: #e65100;">Page Skipped — Already Exists</h2>
            </div>
            <p><code>{slug}</code> already exists on WordPress.</p>
            <p>The script will publish the <strong>next page in queue</strong> tomorrow.</p>
            <p>Queue remaining: <strong>{queue_remaining}</strong> pages</p>
        </div>"""

    else:  # failed
        subject = f"❌ FAILED: /{slug}/ — needs attention"
        body = f"""
        <div style="font-family: -apple-system, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #ffebee; border-left: 4px solid #c62828; padding: 16px;">
                <h2 style="margin: 0; color: #c62828;">Publish Failed</h2>
            </div>
            <p><strong>Slug:</strong> <code>{slug}</code></p>
            <p><strong>HTTP Code:</strong> {result.get("http_code", "—")}</p>
            <p><strong>Error:</strong></p>
            <pre style="background: #f5f5f5; padding: 12px; overflow-x: auto; font-size: 12px; border-radius: 4px;">
{result.get("error", "Unknown error")}</pre>
            <p style="color: #c62828;"><strong>Action needed:</strong> Check WP credentials and API access.
            Re-run manually from GitHub Actions after fixing.</p>
        </div>"""

    # ── Send via Resend API ──────────────────────────────────────────
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": EMAIL_FROM,
                "to": [EMAIL_TO],
                "subject": subject,
                "html": body,
            },
        )
        if resp.status_code == 200:
            log.info(f"✉️  Email sent to {EMAIL_TO} via Resend")
            return True
        else:
            log.error(f"Resend API error {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"Email send failed: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Daily 1-page publisher + email notification")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no publish, no email")
    parser.add_argument("--slug", help="Force-publish a specific slug")
    parser.add_argument("--status", action="store_true", help="Show queue status and exit")
    args = parser.parse_args()

    ledger = load_ledger()
    queue = build_queue(ledger)

    # ── Status ───────────────────────────────────────────────────────
    if args.status:
        total_published = len(ledger["published"])
        total_failed = len(ledger["failed"])
        print(f"\n📊 Publish Status")
        print(f"{'─'*45}")
        print(f"  Published     : {total_published}")
        print(f"  Failed        : {total_failed}")
        print(f"  In queue      : {len(queue)}")
        print(f"  Last run      : {ledger.get('last_run', 'never')}")
        if queue:
            print(f"\n  Next 5 in queue:")
            for i, p in enumerate(queue[:5]):
                print(f"    {i+1}. /{p['slug']}/  (P{p['priority']}, W{p['wave']})")
            if len(queue) > 5:
                print(f"    ... +{len(queue) - 5} more")
        else:
            print(f"\n  ✅ All pages published!")
        return

    # ── Load template ────────────────────────────────────────────────
    tpl = Path(TEMPLATE_PATH)
    if not tpl.exists():
        sys.exit(f"ERROR: Template not found: {TEMPLATE_PATH}")
    template = tpl.read_text(encoding="utf-8")

    # ── Pick target page ─────────────────────────────────────────────
    if args.slug:
        target = next((p for p in queue if p["slug"] == args.slug), None)
        if not target:
            sys.exit(f"ERROR: '{args.slug}' not in queue (already published or not found)")
    else:
        if not queue:
            log.info("✅ Queue empty — all pages published. Nothing to do.")
            return
        target = queue[0]

    log.info(f"{'[DRY RUN] ' if args.dry_run else ''}Target: /{target['slug']}/")
    log.info(f"  City: {target['city']}, {target['state_abbreviation']}")

    # ── Render HTML ──────────────────────────────────────────────────
    content, title, meta_desc = render_page(template, target)
    log.info(f"  Rendered: {len(content):,} chars")

    if args.dry_run:
        print(f"\n{'='*55}")
        print(f"  DRY RUN — would publish:")
        print(f"  Slug   : {target['slug']}")
        print(f"  Title  : {title}")
        print(f"  Chars  : {len(content):,}")
        print(f"  After  : {len(queue) - 1} pages remain in queue")
        print(f"{'='*55}")
        return

    # ── Publish to WordPress ─────────────────────────────────────────
    if not WP_USERNAME or not WP_APP_PASSWORD:
        sys.exit("ERROR: WP_USERNAME and WP_APP_PASSWORD not set")

    log.info("  Publishing to WordPress...")
    result = wp_publish(target["slug"], title, content, meta_desc)
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
    log.info(f"  Ledger saved ({len(ledger['published'])} published total)")

    # ── Send email ───────────────────────────────────────────────────
    remaining = len(queue) - (1 if result["status"] == "published" else 0)
    send_email(result, remaining)

    log.info("🏁 Done.")


if __name__ == "__main__":
    main()