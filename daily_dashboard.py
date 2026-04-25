"""
TaoScout Daily Operator Dashboard
Deterministic. No LLM. Fast.
"""
from datetime import datetime, timezone
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import db as DB
import analytics as AN
from scout import (
    get_chain_data, load_enrichment, build_grounding,
    load_data, MY_SUBNETS, META
)

def get_meta(netuid):
    return META.get(str(netuid), {"name": "Unknown", "type": "UNVERIFIED"})

def build_dashboard():
    d, src = get_chain_data()
    if not d:
        return {"error": "No chain data available"}

    enrich    = load_enrichment()
    tao_usd   = enrich.get("tao_price_usd", 0.0)
    subnets   = d.get("subnets", [])
    block     = d.get("block", "?")
    snap_count = DB.get_snapshot_count()
    movers    = DB.get_movers(hours=24, top_n=20) if snap_count >= 2 else []

    # ── Top opportunities ──────────────────────────────────────────────────────
    top_opps = AN.top_opportunities(subnets, tao_usd, movers, top_n=5)
    opportunities = []
    for o in top_opps:
        m = o["metrics"]
        opportunities.append({
            "netuid":       o["netuid"],
            "name":         o["name"],
            "score":        o["score"],
            "emission_tao": round(m["emission"], 5),
            "emission_usd": round(m.get("emission_usd", 0), 2),
            "burn_tao":     round(m["burn_tao"], 4),
            "ratio":        round(m["ratio"], 1),
            "fill_pct":     round(m["fill_pct"], 1),
        })

    # ── Top movers ─────────────────────────────────────────────────────────────
    top_movers = []
    for mv in movers[:8]:
        chg = mv.get("em_pct_change", 0) or 0
        top_movers.append({
            "netuid":     mv["netuid"],
            "name":       mv.get("name", ""),
            "direction":  "up" if chg > 0 else "down",
            "pct_change": round(chg, 1),
            "current":    round(mv.get("current_emission", 0), 5),
        })

    # ── New subnet registrations ───────────────────────────────────────────────
    new_regs = []
    if snap_count >= 2:
        for mv in movers:
            nid = mv.get("netuid")
            s = next((x for x in subnets if x.get("netuid") == nid), None)
            if not s:
                continue
            neurons = int(s.get("neurons", 0) or 0)
            max_n   = int(s.get("max_neurons", 256) or 256)
            fill    = round(100 * neurons / max(max_n, 1), 1)
            if fill < 50 and float(s.get("emission", 0) or 0) > 0:
                new_regs.append({
                    "netuid": nid,
                    "name":   s.get("name", ""),
                    "delta":  mv.get("em_pct_change", 0),
                    "fill":   fill,
                })

    # ── Hardware fit recommendations ───────────────────────────────────────────
    from analytics import GPU_CLASSES
    hw_recs = {}
    for cls, info in GPU_CLASSES.items():
        ranked = AN.rank_subnets_for_gpu_class(
            subnets, cls, tao_usd, movers, top_n=3
        )
        hw_recs[cls] = {
            "label": info["label"],
            "cards": info["cards"],
            "top3":  [{"netuid": r["netuid"], "name": r["name"],
                       "score": r["combined_score"]} for r in ranked]
        }

    # ── My subnets status ──────────────────────────────────────────────────────
    my_status = []
    for nid in MY_SUBNETS:
        s = next((x for x in subnets if x.get("netuid") == nid), None)
        if not s:
            continue
        em  = float(s.get("emission", 0) or 0)
        b   = float(s.get("burn_tao", 0) or 0)
        mv  = next((m for m in movers if m.get("netuid") == nid), None)
        chg = mv.get("em_pct_change", 0) if mv else 0
        my_status.append({
            "netuid":       nid,
            "name":         s.get("name", ""),
            "emission_tao": round(em, 5),
            "emission_usd": round(em * tao_usd, 3),
            "burn_tao":     round(b, 4),
            "ratio":        round(em / b if b > 0 else 0, 1),
            "fill_pct":     round(100 * int(s.get("neurons", 0)) /
                            max(int(s.get("max_neurons", 256)), 1), 1),
            "trend_24h":    round(chg, 1),
            "trend_dir":    "up" if chg > 0 else ("flat" if chg == 0 else "down"),
        })

    # ── Action today ──────────────────────────────────────────────────────────
    actions = []

    # Check my subnets for issues
    for ms in my_status:
        if ms["emission_tao"] == 0:
            actions.append({
                "priority": "HIGH",
                "subnet":   f"SN{ms['netuid']} {ms['name']}",
                "action":   "Zero emission — check if subnet is active or deregistered"
            })
        elif ms["trend_24h"] < -20:
            actions.append({
                "priority": "MEDIUM",
                "subnet":   f"SN{ms['netuid']} {ms['name']}",
                "action":   f"Emission dropped {abs(ms['trend_24h'])}% in 24h — review validator/miner status"
            })
        if ms["fill_pct"] >= 99 and ms["ratio"] > 5:
            actions.append({
                "priority": "LOW",
                "subnet":   f"SN{ms['netuid']} {ms['name']}",
                "action":   "Full and high ratio — competitive slot, maintain uptime"
            })

    # Top opportunity not currently mined
    my_netuids = set(MY_SUBNETS)
    for opp in opportunities:
        if opp["netuid"] not in my_netuids and opp["score"] > 50:
            actions.append({
                "priority": "INFO",
                "subnet":   f"SN{opp['netuid']} {opp['name']}",
                "action":   f"Score {opp['score']}/100 — worth evaluating for your hardware"
            })
            break

    grounding = build_grounding(d, enrich)

    return {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "block":           block,
        "tao_usd":         tao_usd,
        "snap_count":      snap_count,
        "top_opportunities": opportunities,
        "top_movers":      top_movers,
        "new_registrations": new_regs,
        "hardware_recs":   hw_recs,
        "my_subnets":      my_status,
        "actions_today":   actions,
        "grounding":       grounding,
        "disclaimer":      "TaoScout automated. Informational only. Not financial advice.",
    }
