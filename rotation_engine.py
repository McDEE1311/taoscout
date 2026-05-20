#!/usr/bin/env python3
"""
TaoScout Rotation Engine v1.0
Capital rotation detection and alpha signal generation for the trading bot.
All deterministic Python. No LLM.
"""

from datetime import datetime, timezone


def detect_rotation(health_scores: list, dereg_scores: list, movers: list) -> dict:
    """
    Detect likely capital rotation events.
    FROM: weak/high-risk subnets
    TO:   strong/growing subnets
    """
    hs_map = {h["netuid"]: h for h in health_scores}
    dr_map = {d["netuid"]: d for d in dereg_scores}

    # Rotation sources: high dereg risk + falling emission
    from_candidates = []
    for dr in dereg_scores:
        nid = dr["netuid"]
        mover = next((m for m in movers if m.get("netuid") == nid), None)
        em_chg = float(mover.get("em_pct_change", 0)) if mover else 0
        if dr["dereg_risk"] >= 40 and em_chg <= -15:
            from_candidates.append({
                "netuid":     nid,
                "name":       dr["name"],
                "dereg_risk": dr["dereg_risk"],
                "em_pct_chg": round(em_chg, 1),
                "signal":     "high risk + falling emission",
            })

    # Rotation targets: strong health + rising emission
    to_candidates = []
    for h in health_scores:
        nid = h["netuid"]
        mover = next((m for m in movers if m.get("netuid") == nid), None)
        em_chg = float(mover.get("em_pct_change", 0)) if mover else 0
        dr = dr_map.get(nid, {})
        if h["health_score"] >= 60 and em_chg >= 10 and dr.get("dereg_risk", 0) < 25:
            to_candidates.append({
                "netuid":       nid,
                "name":         h["name"],
                "health_score": h["health_score"],
                "em_pct_chg":   round(em_chg, 1),
                "signal":       "strong health + rising emission",
            })

    # Confidence based on signal count and magnitude
    from_count = len(from_candidates)
    to_count   = len(to_candidates)
    if from_count >= 3 and to_count >= 2:
        confidence = 0.75
    elif from_count >= 2 and to_count >= 2:
        confidence = 0.60
    elif from_count >= 1 and to_count >= 1:
        confidence = 0.40
    else:
        confidence = 0.20

    rotation_detected = from_count >= 1 and to_count >= 1

    return {
        "rotation_detected":  rotation_detected,
        "confidence":         round(confidence, 2),
        "from_subnets":       from_candidates[:5],
        "to_subnets":         to_candidates[:5],
        "from_count":         from_count,
        "to_count":           to_count,
        "note":               "Capital likely rotating from weak/declining to strong/growing subnets" if rotation_detected else "No clear rotation signal",
        "timestamp":          datetime.now(timezone.utc).isoformat(),
    }


