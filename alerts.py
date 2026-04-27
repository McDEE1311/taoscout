"""
TaoScout Alert Mode — v1.0
Watches all 129 subnets for significant changes.
No LLM needed. Pure deterministic Python.
"""
import sqlite3, json
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

import db as DB
from scout import load_data, get_chain_data, load_enrichment, MY_SUBNETS

# ── Alert thresholds ──────────────────────────────────────────────────────────
THRESHOLDS = {
    "emission_spike_pct":    50.0,   # emission up >50% = alert
    "emission_drop_pct":    -30.0,   # emission down >30% = alert
    "burn_spike_pct":        25.0,   # burn cost up >25% = alert
    "fill_spike":            10,     # neuron count up >10 in window = alert
    "fill_drop":             -5,     # neuron count down >5 = alert
    "zero_emission_new":     True,   # subnet went from >0 to 0 = alert
    "emission_restored":     True,   # subnet went from 0 to >0 = alert
    "high_ratio_threshold":  20.0,   # emit/burn ratio above this = opportunity alert
    "low_ratio_threshold":    2.0,   # emit/burn ratio below this = warning
}

PRIORITY_LABELS = {
    "CRITICAL": "🔴",
    "HIGH":     "🟠",
    "MEDIUM":   "🟡",
    "LOW":      "🟢",
    "INFO":     "🔵",
}

def build_alerts(hours=24, my_subnets=None):
    """
    Scan all 129 subnets for alert conditions.
    Returns structured alert list sorted by priority.
    """
    if my_subnets is None:
        my_subnets = MY_SUBNETS

    snap_count = DB.get_snapshot_count()
    if snap_count < 2:
        return {
            "error": "Insufficient snapshots for alert analysis",
            "snap_count": snap_count,
            "needed": 2
        }

    d, src = get_chain_data()
    if not d:
        return {"error": "No chain data available"}

    enrich   = load_enrichment()
    tao_usd  = enrich.get("tao_price_usd", 0.0)
    subnets  = d.get("subnets", [])
    block    = d.get("block", "?")
    movers   = DB.get_movers(hours=hours, top_n=200)

    # Build lookup from movers
    mover_map = {m["netuid"]: m for m in movers}

    alerts = []

    for s in subnets:
        nid    = s.get("netuid")
        name   = s.get("name", f"SN{nid}")
        em     = float(s.get("emission", 0) or 0)
        burn   = float(s.get("burn_tao", 0) or 0)
        neurons = int(s.get("neurons", 0) or 0)
        max_n  = int(s.get("max_neurons", 256) or 256)
        fill   = round(100 * neurons / max(max_n, 1), 1)
        ratio  = round(em / burn if burn > 0 else 0, 1)
        is_mine = nid in my_subnets
        mv     = mover_map.get(nid, {})
        chg    = float(mv.get("em_pct_change", 0) or 0)
        prev   = float(mv.get("previous_emission", 0) or 0)
        usd    = round(em * tao_usd, 3)

        # ── Zero emission restored ─────────────────────────────────────────
        # Only alert if subnet had 3+ consecutive zero snapshots (real downtime)
        # Normal epoch cycling hits zero briefly — we require sustained zeros
        if THRESHOLDS["emission_restored"] and prev == 0 and em > 0:
            history = DB.get_subnet_history(nid, hours=72)
            # Count consecutive zeros from most recent backwards
            consecutive_zeros = 0
            for h in (history or []):
                if float(h.get("emission", 0) or 0) == 0:
                    consecutive_zeros += 1
                else:
                    break
            if consecutive_zeros >= 3:
                alerts.append({
                    "priority":  "HIGH",
                    "type":      "EMISSION_RESTORED",
                    "netuid":    nid,
                    "name":      name,
                    "my_subnet": is_mine,
                    "message":   f"Emission restored after {consecutive_zeros} zero snapshots → {em:.5f}τ (${usd})",
                    "value":     em,
                    "change_pct": None,
                })
            continue

        # ── Emission spike ─────────────────────────────────────────────────
        if chg >= THRESHOLDS["emission_spike_pct"] and em > 0:
            priority = "HIGH" if chg >= 100 else "MEDIUM"
            alerts.append({
                "priority":  priority,
                "type":      "EMISSION_SPIKE",
                "netuid":    nid,
                "name":      name,
                "my_subnet": is_mine,
                "message":   f"Emission up {chg:.1f}% → {em:.5f}τ (${usd})",
                "value":     em,
                "change_pct": chg,
            })

        # ── Emission drop ──────────────────────────────────────────────────
        elif chg <= THRESHOLDS["emission_drop_pct"] and prev > 0:
            priority = "CRITICAL" if em == 0 else ("HIGH" if is_mine else "MEDIUM")
            alerts.append({
                "priority":  priority,
                "type":      "EMISSION_DROP" if em > 0 else "EMISSION_ZEROED",
                "netuid":    nid,
                "name":      name,
                "my_subnet": is_mine,
                "message":   f"Emission {'dropped to zero' if em == 0 else f'down {abs(chg):.1f}%'} → {em:.5f}τ",
                "value":     em,
                "change_pct": chg,
            })

        # ── High opportunity ratio ─────────────────────────────────────────
        if ratio >= THRESHOLDS["high_ratio_threshold"] and em > 0 and fill < 80:
            alerts.append({
                "priority":  "INFO",
                "type":      "HIGH_RATIO_OPEN",
                "netuid":    nid,
                "name":      name,
                "my_subnet": is_mine,
                "message":   f"Ratio {ratio}x with {fill}% fill — open slot opportunity",
                "value":     ratio,
                "change_pct": None,
            })

        # ── My subnet specific warnings ────────────────────────────────────
        if is_mine and em == 0:
            alerts.append({
                "priority":  "HIGH",
                "type":      "MY_SUBNET_ZERO",
                "netuid":    nid,
                "name":      name,
                "my_subnet": True,
                "message":   f"YOUR subnet SN{nid} has zero emission — verify miner status",
                "value":     0,
                "change_pct": None,
            })

        if is_mine and chg <= -20 and em > 0:
            alerts.append({
                "priority":  "MEDIUM",
                "type":      "MY_SUBNET_DROP",
                "netuid":    nid,
                "name":      name,
                "my_subnet": True,
                "message":   f"YOUR subnet SN{nid} emission down {abs(chg):.1f}% — review miner",
                "value":     em,
                "change_pct": chg,
            })

    # ── Sort: my subnets first, then by priority ───────────────────────────
    priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    alerts.sort(key=lambda a: (
        0 if a["my_subnet"] else 1,
        priority_order.get(a["priority"], 5)
    ))

    # ── Summary counts ─────────────────────────────────────────────────────
    summary = {
        "critical": sum(1 for a in alerts if a["priority"] == "CRITICAL"),
        "high":     sum(1 for a in alerts if a["priority"] == "HIGH"),
        "medium":   sum(1 for a in alerts if a["priority"] == "MEDIUM"),
        "low":      sum(1 for a in alerts if a["priority"] == "LOW"),
        "info":     sum(1 for a in alerts if a["priority"] == "INFO"),
        "total":    len(alerts),
        "my_subnet_alerts": sum(1 for a in alerts if a["my_subnet"]),
    }

    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "block":         block,
        "tao_usd":       tao_usd,
        "window_hours":  hours,
        "snap_count":    snap_count,
        "subnets_scanned": len(subnets),
        "summary":       summary,
        "alerts":        alerts,
        "disclaimer":    "TaoScout automated alerts. Informational only. Not financial advice.",
    }


