#!/usr/bin/env python3
"""
TaoScout Intelligence Engine v1.0
- Stake Flow Rankings
- Subnet Death Risk
- Registration Opportunity Predictor
"""

# ── STAKE FLOW ENGINE ─────────────────────────────────────────────────────────
def build_stake_flow(subnets, tao_usd=0.0, top_n=15):
    """
    Rank subnets by alpha stake flow.
    Positive = TAO flowing IN (bullish)
    Negative = TAO flowing OUT (bearish)
    Stake moves BEFORE emissions move — this is the leading indicator.
    """
    rows = []
    for s in subnets:
        flow_1d  = float(s.get("net_flow_1d", 0) or 0)
        flow_7d  = float(s.get("net_flow_7d", 0) or 0)
        flow_30d = float(s.get("net_flow_30d", 0) or 0)
        em       = float(s.get("emission", 0) or 0)
        proj     = float(s.get("emission_projected", 0) or 0)
        emission = proj if em == 0 and proj > 0 else em

        # Skip root subnet and subnets with no flow data
        if s["netuid"] == 0:
            continue
        if flow_1d == 0 and flow_7d == 0:
            continue

        # Flow signal
        if flow_1d > 100:
            signal = "STRONG INFLOW"
            sentiment = "BULLISH"
        elif flow_1d > 10:
            signal = "INFLOW"
            sentiment = "BULLISH"
        elif flow_1d > 0:
            signal = "SLIGHT INFLOW"
            sentiment = "NEUTRAL"
        elif flow_1d > -10:
            signal = "SLIGHT OUTFLOW"
            sentiment = "NEUTRAL"
        elif flow_1d > -100:
            signal = "OUTFLOW"
            sentiment = "BEARISH"
        else:
            signal = "STRONG OUTFLOW"
            sentiment = "BEARISH"

        # Momentum: is 1d flow accelerating vs 7d avg?
        flow_7d_daily = flow_7d / 7 if flow_7d else 0
        momentum = "ACCELERATING" if abs(flow_1d) > abs(flow_7d_daily) * 1.2 else "STABLE"

        rows.append({
            "netuid":       s["netuid"],
            "name":         s.get("name", "Unknown"),
            "net_flow_1d":  round(flow_1d, 2),
            "net_flow_7d":  round(flow_7d, 2),
            "net_flow_30d": round(flow_30d, 2),
            "flow_1d_usd":  round(flow_1d * tao_usd, 2) if tao_usd > 0 else None,
            "signal":       signal,
            "sentiment":    sentiment,
            "momentum":     momentum,
            "emission_tao": round(emission, 6),
            "emission_usd": round(emission * tao_usd, 4) if tao_usd > 0 else None,
            "active_validators": s.get("active_validators", 0),
            "active_miners":     s.get("active_miners", 0),
        })

    # Sort: inflows first (descending), then outflows
    rows.sort(key=lambda x: x["net_flow_1d"], reverse=True)

    inflows  = [r for r in rows if r["net_flow_1d"] > 0]
    outflows = [r for r in rows if r["net_flow_1d"] < 0]

    return {
        "intent":          "stake_flow",
        "question_type":   "STAKE_FLOW",
        "confidence":      "HIGH",
        "confidence_reason": "Live TaoStats net_flow_1d/7d/30d data",
        "fields_used":     ["net_flow_1d", "net_flow_7d", "net_flow_30d", "emission"],
        "computation":     "Sorted by net_flow_1d DESC. Stake flow precedes emission changes.",
        "tao_usd":         tao_usd,
        "top_inflows":     inflows[:top_n],
        "top_outflows":    outflows[-top_n:],
        "total_tracked":   len(rows),
        "note":            "Stake flow moves BEFORE emissions. Use as leading indicator.",
    }


