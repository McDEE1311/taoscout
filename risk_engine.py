#!/usr/bin/env python3
"""
TaoScout Risk Engine v1.0
Weighted risk scoring across 6 dimensions.
All math in Python. No LLM inference.
"""

# ── Risk weights ───────────────────────────────────────────────────────────────
RISK_WEIGHTS = {
    "zero_emission":   {"weight": 30, "label": "ZERO EMISSION",   "severity": "CRITICAL"},
    "negative_mover":  {"weight": 20, "label": "EMISSION FALLING", "severity": "HIGH"},
    "high_burn":       {"weight": 15, "label": "HIGH BURN COST",   "severity": "HIGH"},
    "overcrowded":     {"weight": 15, "label": "AT CAPACITY",      "severity": "MEDIUM"},
    "volatility":      {"weight": 10, "label": "HIGH VOLATILITY",  "severity": "MEDIUM"},
    "low_reward":      {"weight": 10, "label": "LOW REWARD",       "severity": "LOW"},
}

def score_subnet_risk(subnet, movers=None, tao_usd=0.0):
    """
    Score a single subnet across all risk dimensions.
    Returns risk_score 0-100 (higher = more risky) and breakdown.
    """
    nid     = subnet.get("netuid", 0)
    em      = float(subnet.get("emission", 0) or 0)
    burn    = float(subnet.get("burn_tao", 0) or 0)
    neurons = int(subnet.get("neurons", 0) or 0)
    max_n   = int(subnet.get("max_neurons", 256) or 256)
    fill    = (neurons / max_n * 100) if max_n > 0 else 0

    flags = {}
    total_risk = 0

    # 1. Zero emission (weight 30)
    if em == 0:
        score = 30
        flags["zero_emission"] = {
            "triggered": True,
            "score": score,
            "weight": RISK_WEIGHTS["zero_emission"]["weight"],
            "severity": "CRITICAL",
            "detail": f"emission=0.0 τ — no rewards available",
            "metric": "emission==0",
        }
        total_risk += score
    else:
        flags["zero_emission"] = {"triggered": False, "score": 0}

    # 2. Negative mover — emission falling (weight 20)
    mover_info = None
    if movers:
        mover_info = next((m for m in movers if m.get("netuid") == nid
                           and m.get("prev_emission", 0) > 0), None)
    if mover_info and mover_info.get("em_pct_change", 0) < -10:
        pct = mover_info["em_pct_change"]
        severity = "CRITICAL" if pct < -50 else "HIGH"
        score = min(20, int(abs(pct) / 5))
        flags["negative_mover"] = {
            "triggered": True,
            "score": score,
            "weight": RISK_WEIGHTS["negative_mover"]["weight"],
            "severity": severity,
            "detail": f"emission {pct:+.1f}% in 24h",
            "metric": f"em_pct_change={pct:.1f}%",
        }
        total_risk += score
    else:
        flags["negative_mover"] = {"triggered": False, "score": 0}

    # 3. High burn cost relative to emission (weight 15)
    if burn > 0 and em > 0:
        payback_tempos = burn / em
        if payback_tempos > 100:
            score = min(15, int(payback_tempos / 20))
            flags["high_burn"] = {
                "triggered": True,
                "score": score,
                "weight": RISK_WEIGHTS["high_burn"]["weight"],
                "severity": "HIGH" if payback_tempos > 500 else "MEDIUM",
                "detail": f"burn={burn:.4f}τ requires {payback_tempos:.0f} tempos to recoup",
                "metric": f"burn/emission={payback_tempos:.0f} tempos",
            }
            total_risk += score
        else:
            flags["high_burn"] = {"triggered": False, "score": 0}
    elif burn > 0.5 and em == 0:
        flags["high_burn"] = {
            "triggered": True,
            "score": 15,
            "weight": 15,
            "severity": "HIGH",
            "detail": f"burn={burn:.4f}τ with zero emission",
            "metric": f"burn={burn:.4f}τ, emission=0",
        }
        total_risk += 15
    else:
        flags["high_burn"] = {"triggered": False, "score": 0}

    # 4. Overcrowded — at or near capacity (weight 15)
    if fill >= 100:
        score = 15
        flags["overcrowded"] = {
            "triggered": True,
            "score": score,
            "weight": RISK_WEIGHTS["overcrowded"]["weight"],
            "severity": "MEDIUM",
            "detail": f"fill={fill:.0f}% ({neurons}/{max_n}) — no open slots",
            "metric": f"fill_pct={fill:.0f}%",
        }
        total_risk += score
    elif fill >= 90:
        score = 8
        flags["overcrowded"] = {
            "triggered": True,
            "score": score,
            "weight": RISK_WEIGHTS["overcrowded"]["weight"],
            "severity": "LOW",
            "detail": f"fill={fill:.0f}% — nearly full",
            "metric": f"fill_pct={fill:.0f}%",
        }
        total_risk += score
    else:
        flags["overcrowded"] = {"triggered": False, "score": 0}

    # 5. Volatility — large emission swings (weight 10)
    if mover_info and mover_info.get("prev_emission", 0) > 0:
        pct_change = abs(mover_info.get("em_pct_change", 0))
        if pct_change > 30:
            score = min(10, int(pct_change / 10))
            flags["volatility"] = {
                "triggered": True,
                "score": score,
                "weight": RISK_WEIGHTS["volatility"]["weight"],
                "severity": "MEDIUM",
                "detail": f"emission swung {pct_change:.1f}% in 24h",
                "metric": f"|em_pct_change|={pct_change:.1f}%",
            }
            total_risk += score
        else:
            flags["volatility"] = {"triggered": False, "score": 0}
    else:
        flags["volatility"] = {"triggered": False, "score": 0}

    # 6. Low reward — emission below threshold (weight 10)
    LOW_EMISSION_THRESHOLD = 0.001
    if 0 < em < LOW_EMISSION_THRESHOLD:
        usd_per_tempo = em * tao_usd if tao_usd > 0 else 0
        score = 10
        flags["low_reward"] = {
            "triggered": True,
            "score": score,
            "weight": RISK_WEIGHTS["low_reward"]["weight"],
            "severity": "LOW",
            "detail": f"emission={em:.6f}τ" + (f" (${usd_per_tempo:.4f}/tempo)" if usd_per_tempo > 0 else ""),
            "metric": f"emission={em:.6f}τ < threshold {LOW_EMISSION_THRESHOLD}τ",
        }
        total_risk += score
    else:
        flags["low_reward"] = {"triggered": False, "score": 0}

    total_risk = min(100, total_risk)

    if total_risk >= 60:
        risk_label = "CRITICAL"
    elif total_risk >= 40:
        risk_label = "HIGH"
    elif total_risk >= 20:
        risk_label = "MEDIUM"
    elif total_risk > 0:
        risk_label = "LOW"
    else:
        risk_label = "CLEAN"

    active_flags = [v for v in flags.values() if v.get("triggered")]

    return {
        "netuid":        nid,
        "name":          subnet.get("name", "Unknown"),
        "risk_score":    total_risk,
        "risk_label":    risk_label,
        "active_flags":  len(active_flags),
        "flags":         flags,
        "formula":       "risk_score = zero_emission(30) + negative_mover(20) + high_burn(15) + overcrowded(15) + volatility(10) + low_reward(10)",
    }