def format_alerts_table(result):
    """Format alerts as readable text table."""
    if "error" in result:
        return f"ERROR: {result['error']}"

    s = result["summary"]
    lines = [
        "─" * 72,
        f"  TAOSCOUT ALERTS — {result['window_hours']}h window — Block {result['block']}",
        f"  TAO ${result['tao_usd']:.2f} | {result['snap_count']} snapshots | {result['subnets_scanned']} subnets scanned",
        "─" * 72,
        f"  🔴 CRITICAL: {s['critical']}  🟠 HIGH: {s['high']}  🟡 MEDIUM: {s['medium']}  🔵 INFO: {s['info']}  |  Total: {s['total']}",
        "─" * 72,
    ]

    if not result["alerts"]:
        lines.append("  ✓ No significant alerts in this window.")
    else:
        prev_mine = None
        for a in result["alerts"]:
            mine_flag = " ★" if a["my_subnet"] else ""
            icon = PRIORITY_LABELS.get(a["priority"], "⚪")
            lines.append(
                f"  {icon} [{a['priority']:8}] SN{a['netuid']:3} {a['name'][:16]:16}{mine_flag}"
            )
            lines.append(f"           {a['message']}")
            lines.append("")

    lines.append("─" * 72)
    lines.append("  ★ = your tracked subnet  |  Not financial advice")
    return "\n".join(lines)