def compute_trade_relevance(subnet_nid: int, health: dict, dereg: dict,
                             mover: dict, rotation: dict) -> dict:
    """
    Compute trade relevance score 0-100 for a subnet.
    This is what the trading bot uses to prioritize signals.
    Does NOT make trade decisions — only rates signal quality.
    """
    scores = {}

    # 1. Signal strength (30 pts) — based on health score
    hs = health.get("health_score", 0)
    if hs >= 75:    scores["signal_strength"] = 30
    elif hs >= 60:  scores["signal_strength"] = 22
    elif hs >= 45:  scores["signal_strength"] = 14
    elif hs >= 30:  scores["signal_strength"] = 6
    else:           scores["signal_strength"] = 0

    # 2. Confidence (25 pts) — inverse of dereg risk
    dr = dereg.get("dereg_risk", 50)
    if dr <= 10:    scores["confidence"] = 25
    elif dr <= 20:  scores["confidence"] = 20
    elif dr <= 35:  scores["confidence"] = 12
    elif dr <= 50:  scores["confidence"] = 5
    else:           scores["confidence"] = 0

    # 3. Freshness (15 pts) — based on emission momentum
    em_chg = float(mover.get("em_pct_change", 0)) if mover else 0
    if abs(em_chg) >= 30:   scores["freshness"] = 15   # large fresh move
    elif abs(em_chg) >= 15: scores["freshness"] = 10
    elif abs(em_chg) >= 5:  scores["freshness"] = 6
    else:                   scores["freshness"] = 2    # stale/no move

    # 4. Liquidity relevance (15 pts) — based on absolute emission size
    em = health.get("emission", 0)
    if em >= 0.015:   scores["liquidity"] = 15
    elif em >= 0.008: scores["liquidity"] = 10
    elif em >= 0.003: scores["liquidity"] = 6
    elif em >= 0.001: scores["liquidity"] = 3
    else:             scores["liquidity"] = 0

    # 5. Rotation strength (15 pts) — is this subnet in a rotation signal?
    to_netuids = [s["netuid"] for s in rotation.get("to_subnets", [])]
    from_netuids = [s["netuid"] for s in rotation.get("from_subnets", [])]
    rot_conf = float(rotation.get("confidence", 0))

    if subnet_nid in to_netuids:
        scores["rotation_strength"] = int(15 * rot_conf)
    elif subnet_nid in from_netuids:
        scores["rotation_strength"] = 0   # in outflow — negative for longs
    else:
        scores["rotation_strength"] = int(5 * rot_conf)

    total = min(100, sum(scores.values()))

    # Directional bias
    if em_chg >= 10 and hs >= 55 and dr <= 30:
        bias = "bullish"
    elif em_chg <= -15 or dr >= 50 or hs <= 25:
        bias = "bearish"
    else:
        bias = "neutral"

    reasons = []
    if em_chg >= 15:    reasons.append(f"emission +{em_chg:.1f}% rising")
    elif em_chg <= -15: reasons.append(f"emission {em_chg:.1f}% declining")
    if hs >= 65:        reasons.append(f"health score {hs}/100 strong")
    elif hs <= 30:      reasons.append(f"health score {hs}/100 weak")
    if dr <= 15:        reasons.append("low dereg risk")
    elif dr >= 50:      reasons.append(f"high dereg risk {dr}/100")
    if subnet_nid in to_netuids: reasons.append("rotation target")
    if subnet_nid in from_netuids: reasons.append("rotation source — capital leaving")

    risk_notes = []
    if health.get("fill_pct", 0) >= 98: risk_notes.append("subnet at capacity — slot risk")
    if abs(em_chg) >= 50:               risk_notes.append("high volatility — wait for confirmation")
    if dr >= 40:                        risk_notes.append("elevated dereg risk — reduce size")
    if not risk_notes:                  risk_notes.append("no major risk flags")

    # Time horizon based on signal type
    if abs(em_chg) >= 30: horizon = "1d-3d"
    elif abs(em_chg) >= 10: horizon = "3d-7d"
    else: horizon = "7d-14d"

    return {
        "netuid":            subnet_nid,
        "name":              health.get("name", "Unknown"),
        "health_score":      hs,
        "dereg_risk":        dr,
        "trade_relevance":   total,
        "directional_bias":  bias,
        "confidence":        round(total / 100, 2),
        "time_horizon":      horizon,
        "breakdown":         scores,
        "reasons":           reasons[:4],
        "risk_notes":        risk_notes[:3],
        "em_pct_change":     round(em_chg, 1),
        "emission_tao":      health.get("emission", 0),
    }


def build_alpha_signals(health_scores: list, dereg_scores: list,
                         movers: list, rotation: dict,
                         min_relevance: int = 40) -> list:
    """
    Build final alpha signal list for the trading bot.
    Only returns signals with trade_relevance >= min_relevance.
    Sorted by trade_relevance descending.
    """
    dr_map    = {d["netuid"]: d for d in dereg_scores}
    mov_map   = {m["netuid"]: m for m in movers}
    signals   = []

    for h in health_scores:
        nid  = h["netuid"]
        dr   = dr_map.get(nid, {"dereg_risk": 50, "name": h["name"]})
        mov  = mov_map.get(nid)
        sig  = compute_trade_relevance(nid, h, dr, mov, rotation)
        if sig["trade_relevance"] >= min_relevance:
            signals.append(sig)

    signals.sort(key=lambda x: -x["trade_relevance"])
    return signals
