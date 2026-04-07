#!/usr/bin/env python3
"""
Delete all tax-attorney-* pages from WordPress CMS and reset the publish ledger.
Run once to start fresh.

Usage:
  python cleanup_wp.py --dry-run    # Show what would be deleted
  python cleanup_wp.py              # Actually delete + reset ledger
"""

import os, sys, json, argparse
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

WP_BASE_URL = os.getenv("WP_BASE_URL", "https://cms.neiljesanitaxresolution.com")
WP_USERNAME = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
LEDGER_PATH = Path("./publish_ledger.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not WP_USERNAME or not WP_APP_PASSWORD:
        sys.exit("ERROR: Set WP_USERNAME and WP_APP_PASSWORD")

    session = requests.Session()
    session.auth = (WP_USERNAME, WP_APP_PASSWORD)

    # Fetch all pages
    all_pages = []
    page_num = 1
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
        all_pages.extend(pages)
        page_num += 1

    # Filter tax-attorney pages
    to_delete = [p for p in all_pages if p.get("slug", "").startswith("tax-attorney-")]

    print(f"\nFound {len(to_delete)} tax-attorney pages to delete:\n")
    for p in to_delete:
        print(f"  ID {p['id']:>4} — /{p['slug']}/")

    if not to_delete:
        print("Nothing to delete.")
        return

    if args.dry_run:
        print(f"\n[DRY RUN] Would delete {len(to_delete)} pages. Run without --dry-run to proceed.")
        return

    # Confirm
    confirm = input(f"\nDelete {len(to_delete)} pages from WordPress? Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return

    # Delete each page (force=true permanently deletes, skips trash)
    deleted = 0
    for p in to_delete:
        resp = session.delete(
            f"{WP_BASE_URL.rstrip('/')}/wp-json/wp/v2/pages/{p['id']}",
            params={"force": True},
        )
        if resp.status_code == 200:
            print(f"  ✓ Deleted: /{p['slug']}/ (ID {p['id']})")
            deleted += 1
        else:
            print(f"  ✗ Failed: /{p['slug']}/ — {resp.status_code}: {resp.text[:100]}")

    print(f"\nDeleted {deleted}/{len(to_delete)} pages.")

    # Reset ledger
    if LEDGER_PATH.exists():
        LEDGER_PATH.unlink()
        print("✓ publish_ledger.json deleted (fresh start)")
    else:
        print("No ledger file found — already clean.")

    print("\n✅ Done. WordPress is clean, ledger is reset.")


if __name__ == "__main__":
    main()