def scan_network_risk(subnets, movers=None, tao_usd=0.0, my_netuids=None, top_n=15):
    """
    Scan all subnets and return risk-ranked list.
    Prioritizes: my_netuids first, then by risk_score.
    """
    all_risks = []
    for s in subnets:
        r = score_subnet_risk(s, movers, tao_usd)
        r["is_mine"] = s.get("netuid") in (my_netuids or [])
        if r["risk_score"] > 0:
            all_risks.append(r)

    all_risks.sort(key=lambda x: (not x["is_mine"], -x["risk_score"]))
    return all_risks[:top_n]

def build_risk_payload(subnets, movers, tao_usd, my_netuids, snapshot_count):
    """Build full risk payload for formatter."""
    risks = scan_network_risk(subnets, movers, tao_usd, my_netuids, top_n=15)

    rows = []
    for r in risks:
        active = [k for k, v in r["flags"].items() if v.get("triggered")]
        flag_str = " | ".join([r["flags"][f]["detail"][:25] for f in active[:2]])
        rows.append({
            "netuid":     r["netuid"],
            "name":       r["name"],
            "risk_score": r["risk_score"],
            "risk_label": r["risk_label"],
            "flags":      flag_str,
            "is_mine":    r["is_mine"],
            "formula_inputs": {k: v["score"] for k,v in r["flags"].items() if v.get("triggered")},
        })

    return {
        "intent":           "risk",
        "question_type":    "WEIGHTED_RISK_SCAN",
        "fields_used":      ["emission", "burn_tao", "neurons", "max_neurons", "em_pct_change (movers)"],
        "computation":      "risk_score = sum of weighted flag scores. Max 100.",
        "formula":          "zero_emission(30)+negative_mover(20)+high_burn(15)+overcrowded(15)+volatility(10)+low_reward(10)",
        "weights":          RISK_WEIGHTS,
        "confidence":       "HIGH",
        "confidence_reason":"All flags backed by explicit chain metrics",
        "snapshot_count":   snapshot_count,
        "results":          rows,
        "result_count":     len(rows),
    }

def format_risk_table(payload):
    """Format risk payload as terminal table."""
    from formatter import divider, table, pad

    results = payload.get("results", [])
    formula = payload.get("formula","")
    n = len(results)

    rows = []
    for r in results:
        mine = "★" if r.get("is_mine") else " "
        rows.append({
            "mine":    mine,
            "netuid":  f"SN{r['netuid']}",
            "name":    r.get("name","Unknown")[:14],
            "score":   f"{r.get('risk_score',0)}/100",
            "label":   r.get("risk_label","—"),
            "flags":   r.get("flags","—")[:35],
        })

    cols = [
        ("mine",  "★", 2,  False),
        ("netuid","Subnet", 8,  False),
        ("name",  "Name",  14,  False),
        ("score", "Risk",   7,  True),
        ("label", "Level",  8,  False),
        ("flags", "Active Flags", 35, False),
    ]

    lines = [
        divider(),
        f"  WEIGHTED RISK SCAN — {n} SUBNETS FLAGGED",
        f"  Formula: {formula}",
        f"  Weights: zero_emission=30 | neg_mover=20 | high_burn=15 | overcrowded=15 | volatility=10 | low_reward=10",
        f"  ★ = my tracked subnet",
        divider(),
    ]
    lines.append(table(rows, cols))
    lines.append("")
    lines.append(f"  Confidence: {payload.get('confidence','HIGH')}  |  {payload.get('confidence_reason','')}")
    lines.append(divider())
    return "\n".join(lines)
