#!/usr/bin/env python3
"""
NeilJesaniTaxResolution.com — Daily Drip Publisher
===================================================
Each day publishes:
  - 1 location page (generates unique paragraphs via Claude API)
  - 1 IRS notice page (from Excel data model)
Sends email notification via Resend after each run.

Usage:
  python daily_publish.py --status
  python daily_publish.py --dry-run
  python daily_publish.py
  python daily_publish.py --slug tax-attorney-miami-fl
  python daily_publish.py --type notices-only
  python daily_publish.py --type locations-only
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

from generate_pages import (
    load_location_data,
    load_notice_data,
    render_location_page,
    render_notice_page,
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
# CONTENT AUDIT — check for banned words before publishing
# ═════════════════════════════════════════════════════════════════════════
BANNED_PHRASES = [
    "tax court", "us tax court", "u.s. tax court",
    "litigation", "litigate",
    "criminal", "criminal investigation",
    "trial", "courtroom", "court-admitted",
    "court proceedings", "prosecute", "prosecution",
]

def audit_content(text: str) -> list[str]:
    """Check text for banned phrases. Returns list of violations found."""
    violations = []
    lower = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            violations.append(phrase)
    return violations


# ═════════════════════════════════════════════════════════════════════════
# LEDGER
# ═════════════════════════════════════════════════════════════════════════
def load_ledger() -> dict:
    if PUBLISH_LEDGER.exists():
        return json.loads(PUBLISH_LEDGER.read_text())
    return {"published": [], "published_notices": [], "failed": []}

def save_ledger(ledger: dict):
    ledger["last_run"] = datetime.now(timezone.utc).isoformat()
    # Ensure notices key exists for older ledgers
    if "published_notices" not in ledger:
        ledger["published_notices"] = []
    PUBLISH_LEDGER.write_text(json.dumps(ledger, indent=2))


# ═════════════════════════════════════════════════════════════════════════
# SYNC — pull already-published pages from WordPress into the ledger
# ═════════════════════════════════════════════════════════════════════════
def sync_ledger_with_wp(ledger: dict):
    log.info("Syncing ledger with existing WordPress pages...")
    published_slugs = {e["slug"] for e in ledger["published"]}
    notice_slugs = {e["slug"] for e in ledger.get("published_notices", [])}
    session = requests.Session()
    session.auth = (os.getenv("WP_USERNAME", ""), os.getenv("WP_APP_PASSWORD", ""))

    page_num = 1
    loc_added = 0
    notice_added = 0
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
                    "synced": True,
                })
                loc_added += 1
            elif slug.startswith("irs-notice-") and slug not in notice_slugs:
                ledger["published_notices"].append({
                    "slug": slug,
                    "date": p.get("date", "synced"),
                    "wp_id": p.get("id"),
                    "synced": True,
                })
                notice_added += 1
        page_num += 1
    log.info(f"  Synced: {loc_added} location + {notice_added} notice entries added")


def _parse_priority(val) -> int:
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
# LOCATION QUEUE
# ═════════════════════════════════════════════════════════════════════════
def build_location_queue(ledger: dict) -> list[dict]:
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

        city_str = str(city)
        state_str = str(state_abbr)
        city_slug = city_str.lower().replace(" ", "-").replace(".", "")
        slug = f"tax-attorney-{city_slug}-{state_str.lower()}"

        if slug in published_slugs:
            continue

        needs_generation = (
            not unique1 or str(unique1).startswith("[WRITE:") or
            not unique2 or str(unique2).startswith("[WRITE:")
        )

        queue.append({
            "slug": slug, "city": city_str, "state_abbreviation": state_str,
            "metro": str(metro or ""), "irs_addr": str(irs_addr or ""),
            "irs_phone": str(irs_phone or ""), "tax_rate": str(tax_rate or ""),
            "agency": str(agency or ""), "agency_url": str(agency_url or ""),
            "pop": str(pop or ""), "income": str(income or ""),
            "nearby": str(nearby or ""), "unique1": str(unique1 or ""),
            "unique2": str(unique2 or ""), "court": str(court or ""),
            "wave": str(wave or ""), "priority": _parse_priority(priority),
            "needs_generation": needs_generation,
        })

    queue.sort(key=lambda x: (x["priority"], x["city"]))
    return queue


# ═════════════════════════════════════════════════════════════════════════
# NOTICE QUEUE
# ═════════════════════════════════════════════════════════════════════════
def build_notice_queue(ledger: dict) -> list[dict]:
    published_slugs = {e["slug"] for e in ledger.get("published_notices", [])}
    notices = load_notice_data(DATA_MODEL_PATH)

    queue = []
    for notice in notices:
        slug = f"irs-notice-{notice['code'].lower()}"
        if slug in published_slugs:
            continue
        notice["slug"] = slug
        notice["_priority"] = _parse_priority(notice.get("priority"))
        queue.append(notice)

    queue.sort(key=lambda x: (x["_priority"], x["code"]))
    return queue


# ═════════════════════════════════════════════════════════════════════════
# GENERATE PARAGRAPHS FOR ONE CITY
# ═════════════════════════════════════════════════════════════════════════
def generate_for_one_city(city_data: dict) -> tuple[str, str]:
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
        client, city_data["city"], state_name, city_data["state_abbreviation"],
        city_data["metro"], city_data["tax_rate"], city_data["agency"], city_data["nearby"],
    )
    log.info(f"  Generated paragraphs: {len(para1)} + {len(para2)} chars")
    return para1, para2


# ═════════════════════════════════════════════════════════════════════════
# PUBLISH LOCATION PAGE
# ═════════════════════════════════════════════════════════════════════════
def publish_location_page(city_data: dict, para1: str, para2: str, template: str) -> dict:
    from generate_pages import STATE_TAX_CONTEXT, get_state_name

    state_abbr = city_data["state_abbreviation"]
    state_ctx = STATE_TAX_CONTEXT.get(state_abbr, {})

    row = {
        "city": city_data["city"], "state_abbreviation": state_abbr,
        "state_name": get_state_name(state_abbr), "metro_area": city_data["metro"],
        "local_irs_office_address": city_data["irs_addr"] or "[VERIFY: IRS.gov]",
        "local_irs_phone": city_data["irs_phone"] or "[VERIFY: IRS.gov]",
        "state_income_tax_rate": city_data["tax_rate"],
        "state_tax_agency_name": city_data["agency"],
        "state_tax_agency_url": city_data["agency_url"],
        "population_est": city_data["pop"], "median_hhi_est": city_data["income"],
        "nearby_cities": city_data["nearby"],
        "unique_local_paragraph_1": para1, "unique_local_paragraph_2": para2,
        "local_court_info": city_data["court"],
        "state_tax_context_sentence": state_ctx.get("sentence", ""),
        "state_tax_context_detail": state_ctx.get("detail", ""),
        "cta_phone": CTA_PHONE,
    }

    slug = city_data["slug"]
    title = f"Tax Attorney in {row['city']}, {row['state_name']}: IRS Audit & Tax Resolution Help"
    meta_desc = (
        f"Facing the IRS in {row['city']}? Neil Jesani's team of tax attorneys, CPAs, "
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
# PUBLISH NOTICE PAGE
# ═════════════════════════════════════════════════════════════════════════
def publish_notice_page(notice: dict) -> dict:
    slug = notice["slug"]
    # WordPress slug uses hyphens: irs-notice-cp2000 (not irs-notice/cp2000)
    wp_slug = slug
    title = f"IRS Notice {notice['code']}: {notice['name']} — What It Means & How to Respond"
    meta_desc = (
        f"Received IRS Notice {notice['code']}? Learn what {notice['name']} means, "
        f"your deadline to respond, and how Neil Jesani's tax attorneys can help. "
        f"Free consultation: {CTA_PHONE}."
    )
    content = render_notice_page(notice)

    publisher = WordPressPublisher()
    result = publisher.publish_page(slug=wp_slug, title=title, content=content, meta_description=meta_desc)

    if result.get("skipped"):
        return {"status": "skipped", "slug": slug}
    elif result.get("success"):
        return {"status": "published", "slug": slug, "id": result.get("id"), "url": f"{WP_BASE_URL}/{wp_slug}/"}
    else:
        return {"status": "failed", "slug": slug, "error": result.get("error", "Unknown")}


# ═════════════════════════════════════════════════════════════════════════
# EMAIL via Resend
# ═════════════════════════════════════════════════════════════════════════
def send_email(results: list[dict], loc_remaining: int, notice_remaining: int):
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set — skipping email.")
        return

    now = datetime.now(timezone.utc).strftime("%b %d, %Y at %H:%M UTC")

    # Build rows for each result
    rows_html = ""
    all_ok = True
    for r in results:
        slug = r["slug"]
        status = r["status"]
        frontend_url = f"{FRONTEND_URL}/{slug}/"
        cms_url = r.get("url", f"{WP_BASE_URL}/{slug}/")
        page_type = "Notice" if slug.startswith("irs-notice") else "Location"

        if status == "published":
            status_icon = "✅"
            url_html = f'<a href="{frontend_url}" style="color:#1565c0">{frontend_url}</a>'
        elif status == "skipped":
            status_icon = "⏭️"
            url_html = f"<em>Already exists</em>"
        else:
            status_icon = "❌"
            url_html = f'<code style="font-size:11px">{r.get("error","Unknown")[:100]}</code>'
            all_ok = False

        rows_html += f"""<tr>
            <td style="padding:8px 0">{status_icon} {page_type}</td>
            <td style="padding:8px 0"><code>{slug}</code></td>
            <td style="padding:8px 0">{url_html}</td>
        </tr>"""

    subject = f"{'✅' if all_ok else '⚠️'} Daily Publish: {len(results)} pages — {now[:6]}"
    body = f"""
    <div style="font-family: -apple-system, Arial, sans-serif; max-width: 640px; margin: 0 auto;">
        <div style="background: {'#e8f5e9' if all_ok else '#fff3e0'}; border-left: 4px solid {'#2e7d32' if all_ok else '#f57c00'}; padding: 16px; margin-bottom: 20px;">
            <h2 style="margin: 0; color: {'#2e7d32' if all_ok else '#e65100'};">Daily Publish Report</h2>
        </div>
        <table style="width: 100%; border-collapse: collapse;">
            <thead><tr style="border-bottom: 1px solid #eee;">
                <th style="padding:8px 0; text-align:left; width:100px;">Status</th>
                <th style="padding:8px 0; text-align:left;">Slug</th>
                <th style="padding:8px 0; text-align:left;">URL</th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        <div style="background: #f5f5f5; padding: 14px; margin-top: 20px; border-radius: 6px;">
            <strong>Queue remaining:</strong> {loc_remaining} location pages, {notice_remaining} notice pages<br>
            <strong>Published at:</strong> {now}<br>
            Next auto-publish: tomorrow 9 AM EST
        </div>
        <p style="color: #999; font-size: 11px; margin-top: 24px;">daily_publish.py via GitHub Actions</p>
    </div>"""

    try:
        resp = requests.post("https://api.resend.com/emails", headers={
            "Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json",
        }, json={
            "from": EMAIL_FROM,
            "to": EMAIL_TO[0],
            "cc": EMAIL_TO[1:],
            "subject": subject,
            "html": body,
        })
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
    parser = argparse.ArgumentParser(description="Daily publisher — 1 location + 1 notice per day")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--slug", help="Force a specific location slug")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--type", choices=["both", "locations-only", "notices-only"], default="both")
    args = parser.parse_args()

    ledger = load_ledger()

    if args.sync:
        sync_ledger_with_wp(ledger)
        save_ledger(ledger)
        return

    loc_queue = build_location_queue(ledger)
    notice_queue = build_notice_queue(ledger)

    # ── Status ───────────────────────────────────────────────────────
    if args.status:
        print(f"\n📊 Publish Status")
        print(f"{'─'*50}")
        print(f"  Location pages published : {len(ledger['published'])}")
        print(f"  Notice pages published   : {len(ledger.get('published_notices', []))}")
        print(f"  Failed                   : {len(ledger['failed'])}")
        print(f"  Location queue           : {len(loc_queue)}")
        print(f"  Notice queue             : {len(notice_queue)}")
        print(f"  Last run                 : {ledger.get('last_run', 'never')}")
        if loc_queue:
            print(f"\n  Next 3 location pages:")
            for i, p in enumerate(loc_queue[:3]):
                gen = "⚡ needs AI" if p["needs_generation"] else "✅ ready"
                print(f"    {i+1}. /{p['slug']}/  [{gen}]")
        if notice_queue:
            print(f"\n  Next 3 notice pages:")
            for i, n in enumerate(notice_queue[:3]):
                print(f"    {i+1}. /{n['slug']}/  ({n['code']} — {n['name'][:40]})")
        if not loc_queue and not notice_queue:
            print(f"\n  ✅ All pages published!")
        return

    # ── Load template ────────────────────────────────────────────────
    tpl = Path(TEMPLATE_PATH)
    if not tpl.exists():
        sys.exit(f"ERROR: Template not found: {TEMPLATE_PATH}")
    template = tpl.read_text(encoding="utf-8")

    publish_results = []

    # ══════════════════════════════════════════════════════════════════
    # LOCATION PAGE
    # ══════════════════════════════════════════════════════════════════
    if args.type in ("both", "locations-only") and loc_queue:
        if args.slug:
            target = next((p for p in loc_queue if p["slug"] == args.slug), None)
            if not target:
                log.error(f"Slug '{args.slug}' not in queue")
                target = None
        else:
            target = loc_queue[0]

        if target:
            log.info(f"{'[DRY RUN] ' if args.dry_run else ''}📍 Location: /{target['slug']}/")
            log.info(f"  City: {target['city']}, {target['state_abbreviation']}")

            if not args.dry_run:
                # Generate paragraphs
                if target["needs_generation"]:
                    log.info("  Generating paragraphs via Claude API...")
                    para1, para2 = generate_for_one_city(target)
                else:
                    para1, para2 = target["unique1"], target["unique2"]

                # Audit content
                MAX_RETRIES = 3
                for attempt in range(MAX_RETRIES):
                    violations = audit_content(para1 + " " + para2)
                    if not violations:
                        break
                    log.warning(f"  ⚠️  Banned words: {violations} — regenerating ({attempt + 2}/{MAX_RETRIES})")
                    para1, para2 = generate_for_one_city(target)
                else:
                    violations = audit_content(para1 + " " + para2)
                    if violations:
                        log.error(f"  ❌ Banned words after {MAX_RETRIES} retries: {violations}")
                        ledger["failed"].append({
                            "slug": target["slug"], "date": datetime.now(timezone.utc).isoformat(),
                            "error": f"Content audit failed: {violations}",
                        })
                        publish_results.append({"status": "failed", "slug": target["slug"], "error": f"Banned words: {violations}"})
                        target = None

                if target:
                    result = publish_location_page(target, para1, para2, template)
                    log.info(f"  → {result['status'].upper()}")
                    publish_results.append(result)

                    entry = {"slug": target["slug"], "city": target["city"],
                             "state": target["state_abbreviation"],
                             "date": datetime.now(timezone.utc).isoformat()}
                    if result["status"] == "published":
                        entry["wp_id"] = result.get("id")
                        ledger["published"].append(entry)
                    elif result["status"] == "failed":
                        entry["error"] = result.get("error", "")[:200]
                        ledger["failed"].append(entry)
            else:
                print(f"  [DRY RUN] Would publish location: {target['slug']}")
    elif args.type in ("both", "locations-only"):
        log.info("📍 Location queue empty — all published.")

    # ══════════════════════════════════════════════════════════════════
    # NOTICE PAGE
    # ══════════════════════════════════════════════════════════════════
    if args.type in ("both", "notices-only") and notice_queue:
        notice = notice_queue[0]
        log.info(f"{'[DRY RUN] ' if args.dry_run else ''}📋 Notice: /{notice['slug']}/ ({notice['code']})")

        if not args.dry_run:
            # Audit notice content
            notice_html = render_notice_page(notice)
            violations = audit_content(notice_html)
            if violations:
                log.warning(f"  ⚠️  Notice template has banned words: {violations}")

            result = publish_notice_page(notice)
            log.info(f"  → {result['status'].upper()}")
            publish_results.append(result)

            entry = {"slug": notice["slug"], "code": notice["code"],
                     "date": datetime.now(timezone.utc).isoformat()}
            if result["status"] == "published":
                entry["wp_id"] = result.get("id")
                ledger.setdefault("published_notices", []).append(entry)
            elif result["status"] == "failed":
                entry["error"] = result.get("error", "")[:200]
                ledger["failed"].append(entry)
        else:
            print(f"  [DRY RUN] Would publish notice: {notice['slug']} ({notice['code']})")
    elif args.type in ("both", "notices-only"):
        log.info("📋 Notice queue empty — all published.")

    # ── Save + Email ─────────────────────────────────────────────────
    if not args.dry_run:
        save_ledger(ledger)
        log.info(f"  Ledger: {len(ledger['published'])} locations + {len(ledger.get('published_notices', []))} notices published")

        if publish_results:
            loc_remaining = len(loc_queue) - sum(1 for r in publish_results if r["status"] == "published" and not r["slug"].startswith("irs-notice"))
            notice_remaining = len(notice_queue) - sum(1 for r in publish_results if r["status"] == "published" and r["slug"].startswith("irs-notice"))
            send_email(publish_results, loc_remaining, notice_remaining)
    else:
        print(f"\n  Location queue: {len(loc_queue)} remaining")
        print(f"  Notice queue: {len(notice_queue)} remaining")

    log.info("🏁 Done.")


if __name__ == "__main__":
    main()