# ── DEATH RISK ENGINE ─────────────────────────────────────────────────────────
def build_death_risk(subnets, tao_usd=0.0, top_n=15):
    """
    Score each subnet 0-100 for risk of dying (losing emission, validators, stake).
    High score = high death risk.
    """
    scored = []
    for s in subnets:
        em       = float(s.get("emission", 0) or 0)
        proj     = float(s.get("emission_projected", 0) or 0)
        emission = proj if em == 0 and proj > 0 else em
        flow_1d  = float(s.get("net_flow_1d", 0) or 0)
        flow_7d  = float(s.get("net_flow_7d", 0) or 0)
        av       = int(s.get("active_validators", 0) or 0)
        am       = int(s.get("active_miners", 0) or 0)
        neurons  = int(s.get("neurons", 0) or 0)
        max_n    = int(s.get("max_neurons", 256) or 256)
        fill_pct = (neurons / max_n * 100) if max_n > 0 else 0
        reg_ok   = s.get("registration_allowed", True)

        risk_score = 0
        flags = []

        # 1. Zero emission (30 pts)
        if emission == 0:
            risk_score += 30
            flags.append("zero_emission")

        # 2. Strong negative flow (25 pts)
        if flow_1d < -300:
            risk_score += 25
            flags.append(f"severe_outflow:{flow_1d:.0f}_TAO/day")
        elif flow_1d < -100:
            risk_score += 15
            flags.append(f"strong_outflow:{flow_1d:.0f}_TAO/day")
        elif flow_1d < -10:
            risk_score += 8
            flags.append(f"outflow:{flow_1d:.0f}_TAO/day")

        # 3. No active validators (20 pts)
        if av == 0 and neurons > 0:
            risk_score += 20
            flags.append("no_active_validators")
        elif av <= 2 and neurons > 10:
            risk_score += 10
            flags.append(f"few_validators:{av}")

        # 4. No active miners (15 pts)
        if am == 0 and neurons > 5:
            risk_score += 15
            flags.append("no_active_miners")
        elif am <= 2 and neurons > 10:
            risk_score += 8
            flags.append(f"few_miners:{am}")

        # 5. Registration closed with low fill (10 pts)
        if not reg_ok and fill_pct < 50:
            risk_score += 10
            flags.append("registration_closed_low_fill")

        # 7d flow trend confirmation
        if flow_7d < -500:
            risk_score = min(100, risk_score + 10)
            flags.append(f"sustained_outflow_7d:{flow_7d:.0f}")

        risk_score = min(100, risk_score)

        if risk_score >= 70:
            level = "CRITICAL"
        elif risk_score >= 45:
            level = "HIGH"
        elif risk_score >= 25:
            level = "MEDIUM"
        else:
            level = "LOW"

        scored.append({
            "netuid":            s["netuid"],
            "name":              s.get("name", "Unknown"),
            "death_risk":        risk_score,
            "risk_level":        level,
            "flags":             flags,
            "emission_tao":      round(emission, 6),
            "net_flow_1d":       round(flow_1d, 2),
            "net_flow_7d":       round(flow_7d, 2),
            "active_validators": av,
            "active_miners":     am,
            "fill_pct":          round(fill_pct, 1),
            "registration_allowed": reg_ok,
        })

    scored.sort(key=lambda x: x["death_risk"], reverse=True)

    critical = [r for r in scored if r["risk_level"] == "CRITICAL"]
    high     = [r for r in scored if r["risk_level"] == "HIGH"]
    low_risk = [r for r in scored if r["risk_level"] == "LOW" and r["emission_tao"] > 0]
    low_risk.sort(key=lambda x: x["emission_tao"], reverse=True)

    return {
        "intent":          "death_risk",
        "question_type":   "DEATH_RISK",
        "confidence":      "HIGH",
        "confidence_reason": "Composite of emission, flow, validator, miner data",
        "fields_used":     ["emission", "net_flow_1d", "net_flow_7d", "active_validators", "active_miners"],
        "computation":     "Weighted risk: zero_emission(30)+flow(25)+validators(20)+miners(15)+registration(10)",
        "tao_usd":         tao_usd,
        "critical_risk":   critical[:top_n],
        "high_risk":       high[:top_n],
        "safest_subnets":  low_risk[:5],
        "total_scored":    len(scored),
    }


