#!/usr/bin/env python3
"""
TaoScout Flow Tracker v1.0
Subnet health scoring and capital flow detection.
All math deterministic Python. No LLM inference.
Builds on existing db.py — does not duplicate data fetches.
"""

from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

HEALTH_WEIGHTS = {
    "emissions_trend":       25,
    "validator_support":     20,
    "miner_stability":       15,
    "incentive_trend":       15,
    "registration_pressure": 10,
    "dev_activity":          10,
    "volatility_penalty":     5,
}


def compute_health_score(subnet: dict, history: list, movers: list) -> dict:
    nid     = subnet.get("netuid", 0)
    em      = float(subnet.get("emission", 0) or 0)
    burn    = float(subnet.get("burn_tao", 0) or 0)
    neurons = int(subnet.get("neurons", 0) or 0)
    max_n   = int(subnet.get("max_neurons", 256) or 256)
    fill    = (neurons / max_n * 100) if max_n > 0 else 0

    scores  = {}
    reasons = []
    mover   = next((m for m in movers if m.get("netuid") == nid), None)

    # 1. Emissions trend (25 pts)
    if em == 0:
        scores["emissions_trend"] = 0
        reasons.append("zero emission")
    elif mover:
        pct = float(mover.get("em_pct_change", 0))
        if pct >= 20:
            scores["emissions_trend"] = 25; reasons.append(f"emission +{pct:.1f}%")
        elif pct >= 5:
            scores["emissions_trend"] = 20; reasons.append(f"emission growing +{pct:.1f}%")
        elif pct >= -5:
            scores["emissions_trend"] = 15; reasons.append("emission stable")
        elif pct >= -20:
            scores["emissions_trend"] = 8;  reasons.append(f"emission declining {pct:.1f}%")
        else:
            scores["emissions_trend"] = 0;  reasons.append(f"emission collapsing {pct:.1f}%")
    else:
        if em > 0.01:    scores["emissions_trend"] = 20
        elif em > 0.005: scores["emissions_trend"] = 15
        elif em > 0.001: scores["emissions_trend"] = 10
        else:            scores["emissions_trend"] = 3
        reasons.append(f"emission={em:.5f}t")

    # 2. Fill/demand (20 pts)
    if fill >= 95:
        scores["validator_support"] = 20; reasons.append(f"fill={fill:.0f}% full demand")
    elif fill >= 75:
        scores["validator_support"] = 16; reasons.append(f"fill={fill:.0f}% healthy")
    elif fill >= 50:
        scores["validator_support"] = 10; reasons.append(f"fill={fill:.0f}% moderate")
    elif fill >= 25:
        scores["validator_support"] = 5;  reasons.append(f"fill={fill:.0f}% weak")
    else:
        scores["validator_support"] = 2;  reasons.append(f"fill={fill:.0f}% nearly empty")

    # 3. Miner stability (15 pts)
    if len(history) >= 3:
        counts = [h.get("neurons", 0) for h in history[:5]]
        churn_pct = ((max(counts) - min(counts)) / max(max(counts), 1)) * 100
        if churn_pct <= 2:    scores["miner_stability"] = 15
        elif churn_pct <= 10: scores["miner_stability"] = 10; reasons.append(f"churn {churn_pct:.0f}%")
        elif churn_pct <= 25: scores["miner_stability"] = 5;  reasons.append(f"moderate churn {churn_pct:.0f}%")
        else:                 scores["miner_stability"] = 0;  reasons.append(f"high churn {churn_pct:.0f}%")
    else:
        scores["miner_stability"] = 8

    # 4. Incentive trend / ratio (15 pts)
    if burn > 0 and em > 0:
        ratio = em / burn
        if ratio >= 20:   scores["incentive_trend"] = 15; reasons.append(f"ratio={ratio:.0f}x excellent")
        elif ratio >= 10: scores["incentive_trend"] = 12; reasons.append(f"ratio={ratio:.0f}x good")
        elif ratio >= 3:  scores["incentive_trend"] = 8
        elif ratio >= 1:  scores["incentive_trend"] = 4
        else:             scores["incentive_trend"] = 0;  reasons.append(f"burn exceeds emission")
    elif em > 0:
        scores["incentive_trend"] = 12
    else:
        scores["incentive_trend"] = 0

    # 5. Registration pressure (10 pts)
    if burn <= 0.001:   scores["registration_pressure"] = 10
    elif burn <= 0.01:  scores["registration_pressure"] = 8
    elif burn <= 0.1:   scores["registration_pressure"] = 5
    elif burn <= 1.0:   scores["registration_pressure"] = 2; reasons.append(f"burn={burn:.2f}t high")
    else:               scores["registration_pressure"] = 0; reasons.append(f"burn={burn:.2f}t prohibitive")

    # 6. Dev activity (10 pts) — neutral until GitHub wired
    scores["dev_activity"] = 5

    # 7. Volatility penalty (5 pts)
    if mover:
        abs_chg = abs(float(mover.get("em_pct_change", 0)))
        if abs_chg <= 5:    scores["volatility_penalty"] = 5
        elif abs_chg <= 20: scores["volatility_penalty"] = 3
        elif abs_chg <= 50: scores["volatility_penalty"] = 1
        else:               scores["volatility_penalty"] = 0; reasons.append(f"volatile {abs_chg:.0f}%")
    else:
        scores["volatility_penalty"] = 3

    total = min(100, sum(scores.values()))
    if total >= 75:   label = "STRONG"
    elif total >= 55: label = "HEALTHY"
    elif total >= 35: label = "WEAK"
    elif total >= 15: label = "POOR"
    else:             label = "CRITICAL"

    return {
        "netuid":       nid,
        "name":         subnet.get("name", "Unknown"),
        "health_score": total,
        "health_label": label,
        "breakdown":    scores,
        "reasons":      reasons[:5],
        "emission":     em,
        "burn":         burn,
        "fill_pct":     round(fill, 1),
        "em_pct_change": float(mover.get("em_pct_change", 0)) if mover else None,
    }


