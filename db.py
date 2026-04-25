#!/usr/bin/env python3
"""
TaoScout - Database Layer
SQLite snapshot storage with historical query support.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH    = SCRIPT_DIR / "data" / "taoscout.db"

def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at    TEXT NOT NULL,
            block         INTEGER UNIQUE,
            subnet_count  INTEGER,
            tao_price_usd REAL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS subnet_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id   INTEGER NOT NULL,
            fetched_at    TEXT NOT NULL,
            block         INTEGER,
            netuid        INTEGER NOT NULL,
            name          TEXT DEFAULT '',
            emission      REAL DEFAULT 0,
            burn_tao      REAL DEFAULT 0,
            neurons       INTEGER DEFAULT 0,
            max_neurons   INTEGER DEFAULT 256,
            tempo         INTEGER DEFAULT 360,
            price         REAL DEFAULT 0,
            fill_pct      REAL DEFAULT 0,
            emission_usd  REAL DEFAULT 0,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_sn_netuid   ON subnet_snapshots(netuid)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sn_fetched  ON subnet_snapshots(fetched_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap_block  ON snapshots(block)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap_time   ON snapshots(fetched_at)")
    conn.commit()
    conn.close()

def insert_snapshot(chain_data, tao_price_usd=0.0):
    conn = get_conn()
    c    = conn.cursor()
    block   = int(chain_data.get("block", 0) or 0)
    subnets = chain_data.get("subnets", [])
    c.execute("SELECT id FROM snapshots WHERE block = ?", (block,))
    if c.fetchone():
        conn.close()
        return None
    fetched_at = datetime.now(timezone.utc).isoformat()
    c.execute("""
        INSERT INTO snapshots (fetched_at, block, subnet_count, tao_price_usd)
        VALUES (?, ?, ?, ?)
    """, (fetched_at, block, len(subnets), tao_price_usd))
    snap_id = c.lastrowid
    rows = []
    for s in subnets:
        neurons     = int(s.get("neurons", 0) or 0)
        max_neurons = int(s.get("max_neurons", 256) or 256)
        emission    = float(s.get("emission", 0) or 0)
        fill_pct    = round((neurons / max_neurons * 100) if max_neurons > 0 else 0, 2)
        rows.append((
            snap_id, fetched_at, block,
            int(s.get("netuid", 0)),
            str(s.get("name", "") or ""),
            emission,
            float(s.get("burn_tao", 0) or 0),
            neurons, max_neurons,
            int(s.get("tempo", 360) or 360),
            float(s.get("price", 0) or 0),
            fill_pct,
            round(emission * tao_price_usd, 4) if tao_price_usd > 0 else 0
        ))
    c.executemany("""
        INSERT INTO subnet_snapshots
        (snapshot_id, fetched_at, block, netuid, name,
         emission, burn_tao, neurons, max_neurons,
         tempo, price, fill_pct, emission_usd)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    conn.close()
    return snap_id

def import_json_snapshots(data_dir, tao_price_usd=0.0):
    data_dir = Path(data_dir)
    imported = 0
    for p in sorted(data_dir.glob("snapshot_*.json")):
        try:
            with open(p) as f:
                d = json.load(f)
            chain = d.get("data", d)
            if chain and "subnets" in chain:
                if insert_snapshot(chain, tao_price_usd):
                    imported += 1
        except Exception:
            pass
    return imported

def get_subnet_history(netuid, hours=24):
    conn = get_conn()
    c    = conn.cursor()
    c.execute("""
        SELECT ss.fetched_at, ss.block, ss.emission, ss.burn_tao,
               ss.neurons, ss.max_neurons, ss.fill_pct, ss.price,
               ss.emission_usd, s.tao_price_usd
        FROM subnet_snapshots ss
        JOIN snapshots s ON ss.snapshot_id = s.id
        WHERE ss.netuid = ?
          AND datetime(ss.fetched_at) >= datetime('now', ? || ' hours')
        ORDER BY ss.fetched_at DESC
    """, (netuid, f"-{hours}"))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_movers(hours=24, top_n=10):
    conn = get_conn()
    c    = conn.cursor()
    c.execute("SELECT id FROM snapshots ORDER BY fetched_at DESC LIMIT 1")
    latest = c.fetchone()
    if not latest:
        conn.close()
        return []
    c.execute("""
        SELECT id FROM snapshots
        WHERE replace(substr(fetched_at, 1, 19), 'T', ' ') <= datetime('now', ? || ' hours')
        ORDER BY fetched_at DESC LIMIT 1
    """, (f"-{hours}",))
    old = c.fetchone()
    if not old:
        conn.close()
        return []
    c.execute("""
        SELECT
            curr.netuid,
            curr.name,
            curr.emission        AS curr_emission,
            curr.fill_pct        AS curr_fill,
            curr.burn_tao        AS curr_burn,
            curr.emission_usd    AS curr_emission_usd,
            prev.emission        AS prev_emission,
            prev.fill_pct        AS prev_fill,
            (curr.emission - prev.emission)              AS em_delta,
            (curr.fill_pct - prev.fill_pct)              AS fill_delta,
            CASE WHEN prev.emission > 0
                 THEN ((curr.emission - prev.emission) / prev.emission * 100)
                 ELSE 0 END                              AS em_pct_change
        FROM subnet_snapshots curr
        JOIN subnet_snapshots prev ON curr.netuid = prev.netuid
        WHERE curr.snapshot_id  = ?
          AND prev.snapshot_id  = ?
        ORDER BY ABS(curr.emission - prev.emission) DESC
        LIMIT ?
    """, (latest["id"], old["id"], top_n))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_snapshot_count():
    conn = get_conn()
    c    = conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM snapshots")
    n = c.fetchone()["n"]
    conn.close()
    return n

def get_latest_block():
    conn = get_conn()
    c    = conn.cursor()
    c.execute("SELECT block, fetched_at FROM snapshots ORDER BY fetched_at DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return dict(row) if row else {}

def db_status():
    conn = get_conn()
    c    = conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM snapshots")
    snap_count = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) AS n FROM subnet_snapshots")
    row_count = c.fetchone()["n"]
    c.execute("SELECT fetched_at FROM snapshots ORDER BY fetched_at DESC LIMIT 1")
    latest = c.fetchone()
    c.execute("SELECT fetched_at FROM snapshots ORDER BY fetched_at ASC LIMIT 1")
    oldest = c.fetchone()
    conn.close()
    return {
        "snapshot_count":     snap_count,
        "subnet_rows":        row_count,
        "latest_snapshot":    latest["fetched_at"][:19] if latest else None,
        "oldest_snapshot":    oldest["fetched_at"][:19] if oldest else None,
        "db_path":            str(DB_PATH),
    }