# ── REGISTRATION OPPORTUNITY PREDICTOR ────────────────────────────────────────
def build_registration_opportunities(subnets, tao_usd=0.0, top_n=10):
    """
    Score subnets for registration opportunity.
    Best = open slots + positive inflow + growing validators + good emission.
    """
    opportunities = []
    for s in subnets:
        em       = float(s.get("emission", 0) or 0)
        proj     = float(s.get("emission_projected", 0) or 0)
        emission = proj if em == 0 and proj > 0 else em
        burn     = float(s.get("burn_tao", 0) or s.get("neuron_reg_cost_tao", 0) or 0)
        flow_1d  = float(s.get("net_flow_1d", 0) or 0)
        av       = int(s.get("active_validators", 0) or 0)
        neurons  = int(s.get("neurons", 0) or 0)
        max_n    = int(s.get("max_neurons", 256) or 256)
        fill_pct = (neurons / max_n * 100) if max_n > 0 else 100
        reg_ok   = s.get("registration_allowed", True)
        regs     = int(s.get("regs_this_interval", 0) or 0)
        blocks   = int(s.get("blocks_until_epoch", 0) or 0)

        # Must have registration open and emission > 0
        if not reg_ok or emission == 0:
            continue

        # Must have open slots
        open_slots = max_n - neurons
        if open_slots <= 0:
            continue

        reg_score = 0
        reasons = []

        # 1. Open slots score (25 pts)
        fill_score = max(0, 25 - int(fill_pct / 4))
        reg_score += fill_score
        if fill_pct < 30:
            reasons.append(f"very_open:{fill_pct:.0f}%_fill_{open_slots}_slots")
        elif fill_pct < 60:
            reasons.append(f"open:{fill_pct:.0f}%_fill_{open_slots}_slots")

        # 2. Emission strength (25 pts)
        MAX_EMISSION = 0.03
        em_score = min(25, int((emission / MAX_EMISSION) * 25))
        reg_score += em_score
        if emission > 0.01:
            reasons.append(f"strong_emission:{emission:.5f}_TAO")

        # 3. Stake inflow (25 pts)
        if flow_1d > 200:
            reg_score += 25
            reasons.append(f"strong_inflow:+{flow_1d:.0f}_TAO/day")
        elif flow_1d > 50:
            reg_score += 15
            reasons.append(f"inflow:+{flow_1d:.0f}_TAO/day")
        elif flow_1d > 0:
            reg_score += 8
            reasons.append(f"slight_inflow:+{flow_1d:.0f}_TAO/day")
        elif flow_1d < -100:
            reg_score -= 10
            reasons.append(f"warning:outflow:{flow_1d:.0f}_TAO/day")

        # 4. Validator presence (15 pts)
        if av >= 5:
            reg_score += 15
            reasons.append(f"healthy_validators:{av}")
        elif av >= 2:
            reg_score += 8
            reasons.append(f"validators:{av}")
        elif av == 0:
            reg_score -= 5
            reasons.append("warning:no_active_validators")

        # 5. Low burn cost (10 pts)
        if burn < 0.01:
            reg_score += 10
            reasons.append(f"low_burn:{burn:.4f}_TAO")
        elif burn < 0.1:
            reg_score += 5
            reasons.append(f"burn:{burn:.4f}_TAO")

        # Timing bonus
        if blocks < 100:
            reasons.append(f"epoch_soon:{blocks}_blocks")

        reg_score = max(0, min(100, reg_score))

        if reg_score >= 70:
            signal = "STRONG BUY"
        elif reg_score >= 50:
            signal = "BUY"
        elif reg_score >= 35:
            signal = "WATCH"
        else:
            signal = "WEAK"

        opportunities.append({
            "netuid":            s["netuid"],
            "name":              s.get("name", "Unknown"),
            "reg_score":         reg_score,
            "signal":            signal,
            "reasons":           reasons,
            "open_slots":        open_slots,
            "fill_pct":          round(fill_pct, 1),
            "emission_tao":      round(emission, 6),
            "emission_usd":      round(emission * tao_usd, 4) if tao_usd > 0 else None,
            "burn_tao":          round(burn, 5),
            "burn_usd":          round(burn * tao_usd, 2) if tao_usd > 0 else None,
            "net_flow_1d":       round(flow_1d, 2),
            "active_validators": av,
            "blocks_until_epoch": blocks,
        })

    opportunities.sort(key=lambda x: x["reg_score"], reverse=True)

    return {
        "intent":          "registration_opportunities",
        "question_type":   "REGISTRATION_OPPORTUNITIES",
        "confidence":      "HIGH",
        "confidence_reason": "Live chain + TaoStats data",
        "fields_used":     ["emission", "burn_tao", "fill_pct", "net_flow_1d", "active_validators", "registration_allowed"],
        "computation":     "Score: open_slots(25)+emission(25)+stake_flow(25)+validators(15)+burn(10)",
        "tao_usd":         tao_usd,
        "opportunities":   opportunities[:top_n],
        "total_eligible":  len(opportunities),
        "note":            "Only subnets with open registration and active emission shown.",
    }