def compute_all_health(subnets: list, movers: list, get_history_fn=None) -> list:
    results = []
    for s in subnets:
        history = get_history_fn(s.get("netuid", 0)) if get_history_fn else []
        results.append(compute_health_score(s, history, movers))
    results.sort(key=lambda x: -x["health_score"])
    return results


def compute_flow_overview(health_scores: list, movers: list, tao_price: float) -> dict:
    flowing_in  = [h for h in health_scores if h["health_score"] >= 60]
    flowing_out = [h for h in health_scores if h["health_score"] < 30]
    neutral     = [h for h in health_scores if 30 <= h["health_score"] < 60]
    at_risk_usd = sum(h["emission"] * tao_price for h in flowing_out if h["emission"] > 0)

    up_movers   = sorted([m for m in movers if m.get("em_pct_change", 0) > 0], key=lambda x: -x["em_pct_change"])
    down_movers = sorted([m for m in movers if m.get("em_pct_change", 0) < 0], key=lambda x: x["em_pct_change"])

    return {
        "flowing_in_count":    len(flowing_in),
        "flowing_out_count":   len(flowing_out),
        "neutral_count":       len(neutral),
        "capital_at_risk_usd": round(at_risk_usd, 2),
        "tao_price_usd":       tao_price,
        "top_inflow":  [{"netuid": h["netuid"], "name": h["name"], "health": h["health_score"]} for h in flowing_in[:5]],
        "top_outflow": [{"netuid": h["netuid"], "name": h["name"], "health": h["health_score"]} for h in flowing_out[:5]],
        "top_gainers": [{"netuid": m["netuid"], "name": m["name"], "em_pct_change": round(m["em_pct_change"], 1)} for m in up_movers[:5]],
        "top_losers":  [{"netuid": m["netuid"], "name": m["name"], "em_pct_change": round(m["em_pct_change"], 1)} for m in down_movers[:5]],
    }
