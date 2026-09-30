"""Append-only application ledger. Hash chained, NOT externally notarized."""
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from .engine import Rules, VERSION, canonical, condition, dataset_hash, iso, validate


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS research (
            id INTEGER PRIMARY KEY,
            slot TEXT NOT NULL UNIQUE,
            published_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS research_no_update BEFORE UPDATE ON research
        BEGIN SELECT RAISE(ABORT, 'Research records are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS research_no_delete BEFORE DELETE ON research
        BEGIN SELECT RAISE(ABORT, 'Research records are append-only'); END;
    ''')
    return conn


def digest(previous, payload):
    return hashlib.sha256((previous + "\n" + payload).encode()).hexdigest()


def publish(path, bars, source, now=None):
    """Publish only the current schedule slot; historical backfill is not supported.

    now is a testing seam; production CLI never accepts a time override.
    """
    validate(bars)
    if not source.strip():
        raise ValueError("Source is required")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    now = now.astimezone(timezone.utc)
    rules = Rules()
    slot = now.replace(hour=(now.hour // rules.update_hours) * rules.update_hours,
                       minute=0, second=0, microsecond=0)
    # Never retrospectively fill an old slot with data learned later.
    if (now - slot).total_seconds() > 1800:
        raise ValueError("Publish within 30 minutes of 00:00, 08:00, or 16:00 UTC")
    known = [b for b in bars if b.end <= now and b.available_at <= now]
    result = condition(known, now, rules)
    payload = {"record_type": "forward_paper_research", "slot": iso(slot),
               "published_at": iso(now), "source": source, "version": VERSION,
               "input_sha256": dataset_hash(known), "research": result,
               "notice": "Paper research, not a filled trade or verified profit."}
    encoded = canonical(payload)
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT payload FROM research WHERE slot=?", (iso(slot),)).fetchone()
        if existing:
            conn.rollback()
            return json.loads(existing["payload"]), False
        previous = conn.execute("SELECT record_hash FROM research ORDER BY id DESC LIMIT 1").fetchone()
        previous_hash = previous["record_hash"] if previous else "0" * 64
        conn.execute("INSERT INTO research(slot,published_at,payload,previous_hash,record_hash) VALUES (?,?,?,?,?)",
                     (iso(slot), iso(now), encoded, previous_hash, digest(previous_hash, encoded)))
        conn.commit()
        return payload, True
    finally:
        conn.close()


def latest_id(path):
    """The highest record id currently in the ledger, or None if empty.
    Read-only. Used to compute an eligibility ceiling (e.g. for a Free
    tier) independently of any caller-supplied pagination cursor."""
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(id) FROM research").fetchone()
        return row[0]
    finally:
        conn.close()


def history(path, limit=500, before=None, max_id=None):
    """max_id, if given, caps every row returned to id <= max_id — enforced
    in this query's WHERE clause, so no value of `before` (however large a
    caller supplies) can ever return a row above it. Pass the result of
    latest_id() minus however many of the newest rows should stay withheld;
    never derive this ceiling from a caller-supplied parameter."""
    if not Path(path).exists():
        return {"records": [], "next_before": None, "integrity": "empty",
                "notice": "No forward research has been published."}
    # Readers never create a database or modify schema.
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        limit = max(1, min(int(limit), 500))
        ceiling = before if before is not None else 9223372036854775807
        if max_id is not None:
            ceiling = min(ceiling, max_id + 1)
        rows = conn.execute("SELECT * FROM research WHERE id < ? ORDER BY id DESC LIMIT ?",
                            (ceiling, limit + 1)).fetchall()
        selected = rows[:limit]
        for row in selected:
            predecessor = conn.execute("SELECT record_hash FROM research WHERE id < ? ORDER BY id DESC LIMIT 1",
                                       (row["id"],)).fetchone()
            expected = predecessor[0] if predecessor else "0" * 64
            if row["previous_hash"] != expected or digest(expected, row["payload"]) != row["record_hash"]:
                raise ValueError("Research ledger integrity check failed")
        return {"records": [{"id": r["id"], **json.loads(r["payload"]),
                             "previous_hash": r["previous_hash"], "record_hash": r["record_hash"]}
                            for r in selected],
                "next_before": selected[-1]["id"] if len(rows) > limit else None,
                "integrity": "selected records hash-checked",
                "notice": "Local hash chain; database owner can rewrite it. Independent archived copies are required for external proof."}
    finally:
        conn.close()
