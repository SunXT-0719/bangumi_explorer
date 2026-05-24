import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "bangumi.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            name_cn TEXT,
            date TEXT,
            platform TEXT,
            type INTEGER,
            score REAL,
            rank INTEGER,
            rating_total INTEGER,
            rating_detail TEXT,
            tags TEXT,
            meta_tags TEXT,
            image_url TEXT,
            eps INTEGER,
            summary TEXT,
            raw_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS update_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL,
            old_score REAL,
            new_score REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subject_id) REFERENCES subjects(id)
        );

        CREATE TABLE IF NOT EXISTS tag_library (
            name TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            id INTEGER PRIMARY KEY DEFAULT 1,
            total_ids INTEGER DEFAULT 0,
            synced_count INTEGER DEFAULT 0,
            last_synced_id INTEGER DEFAULT 0,
            status TEXT DEFAULT 'idle',
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS known_ids (
            id INTEGER PRIMARY KEY,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_subjects_name ON subjects(name);
        CREATE INDEX IF NOT EXISTS idx_subjects_name_cn ON subjects(name_cn);
        CREATE INDEX IF NOT EXISTS idx_subjects_date ON subjects(date);
        CREATE INDEX IF NOT EXISTS idx_subjects_score ON subjects(score DESC);
        CREATE INDEX IF NOT EXISTS idx_subjects_rank ON subjects(rank);
        CREATE INDEX IF NOT EXISTS idx_tag_library_count ON tag_library(count DESC);
        CREATE INDEX IF NOT EXISTS idx_tag_library_name ON tag_library(name);
    """)

    # Ensure sync_state row exists
    conn.execute("INSERT OR IGNORE INTO sync_state (id) VALUES (1)")
    conn.commit()
    conn.close()


# ---- Subject CRUD ----

def upsert_subject(data):
    conn = get_db()
    old = conn.execute("SELECT score FROM subjects WHERE id = ?", (data["id"],)).fetchone()
    old_score = old["score"] if old else None

    conn.execute("""
        INSERT OR REPLACE INTO subjects
        (id, name, name_cn, date, platform, type, score, rank, rating_total,
         rating_detail, tags, meta_tags, image_url, eps, summary, raw_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["id"], data["name"], data.get("name_cn", ""),
        data.get("date", ""), data.get("platform", ""), data.get("type", 0),
        data.get("score", 0), data.get("rank", 0), data.get("rating_total", 0),
        json.dumps(data.get("rating_detail", {}), ensure_ascii=False),
        json.dumps(data.get("tags", []), ensure_ascii=False),
        json.dumps(data.get("meta_tags", []), ensure_ascii=False),
        data.get("image_url", ""), data.get("eps", 0),
        data.get("summary", ""),
        json.dumps(data, ensure_ascii=False),
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ))

    if old_score is not None and abs(old_score - data.get("score", 0)) > 0.001:
        conn.execute("INSERT INTO update_log (subject_id, old_score, new_score) VALUES (?, ?, ?)",
                     (data["id"], old_score, data.get("score", 0)))

    # Collect tags
    for tag_entry in data.get("tags", []):
        tag_name = tag_entry[0] if isinstance(tag_entry, list) else tag_entry
        conn.execute(
            "INSERT INTO tag_library (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1",
            (tag_name,))

    conn.commit()
    conn.close()
    return old_score


def upsert_subjects_batch(data_list):
    """Batch upsert for sync performance."""
    conn = get_db()
    tag_counts = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for data in data_list:
        conn.execute("""
            INSERT OR REPLACE INTO subjects
            (id, name, name_cn, date, platform, type, score, rank, rating_total,
             rating_detail, tags, meta_tags, image_url, eps, summary, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["id"], data["name"], data.get("name_cn", ""),
            data.get("date", ""), data.get("platform", ""), data.get("type", 0),
            data.get("score", 0), data.get("rank", 0), data.get("rating_total", 0),
            json.dumps(data.get("rating_detail", {}), ensure_ascii=False),
            json.dumps(data.get("tags", []), ensure_ascii=False),
            json.dumps(data.get("meta_tags", []), ensure_ascii=False),
            data.get("image_url", ""), data.get("eps", 0),
            data.get("summary", ""),
            json.dumps(data, ensure_ascii=False), now,
        ))
        for tag_entry in data.get("tags", []):
            tag_name = tag_entry[0] if isinstance(tag_entry, list) else tag_entry
            tag_counts[tag_name] = tag_counts.get(tag_name, 0) + 1

    for tag_name, count in tag_counts.items():
        conn.execute(
            "INSERT INTO tag_library (name, count) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET count = count + ?",
            (tag_name, count, count))

    conn.commit()
    conn.close()


# ---- Sync State ----

def get_sync_state():
    conn = get_db()
    row = conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else {}


def update_sync_state(**kwargs):
    conn = get_db()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values())
    conn.execute(f"UPDATE sync_state SET {sets} WHERE id = 1", vals)
    conn.commit()
    conn.close()


def get_known_ids():
    conn = get_db()
    rows = conn.execute("SELECT id FROM known_ids").fetchall()
    conn.close()
    return {r["id"] for r in rows}


def add_known_ids(ids):
    conn = get_db()
    conn.executemany("INSERT OR IGNORE INTO known_ids (id) VALUES (?)", [(i,) for i in ids])
    conn.commit()
    conn.close()


def get_synced_ids():
    """Get all subject IDs already in the database."""
    conn = get_db()
    rows = conn.execute("SELECT id FROM subjects").fetchall()
    conn.close()
    return {r["id"] for r in rows}


# ---- Local Search ----

def search_local(keyword=None, tags=None, date_from=None, date_to=None,
                 rank_from=None, rank_to=None,
                 sort_by="score", page=1, limit=10):
    conn = get_db()
    query = "SELECT * FROM subjects WHERE rating_total >= 10 AND rank > 0"
    params = []

    if keyword:
        query += " AND (name LIKE ? OR name_cn LIKE ?)"
        like_kw = f"%{keyword}%"
        params.extend([like_kw, like_kw])

    if tags:
        for tag in tags:
            query += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')  # coarse filter, refined below

    if date_from:
        query += " AND date >= ?"
        params.append(date_from if len(date_from) == 10 else f"{date_from}-01")

    if date_to:
        query += " AND date <= ?"
        params.append(date_to if len(date_to) == 10 else f"{date_to}-31")

    if rank_from is not None:
        query += " AND rank >= ?"
        params.append(int(rank_from))

    if rank_to is not None:
        query += " AND rank <= ?"
        params.append(int(rank_to))

    # Count total (before tag threshold)
    count_query = query.replace("SELECT *", "SELECT COUNT(*) as cnt")
    total = conn.execute(count_query, params).fetchone()["cnt"]

    # Sort
    order_map = {
        "score": "score DESC",
        "score_asc": "score ASC",
        "rank": "rank ASC",
        "rank_desc": "rank DESC",
        "time_asc": "date ASC",
        "time_desc": "date DESC",
    }
    order = order_map.get(sort_by, "score DESC")

    # For tag-filtered searches, fetch all matching rows for post-filtering
    if tags:
        query += f" ORDER BY {order}"
        rows = conn.execute(query, params).fetchall()
    else:
        query += f" ORDER BY {order}"
        offset = (page - 1) * limit
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()

    conn.close()

    results = []
    for row in rows:
        d = dict(row)
        d["rating_detail"] = json.loads(d.get("rating_detail", "{}"))
        d["tags"] = json.loads(d.get("tags", "[]"))
        d["meta_tags"] = json.loads(d.get("meta_tags", "[]"))
        # Compute primary tag names (>= 20% of max tag count) for display
        raw_tags = d["tags"]
        primary = []
        if raw_tags and isinstance(raw_tags[0], list):
            max_c = max(t[1] for t in raw_tags) if raw_tags else 1
            primary = [t[0] for t in raw_tags if t[1] >= max_c * 0.2]
        d["primary_tags"] = primary
        results.append(d)

    # Post-filter: apply tag count threshold (10% of max tag count)
    if tags and results:
        filtered = []
        for d in results:
            tag_entries = d["tags"]
            if not tag_entries:
                continue
            # Check if tags have count data (new format) or are plain strings (old format)
            has_counts = isinstance(tag_entries[0], list)
            if not has_counts:
                # Old format: no counts, skip threshold, just check tag exists
                tag_names = set(tag_entries)
                if all(t in tag_names for t in tags):
                    filtered.append(d)
                continue
            # New format: [["name", count], ...]
            tag_dict = {t[0]: t[1] for t in tag_entries}
            max_count = max(t[1] for t in tag_entries) if tag_entries else 1
            threshold = max_count * 0.05
            ok = True
            for t in tags:
                cnt = tag_dict.get(t, 0)
                if cnt < threshold:
                    ok = False
                    break
            if ok:
                filtered.append(d)
        total = len(filtered)
        # Apply pagination
        start = (page - 1) * limit
        results = filtered[start:start + limit]
    return results, total


def get_subjects_for_refresh(limit=100):
    """Get subjects that need rating refresh, prioritizing old-format tag entries."""
    conn = get_db()
    rows = conn.execute("""
        SELECT id FROM subjects
        WHERE rating_total >= 50
        ORDER BY
            CASE WHEN tags LIKE '[\"%' AND tags NOT LIKE '[[%' THEN 0 ELSE 1 END,
            updated_at ASC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [r["id"] for r in rows]


# ---- Tag Library ----

def search_tags(query, min_count=5, limit=30):
    conn = get_db()
    rows = conn.execute("""
        SELECT name, count FROM tag_library
        WHERE count >= ? AND name LIKE ?
        ORDER BY count DESC LIMIT ?
    """, (min_count, f"%{query}%", limit)).fetchall()
    conn.close()
    return [{"name": r["name"], "count": r["count"]} for r in rows]


def get_popular_tags(min_count=50, limit=100):
    conn = get_db()
    rows = conn.execute("""
        SELECT name, count FROM tag_library
        WHERE count >= ? ORDER BY count DESC LIMIT ?
    """, (min_count, limit)).fetchall()
    conn.close()
    return [{"name": r["name"], "count": r["count"]} for r in rows]


def seed_tag_library_from_subjects():
    conn = get_db()
    rows = conn.execute("SELECT tags FROM subjects").fetchall()
    for row in rows:
        tags = json.loads(row["tags"] or "[]")
        for tag_entry in tags:
            tag_name = tag_entry[0] if isinstance(tag_entry, list) else tag_entry
            conn.execute(
                "INSERT INTO tag_library (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1",
                (tag_name,))
    conn.commit()
    conn.close()


def get_db_stats():
    conn = get_db()
    subjects = conn.execute("SELECT COUNT(*) as cnt FROM subjects").fetchone()["cnt"]
    tags = conn.execute("SELECT COUNT(*) as cnt FROM tag_library").fetchone()["cnt"]
    sync = dict(conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone())
    conn.close()
    return {"subjects": subjects, "tags": tags, "sync": sync}
