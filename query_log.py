"""
TaoScout Query Logger
Tracks all API calls for analytics and improvement.
"""
import sqlite3, time
from datetime import datetime, timezone
from pathlib import Path

LOG_DB = Path("/home/mcdeerig/taoscout/data/query_log.db")

def init_log_db():
    con = sqlite3.connect(LOG_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS query_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            endpoint    TEXT,
            question    TEXT,
            intent      TEXT,
            model_used  TEXT,
            gpu_lane    TEXT,
            latency_ms  INTEGER,
            success     INTEGER,
            fallback    INTEGER,
            error       TEXT
        )
    """)
    con.commit()
    con.close()

def log_query(endpoint, question="", intent="", model="",
              gpu_lane="", latency_ms=0, success=True,
              fallback=False, error=""):
    try:
        con = sqlite3.connect(LOG_DB)
        con.execute("""
            INSERT INTO query_log
            (ts, endpoint, question, intent, model_used, gpu_lane,
             latency_ms, success, fallback, error)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            endpoint, question[:200], intent, model,
            gpu_lane, latency_ms,
            1 if success else 0,
            1 if fallback else 0,
            error[:200]
        ))
        con.commit()
        con.close()
    except Exception:
        pass

def get_stats(hours=24):
    try:
        con = sqlite3.connect(LOG_DB)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as succeeded,
                SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN fallback=1 THEN 1 ELSE 0 END) as fallbacks,
                AVG(latency_ms) as avg_latency_ms,
                MAX(latency_ms) as max_latency_ms
            FROM query_log
            WHERE ts >= datetime('now', ?)
        """, (f"-{hours} hours",))
        summary = dict(cur.fetchone())

        cur.execute("""
            SELECT endpoint, COUNT(*) as cnt
            FROM query_log
            WHERE ts >= datetime('now', ?)
            GROUP BY endpoint ORDER BY cnt DESC
        """, (f"-{hours} hours",))
        by_endpoint = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT question, COUNT(*) as cnt
            FROM query_log
            WHERE endpoint='ask' AND ts >= datetime('now', ?)
            GROUP BY question ORDER BY cnt DESC LIMIT 10
        """, (f"-{hours} hours",))
        top_questions = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT gpu_lane, COUNT(*) as cnt,
                   AVG(latency_ms) as avg_ms
            FROM query_log
            WHERE ts >= datetime('now', ?)
            GROUP BY gpu_lane ORDER BY cnt DESC
        """, (f"-{hours} hours",))
        by_lane = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT question, error, ts
            FROM query_log
            WHERE success=0 AND ts >= datetime('now', ?)
            ORDER BY ts DESC LIMIT 10
        """, (f"-{hours} hours",))
        recent_errors = [dict(r) for r in cur.fetchall()]

        con.close()
        return {
            "window_hours": hours,
            "summary": summary,
            "by_endpoint": by_endpoint,
            "top_questions": top_questions,
            "by_gpu_lane": by_lane,
            "recent_errors": recent_errors,
        }
    except Exception as e:
        return {"error": str(e)}

init_log_db()
