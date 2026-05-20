#!/usr/bin/env python3
"""
TaoScout Dereg Risk v1.0
Extended deregistration and haircut risk scoring.
Wraps and extends the existing risk_engine.py with additional signals.
All deterministic Python. No LLM.
"""

DEREG_WEIGHTS = {
    "emissions_collapse": 25,
    "validator_drop":     25,
    "consensus_weak":     20,
    "miner_churn":        15,
    "stale_dev":          10,
    "negative_sentiment":  5,
}

# Thresholds for dereg pressure
EMISSION_COLLAPSE_THRESHOLD = -30.0   # pct change in 24h
FILL_DROP_THRESHOLD          = -10.0  # pct drop in fill
LOW_EMISSION_ABS             = 0.0005 # tau/block — almost zero


def score_dereg_risk(subnet: dict, history: list, movers: list) -> dict:
    """
    Score deregistration/haircut risk for one subnet.
    Returns risk_score 0-100 (higher = more danger) with reasons.
    """
    nid     = subnet.get("netuid", 0)
    em      = float(subnet.get("emission", 0) or 0)
    burn    = float(subnet.get("burn_tao", 0) or 0)
    neurons = int(subnet.get("neurons", 0) or 0)
    max_n   = int(subnet.get("max_neurons", 256) or 256)
    fill    = (neurons / max_n * 100) if max_n > 0 else 0
    mover   = next((m for m in movers if m.get("netuid") == nid), None)

    scores  = {}
    reasons = []
    risk_flags = []

    # 1. Emission collapse (25 pts)
    if em == 0:
        scores["emissions_collapse"] = 25
        risk_flags.append("zero emission — dereg imminent risk")
    elif mover:
        pct = float(mover.get("em_pct_change", 0))
        if pct <= -50:
            scores["emissions_collapse"] = 25; risk_flags.append(f"emission -50%+ collapse")
        elif pct <= -30:
            scores["emissions_collapse"] = 18; risk_flags.append(f"emission {pct:.1f}% sharp drop")
        elif pct <= -15:
            scores["emissions_collapse"] = 10; reasons.append(f"emission declining {pct:.1f}%")
        else:
            scores["emissions_collapse"] = 0
    else:
        if em < LOW_EMISSION_ABS:
            scores["emissions_collapse"] = 15; risk_flags.append(f"emission near zero {em:.6f}t")
        else:
            scores["emissions_collapse"] = 0

    # 2. Validator/fill drop (25 pts)
    if fill < 20:
        scores["validator_drop"] = 25; risk_flags.append(f"fill={fill:.0f}% — near empty")
    elif fill < 40:
        scores["validator_drop"] = 15; risk_flags.append(f"fill={fill:.0f}% — low demand")
    elif fill < 60:
        scores["validator_drop"] = 8;  reasons.append(f"fill={fill:.0f}% — below average")
    else:
        scores["validator_drop"] = 0

    # Check for fill drop in history
    if len(history) >= 2:
        prev_fill = float(history[-1].get("fill_pct", fill))
        fill_delta = fill - prev_fill
        if fill_delta <= -15:
            scores["validator_drop"] = min(25, scores.get("validator_drop", 0) + 10)
            risk_flags.append(f"fill dropped {fill_delta:.1f}% recently")

    # 3. Consensus/incentive weakness (20 pts)
    if burn > 0 and em > 0:
        ratio = em / burn
        if ratio < 0.5:
            scores["consensus_weak"] = 20; risk_flags.append(f"burn exceeds emission {ratio:.2f}x")
        elif ratio < 1:
            scores["consensus_weak"] = 12; risk_flags.append(f"near-zero incentive ratio {ratio:.2f}x")
        elif ratio < 3:
            scores["consensus_weak"] = 5
        else:
            scores["consensus_weak"] = 0
    elif em == 0 and burn > 0:
        scores["consensus_weak"] = 20; risk_flags.append(f"burn={burn:.4f}t zero incentive")
    else:
        scores["consensus_weak"] = 0

    # 4. Miner churn (15 pts)
    if len(history) >= 3:
        counts = [h.get("neurons", neurons) for h in history[:5]]
        if counts:
            churn_pct = ((max(counts) - min(counts)) / max(max(counts), 1)) * 100
            if churn_pct >= 30:
                scores["miner_churn"] = 15; risk_flags.append(f"high miner churn {churn_pct:.0f}%")
            elif churn_pct >= 15:
                scores["miner_churn"] = 8;  reasons.append(f"moderate churn {churn_pct:.0f}%")
            elif churn_pct >= 5:
                scores["miner_churn"] = 3
            else:
                scores["miner_churn"] = 0
    else:
        scores["miner_churn"] = 3  # neutral

    # 5. Stale dev (10 pts) — placeholder until GitHub wired
    scores["stale_dev"] = 5  # neutral

    # 6. Negative sentiment (5 pts) — placeholder
    scores["negative_sentiment"] = 0

    total = min(100, sum(scores.values()))

    if total >= 60:   label = "CRITICAL"
    elif total >= 40: label = "HIGH"
    elif total >= 20: label = "MEDIUM"
    elif total > 0:   label = "LOW"
    else:             label = "CLEAN"

    return {
        "netuid":          nid,
        "name":            subnet.get("name", "Unknown"),
        "dereg_risk":      total,
        "dereg_label":     label,
        "breakdown":       scores,
        "risk_flags":      risk_flags[:4],
        "reasons":         reasons[:3],
        "emission":        em,
        "fill_pct":        round(fill, 1),
        "em_pct_change":   float(mover.get("em_pct_change", 0)) if mover else None,
    }


def score_all_dereg(subnets: list, movers: list, get_history_fn=None,
                    my_netuids: list = None, top_n: int = 20) -> list:
    results = []
    for s in subnets:
        nid = s.get("netuid", 0)
        history = get_history_fn(nid) if get_history_fn else []
        r = score_dereg_risk(s, history, movers)
        r["is_mine"] = nid in (my_netuids or [])
        if r["dereg_risk"] > 0:
            results.append(r)

    results.sort(key=lambda x: (not x.get("is_mine", False), -x["dereg_risk"]))
    return results[:top_n]
