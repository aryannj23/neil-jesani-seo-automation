#!/usr/bin/env python3
"""
NeilJesaniTaxResolution.com — Daily Drip Publisher
===================================================
Publishes exactly 1 page per day by reusing generate_pages.py logic.
Sends an email notification via Resend after each publish.

Usage:
  python daily_publish.py --status        # Show queue
  python daily_publish.py --dry-run       # Preview next page
  python daily_publish.py                 # Publish 1 page + email
  python daily_publish.py --slug tax-attorney-miami-fl   # Force specific page
"""

import os, sys, json, logging, argparse, time
from pathlib import Path
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("ERROR: pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Import everything from your existing working script ─────────────────
from generate_pages import (
    load_location_data,
    render_location_page,
    WordPressPublisher,
    DATA_MODEL_PATH,
    TEMPLATE_PATH,
    WP_BASE_URL,
    CTA_PHONE,
    STATE_TAX_CONTEXT,
)

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM     = "Neil Jesani Publisher <bot@neiljesanitaxresolution.com>"
EMAIL_TO       = "aryan@neiljesani.com"
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
# QUEUE — uses generate_pages.load_location_data directly
# ═════════════════════════════════════════════════════════════════════════
def build_queue(ledger: dict) -> list[dict]:
    """Load ALL location rows via generate_pages.py, filter out already-published."""
    published_slugs = {e["slug"] for e in ledger["published"]}

    # Load all waves — no wave filter
    all_rows = load_location_data(DATA_MODEL_PATH, wave_filter=None)

    queue = []
    for row in all_rows:
        city_slug = row["city"].lower().replace(" ", "-").replace(".", "")
        state_slug = row["state_abbreviation"].lower()
        slug = f"tax-attorney-{city_slug}-{state_slug}"

        if slug in published_slugs:
            continue

        row["_slug"] = slug
        row["_priority"] = int(row.get("priority") or 99)
        queue.append(row)

    # Sort by priority (lower = first), then city name
    queue.sort(key=lambda x: (x["_priority"], x["city"]))
    return queue


# ═════════════════════════════════════════════════════════════════════════
# PUBLISH — uses generate_pages.WordPressPublisher + render_location_page
# ═════════════════════════════════════════════════════════════════════════
def publish_one_page(row: dict, template: str) -> dict:
    """Render and publish a single page using existing generate_pages logic."""
    slug = row["_slug"]
    title = f"Tax Attorney in {row['city']}, {row['state_name']}: IRS Audit & Tax Resolution Help"
    meta_desc = (
        f"Facing the IRS in {row['city']}? Neil Jesani's team of Tax Court attorneys, CPAs, "
        f"and Enrolled Agents helps {row['city']} residents resolve IRS audits, tax debt, "
        f"and collections. Free consultation: {CTA_PHONE}."
    )

    # Render using the exact same function as generate_pages.py
    content = render_location_page(template, row)

    # Publish using the exact same WordPressPublisher class
    publisher = WordPressPublisher()
    result = publisher.publish_page(
        slug=slug,
        title=title,
        content=content,
        meta_description=meta_desc,
    )

    # Normalize result format
    if result.get("skipped"):
        return {"status": "skipped", "slug": slug, "reason": "already exists on WP"}
    elif result.get("success"):
        return {
            "status": "published",
            "slug": slug,
            "id": result.get("id"),
            "url": f"{WP_BASE_URL}/{slug}/",
        }
    else:
        return {
            "status": "failed",
            "slug": slug,
            "error": result.get("error", "Unknown error"),
        }


# ═════════════════════════════════════════════════════════════════════════
# EMAIL via Resend
# ═════════════════════════════════════════════════════════════════════════
def send_email(result: dict, queue_remaining: int):
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set — skipping email.")
        return False

    now = datetime.now(timezone.utc).strftime("%b %d, %Y at %H:%M UTC")
    slug = result["slug"]
    url = result.get("url", f"{WP_BASE_URL}/{slug}/")
    status = result["status"]

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
    else:
        subject = f"❌ FAILED: /{slug}/ — needs attention"
        body = f"""
        <div style="font-family: -apple-system, Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #ffebee; border-left: 4px solid #c62828; padding: 16px;">
                <h2 style="margin: 0; color: #c62828;">Publish Failed</h2>
            </div>
            <p><strong>Slug:</strong> <code>{slug}</code></p>
            <p><strong>Error:</strong></p>
            <pre style="background: #f5f5f5; padding: 12px; overflow-x: auto; font-size: 12px; border-radius: 4px;">
{result.get("error", "Unknown error")}</pre>
            <p style="color: #c62828;"><strong>Action needed:</strong> Check WP credentials and API access.
            Re-run manually from GitHub Actions after fixing.</p>
        </div>"""

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
        print(f"\n📊 Publish Status")
        print(f"{'─'*45}")
        print(f"  Published     : {len(ledger['published'])}")
        print(f"  Failed        : {len(ledger['failed'])}")
        print(f"  In queue      : {len(queue)}")
        print(f"  Last run      : {ledger.get('last_run', 'never')}")
        if queue:
            print(f"\n  Next 5 in queue:")
            for i, p in enumerate(queue[:5]):
                print(f"    {i+1}. /{p['_slug']}/  (P{p['_priority']}, W{p.get('wave','')})")
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
        target = next((p for p in queue if p["_slug"] == args.slug), None)
        if not target:
            sys.exit(f"ERROR: '{args.slug}' not in queue (already published or not found)")
    else:
        if not queue:
            log.info("✅ Queue empty — all pages published. Nothing to do.")
            return
        target = queue[0]

    log.info(f"{'[DRY RUN] ' if args.dry_run else ''}Target: /{target['_slug']}/")
    log.info(f"  City: {target['city']}, {target['state_abbreviation']}")

    if args.dry_run:
        content = render_location_page(template, target)
        print(f"\n{'='*55}")
        print(f"  DRY RUN — would publish:")
        print(f"  Slug   : {target['_slug']}")
        print(f"  City   : {target['city']}, {target['state_name']}")
        print(f"  Chars  : {len(content):,}")
        print(f"  After  : {len(queue) - 1} pages remain in queue")
        print(f"{'='*55}")
        return

    # ── Publish to WordPress ─────────────────────────────────────────
    log.info("  Publishing to WordPress...")
    result = publish_one_page(target, template)
    log.info(f"  → {result['status'].upper()}")

    # ── Update ledger ────────────────────────────────────────────────
    entry = {
        "slug": target["_slug"],
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