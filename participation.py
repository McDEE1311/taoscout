#!/usr/bin/env python3
"""
TaoScout Participation Trends v1.0
Neuron count changes from SQLite snapshots.
No LLM inference. All deterministic from stored data.

Note: neurons = total registered participants (miners + validators combined).
This is a participation metric, not a pure validator metric.
Full validator metagraph requires per-subnet chain calls (future feature).
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent / "data" / "taoscout.db"

def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def get_participation_delta(hours=24, top_n=15):
    """
    Get subnets with biggest neuron count changes over N hours.
    Returns ranked list sorted by absolute change.
    """
    conn = get_conn()
    c    = conn.cursor()

    c.execute("SELECT id FROM snapshots ORDER BY fetched_at DESC LIMIT 1")
    latest = c.fetchone()
    if not latest:
        conn.close()
        return []

    c.execute("""
        SELECT id FROM snapshots
        WHERE replace(substr(fetched_at,1,19),'T',' ') <= datetime('now', ? || ' hours')
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
            curr.neurons        AS curr_neurons,
            curr.max_neurons    AS max_neurons,
            curr.fill_pct       AS curr_fill,
            curr.emission       AS curr_emission,
            prev.neurons        AS prev_neurons,
            prev.fill_pct       AS prev_fill,
            (curr.neurons - prev.neurons)   AS neuron_delta,
            (curr.fill_pct - prev.fill_pct) AS fill_delta
        FROM subnet_snapshots curr
        JOIN subnet_snapshots prev ON curr.netuid = prev.netuid
        WHERE curr.snapshot_id  = ?
          AND prev.snapshot_id  = ?
          AND (curr.neurons != prev.neurons OR curr.fill_pct != prev.fill_pct)
        ORDER BY ABS(curr.neurons - prev.neurons) DESC
        LIMIT ?
    """, (latest["id"], old["id"], top_n))

    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    for r in rows:
        r["direction"] = "UP" if r["neuron_delta"] > 0 else "DOWN"
        r["pct_change"] = round(
            (r["neuron_delta"] / r["prev_neurons"] * 100)
            if r["prev_neurons"] > 0 else 0, 1
        )
    return rows

def get_concentration_risk(min_fill=90):
    """
    Subnets near or at capacity — harder to register, concentration risk.
    """
    conn = get_conn()
    c    = conn.cursor()
    c.execute("SELECT id FROM snapshots ORDER BY fetched_at DESC LIMIT 1")
    latest = c.fetchone()
    if not latest:
        conn.close()
        return []
    c.execute("""
        SELECT netuid, name, neurons, max_neurons, fill_pct, emission, emission_usd
        FROM subnet_snapshots
        WHERE snapshot_id = ?
          AND fill_pct >= ?
          AND emission > 0
        ORDER BY fill_pct DESC, emission DESC
        LIMIT 15
    """, (latest["id"], min_fill))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_new_participants(hours=24):
    """
    Subnets where neuron count increased — new registrations happening.
    """
    deltas = get_participation_delta(hours=hours, top_n=50)
    return [r for r in deltas if r["neuron_delta"] > 0][:10]

def get_dropped_participants(hours=24):
    """
    Subnets where neuron count decreased — deregistrations happening.
    """
    deltas = get_participation_delta(hours=hours, top_n=50)
    return [r for r in deltas if r["neuron_delta"] < 0][:10]

def get_participation_snapshot_count():
    conn = get_conn()
    c    = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM snapshots")
    n = c.fetchone()["n"]
    conn.close()
    return n

def build_participation_payload(hours=24):
    """Full participation trends payload."""
    snap_count = get_participation_snapshot_count()

    if snap_count < 4:
        return {
            "available":    False,
            "snap_count":   snap_count,
            "needed":       4,
            "confidence":   "INSUFFICIENT",
            "reason":       f"Need 4+ snapshots for participation trends. Have {snap_count}.",
        }

    deltas     = get_participation_delta(hours=hours, top_n=15)
    new_parts  = [r for r in deltas if r["neuron_delta"] > 0]
    dropped    = [r for r in deltas if r["neuron_delta"] < 0]
    crowded    = get_concentration_risk(min_fill=95)

    return {
        "available":          True,
        "snap_count":         snap_count,
        "hours":              hours,
        "confidence":         "MEDIUM",
        "confidence_reason":  "Computed from neuron count delta in SQLite snapshots. neurons = total registered participants.",
        "data_note":          "neurons field = miners + validators combined. Full validator-only metagraph requires per-subnet chain calls (not yet implemented).",
        "fields_used":        ["neurons", "max_neurons", "fill_pct", "emission"],
        "computation":        "delta = curr_neurons - prev_neurons from SQLite snapshot comparison",
        "new_participants":   new_parts,
        "dropped_participants": dropped,
        "concentration_risk": crowded,
        "total_changes":      len(deltas),
    }

def format_participation_table(payload):
    """Format participation trends as terminal table."""
    if not payload.get("available"):
        return (
            "─" * 72 + "\n"
            f"  PARTICIPATION TRENDS — INSUFFICIENT DATA\n"
            f"  {payload.get('reason','')}\n"
            + "─" * 72
        )

    lines = ["─" * 72]
    lines.append(f"  PARTICIPATION TRENDS ({payload['hours']}H) — {payload['snap_count']} SNAPSHOTS")
    lines.append(f"  Note: {payload['data_note'][:70]}")
    lines.append("─" * 72)

    new_p = payload.get("new_participants", [])
    if new_p:
        lines.append("")
        lines.append("  NEW REGISTRATIONS (neuron count increased):")
        lines.append(f"  {'Subnet':<8}  {'Name':<18}  {'Delta':>7}  {'Curr':>6}  {'Fill%':>5}  {'Emit(t)':>10}")
        lines.append(f"  {'──────':<8}  {'──────────────────':<18}  {'───────':>7}  {'──────':>6}  {'─────':>5}  {'──────────':>10}")
        for r in new_p[:8]:
            lines.append(
                f"  SN{r['netuid']:<6}  {r['name'][:18]:<18}  "
                f"{r['neuron_delta']:>+6}  {r['curr_neurons']:>6}  "
                f"{r['curr_fill']:>4.0f}%  {r['curr_emission']:>10.5f}"
            )

    dropped = payload.get("dropped_participants", [])
    if dropped:
        lines.append("")
        lines.append("  DEREGISTRATIONS (neuron count decreased):")
        lines.append(f"  {'Subnet':<8}  {'Name':<18}  {'Delta':>7}  {'Curr':>6}  {'Fill%':>5}  {'Emit(t)':>10}")
        lines.append(f"  {'──────':<8}  {'──────────────────':<18}  {'───────':>7}  {'──────':>6}  {'─────':>5}  {'──────────':>10}")
        for r in dropped[:8]:
            lines.append(
                f"  SN{r['netuid']:<6}  {r['name'][:18]:<18}  "
                f"{r['neuron_delta']:>+6}  {r['curr_neurons']:>6}  "
                f"{r['curr_fill']:>4.0f}%  {r['curr_emission']:>10.5f}"
            )

    crowded = payload.get("concentration_risk", [])
    if crowded:
        lines.append("")
        lines.append("  CONCENTRATION RISK (>= 95% fill, active emission):")
        lines.append(f"  {'Subnet':<8}  {'Name':<18}  {'Fill%':>5}  {'Slots':>10}  {'Emit(t)':>10}")
        lines.append(f"  {'──────':<8}  {'──────────────────':<18}  {'─────':>5}  {'──────────':>10}  {'──────────':>10}")
        for r in crowded[:8]:
            slots = f"{r['neurons']}/{r['max_neurons']}"
            lines.append(
                f"  SN{r['netuid']:<6}  {r['name'][:18]:<18}  "
                f"{r['fill_pct']:>4.0f}%  {slots:>10}  {r['emission']:>10.5f}"
            )

    lines.append("")
    lines.append(f"  Confidence: {payload['confidence']}  |  {payload['confidence_reason'][:60]}")
    lines.append("─" * 72)
    return "\n".join(lines)
