"""
Sync all bangumi anime data from bangumi API using IDs from bangumi-data.

Usage:
    python sync_db.py          # Full sync from scratch
    python sync_db.py --update # Refresh ratings for existing subjects
    python sync_db.py --status # Show sync status
"""

import json
import os
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from database import (
    init_db, get_db, upsert_subjects_batch, add_known_ids,
    get_known_ids, get_synced_ids, get_sync_state, update_sync_state,
    get_subjects_for_refresh, get_db_stats,
)

BANGUMI_DATA_DIR = "/tmp/bangumi-data-investigation/data/items"
API = "https://api.bgm.tv/v0/subjects"
HEADERS = {"User-Agent": "bangumi-analysis/0.1 (academic project; contact@example.com)"}
WORKERS = 12
BATCH_SIZE = 50


def calc_score(count):
    if not count: return 0
    tw = sum(int(k) * int(v) for k, v in count.items())
    tv = sum(int(v) for v in count.values())
    return round(tw / tv, 2) if tv > 0 else 0


def normalize_detail(item):
    rating = item.get("rating", {}) or {}
    images = item.get("images", {}) or {}
    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "name_cn": item.get("name_cn", ""),
        "date": item.get("date", ""),
        "platform": item.get("platform", ""),
        "type": item.get("type", 0),
        "score": calc_score(rating.get("count", {})),
        "rank": rating.get("rank", 0),
        "rating_total": rating.get("total", 0),
        "rating_detail": rating.get("count", {}),
        "tags": [[t["name"], t.get("count", 0)] for t in item.get("tags", [])],
        "meta_tags": item.get("meta_tags", []),
        "image_url": images.get("common", images.get("medium", "")),
        "eps": item.get("eps", 0),
        "summary": item.get("summary", ""),
    }


def collect_bangumi_ids():
    """Extract all bangumi.tv IDs from bangumi-data repo."""
    ids = set()
    for year_dir in sorted(os.listdir(BANGUMI_DATA_DIR)):
        path = os.path.join(BANGUMI_DATA_DIR, year_dir)
        if not os.path.isdir(path):
            continue
        for fname in sorted(os.listdir(path)):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(path, fname)) as f:
                for item in json.load(f):
                    for site in item.get("sites", []):
                        if site.get("site") == "bangumi":
                            ids.add(int(site["id"]))
    return sorted(ids)


def fetch_subject(sid):
    """Fetch a single subject detail. Returns normalized data or None."""
    try:
        resp = requests.get(f"{API}/{sid}", headers=HEADERS, timeout=12)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return normalize_detail(resp.json())
    except Exception:
        return None


def full_sync():
    """Full sync: fetch all anime from bangumi-data IDs."""
    print("Collecting bangumi IDs from bangumi-data...")
    all_ids = collect_bangumi_ids()
    print(f"Found {len(all_ids)} unique bangumi IDs")

    # Store known IDs
    add_known_ids(all_ids)

    # Determine which IDs need fetching
    synced = get_synced_ids()
    to_fetch = [i for i in all_ids if i not in synced]
    print(f"Already synced: {len(synced)}, need to fetch: {len(to_fetch)}")

    if not to_fetch:
        print("All IDs already synced!")
        return

    update_sync_state(
        total_ids=len(all_ids), synced_count=len(synced),
        status="running", started_at=datetime.now(timezone.utc).isoformat(),
    )

    # Fetch in batches
    batch = []
    success = 0
    fail = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {}
        # Submit initial batch
        for sid in to_fetch[:WORKERS * 2]:
            futures[executor.submit(fetch_subject, sid)] = sid

        idx = len(futures)
        done = 0
        total = len(to_fetch)

        while futures:
            for future in as_completed(futures):
                sid = futures.pop(future)
                done += 1
                result = future.result()

                if result is not None:
                    batch.append(result)
                    success += 1
                    if len(batch) >= BATCH_SIZE:
                        upsert_subjects_batch(batch)
                        batch = []
                else:
                    fail += 1

                # Submit next
                if idx < total:
                    futures[executor.submit(fetch_subject, to_fetch[idx])] = to_fetch[idx]
                    idx += 1

                # Progress
                if done % 200 == 0 or done == total:
                    elapsed = time.time() - start_time
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    print(f"  {done}/{total} ({done*100//total}%) "
                          f"success={success} fail={fail} "
                          f"rate={rate:.1f}/s eta={eta/60:.0f}min")

        # Flush remaining batch
        if batch:
            upsert_subjects_batch(batch)

    elapsed = time.time() - start_time
    print(f"\nSync complete in {elapsed/60:.1f}min: {success} fetched, {fail} failed")

    update_sync_state(
        synced_count=len(get_synced_ids()),
        status="completed",
        finished_at=datetime.now(timezone.utc).isoformat(),
    )

    # Rebuild tag library
    print("Rebuilding tag library...")
    db = get_db()
    db.execute("DELETE FROM tag_library")
    db.commit()
    from database import seed_tag_library_from_subjects
    seed_tag_library_from_subjects()
    print("Done.")


def update_ratings():
    """Refresh ratings for subjects that haven't been updated recently."""
    ids = get_subjects_for_refresh(limit=200)
    if not ids:
        print("No subjects need refresh right now.")
        return

    print(f"Refreshing ratings for {len(ids)} subjects...")
    success = 0
    batch = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(fetch_subject, sid): sid for sid in ids}
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                batch.append(result)
                success += 1
                if len(batch) >= BATCH_SIZE:
                    upsert_subjects_batch(batch)
                    batch = []

        if batch:
            upsert_subjects_batch(batch)

    print(f"Updated {success}/{len(ids)} subjects")


if __name__ == "__main__":
    init_db()

    if "--status" in sys.argv:
        stats = get_db_stats()
        print(f"Subjects in DB: {stats['subjects']}")
        print(f"Tags in library: {stats['tags']}")
        s = stats['sync']
        print(f"Sync status: {s['status']} ({s['synced_count']}/{s['total_ids']})")
        if s.get('finished_at'):
            print(f"Last sync: {s['finished_at']}")
    elif "--update" in sys.argv:
        update_ratings()
    else:
        full_sync()
