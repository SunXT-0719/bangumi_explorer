import json
import calendar
import threading
import time
import requests
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify

from database import (
    init_db, upsert_subject, search_local, get_db_stats,
    search_tags, get_popular_tags, seed_tag_library_from_subjects,
    get_subjects_for_refresh, upsert_subjects_batch,
)

app = Flask(__name__)

BANGUMI_API = "https://api.bgm.tv"
HEADERS = {"User-Agent": "bangumi-analysis/0.1 (academic project; contact@example.com)"}
PAGE_SIZE = 10
MIN_VOTES = 10

# Background updater
_updater_thread = None
_updater_stop = False


def _make_api_call(keyword, tags, date_from, date_to):
    filters = {"type": [2]}
    if tags:
        filters["tag"] = tags
    if date_from or date_to:
        airdate = []
        if date_from:
            d = date_from if len(date_from) == 10 else f"{date_from}-01"
            airdate.append(f">={d}")
        if date_to:
            d = date_to if len(date_to) == 10 else f"{date_to}-12-31"
            if len(date_to) == 7:
                y, m = int(date_to[:4]), int(date_to[5:7])
                d = f"{date_to}-{calendar.monthrange(y, m)[1]:02d}"
            airdate.append(f"<={d}")
        filters["air_date"] = airdate

    resp = requests.post(
        f"{BANGUMI_API}/v0/search/subjects",
        json={"keyword": keyword, "filter": filters, "limit": PAGE_SIZE},
        headers=HEADERS, timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []), data.get("total", 0)


def calc_score(count):
    if not count: return 0
    tw = sum(int(k) * int(v) for k, v in count.items())
    tv = sum(int(v) for v in count.values())
    return round(tw / tv, 2) if tv > 0 else 0


def normalize_subject(item):
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


def fetch_subject_detail(sid):
    try:
        resp = requests.get(f"{BANGUMI_API}/v0/subjects/{sid}", headers=HEADERS, timeout=12)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return normalize_subject(resp.json())
    except Exception:
        return None


def background_updater(interval_minutes=60):
    """Periodically refresh ratings for subjects in the database."""
    global _updater_stop
    print(f"[Updater] Started, interval={interval_minutes}min")
    while not _updater_stop:
        _updater_stop_event.wait(interval_minutes * 60)
        if _updater_stop:
            break
        try:
            ids = get_subjects_for_refresh(limit=300)
            if ids:
                print(f"[Updater] Refreshing {len(ids)} subjects...")
                batch = []
                success = 0
                for sid in ids:
                    data = fetch_subject_detail(sid)
                    if data:
                        batch.append(data)
                        success += 1
                        if len(batch) >= 50:
                            upsert_subjects_batch(batch)
                            batch = []
                    time.sleep(0.3)
                if batch:
                    upsert_subjects_batch(batch)
                print(f"[Updater] Refreshed {success}/{len(ids)} subjects")
        except Exception as e:
            print(f"[Updater] Error: {e}")


_updater_stop_event = threading.Event()


def start_updater(interval_minutes=60):
    global _updater_thread, _updater_stop
    if _updater_thread and _updater_thread.is_alive():
        return
    _updater_stop = False
    _updater_thread = threading.Thread(target=background_updater, args=(interval_minutes,), daemon=True)
    _updater_thread.start()


# ---- Routes ----

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    keyword = request.args.get("keyword", "").strip()
    tags_json = request.args.get("tags", "[]")
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    rank_from = request.args.get("rank_from", "").strip()
    rank_to = request.args.get("rank_to", "").strip()
    page = request.args.get("page", 1, type=int)
    sort_field = request.args.get("sort_field", "rank")
    sort_order = request.args.get("sort_order", "desc")
    # Map to internal format: score, rank, time_asc, time_desc
    if sort_field == "rank":
        sort_by = "rank" if sort_order == "asc" else "rank_desc"
    elif sort_field == "time":
        sort_by = "time_asc" if sort_order == "asc" else "time_desc"
    else:
        sort_by = "score"  # score always uses the order in DB query
    # Store effective sort_order for score inversion
    if sort_field == "score" and sort_order == "asc":
        sort_by = "score_asc"

    try:
        tags = json.loads(tags_json)
    except (json.JSONDecodeError, TypeError):
        tags = []

    # Search local DB first
    results, total = search_local(
        keyword=keyword, tags=tags,
        date_from=date_from, date_to=date_to,
        rank_from=rank_from if rank_from else None,
        rank_to=rank_to if rank_to else None,
        sort_by=sort_by, page=page, limit=PAGE_SIZE,
    )

    # If local DB has results, use them
    if total > 0:
        return jsonify({
            "total": total, "page": page, "limit": PAGE_SIZE,
            "results": results, "source": "local",
        })

    # Fallback: search bangumi API
    try:
        items, api_total = _make_api_call(keyword, tags, date_from, date_to)
        results = []
        for item in items:
            normalized = normalize_subject(item)
            if normalized["rating_total"] >= MIN_VOTES:
                upsert_subject(normalized)
                results.append(normalized)

        return jsonify({
            "total": min(api_total, len(results)),
            "page": 1, "limit": PAGE_SIZE,
            "results": results, "source": "api",
        })
    except requests.RequestException as e:
        return jsonify({"error": f"API 请求失败: {str(e)}", "results": []}), 502


@app.route("/api/tags/autocomplete")
def api_tags_autocomplete():
    query = request.args.get("q", "").strip()
    if query:
        results = search_tags(query, min_count=5, limit=20)
    else:
        results = get_popular_tags(min_count=50, limit=50)
    return jsonify(results)


@app.route("/api/subject/<int:subject_id>")
def api_subject_detail(subject_id):
    try:
        detail = fetch_subject_detail(subject_id)
        if detail:
            upsert_subject(detail)
            return jsonify(detail)
        return jsonify({"error": "Subject not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/subject/<int:subject_id>/refresh")
def api_refresh_subject(subject_id):
    data = fetch_subject_detail(subject_id)
    if data:
        upsert_subject(data)
        return jsonify({"message": "refreshed", "score": data["score"]})
    return jsonify({"error": "Refresh failed"}), 502


@app.route("/api/stats")
def api_stats():
    return jsonify(get_db_stats())


if __name__ == "__main__":
    init_db()
    seed_tag_library_from_subjects()
    start_updater(interval_minutes=30)
    app.run(debug=False, port=8080)
