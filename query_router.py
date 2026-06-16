#!/usr/bin/env python3
"""
TaoScout Query Router v1.0
Intercepts natural language questions, routes to deterministic functions,
returns structured JSON payload. LLM only formats output, never reasons about data.
"""
import re
from pathlib import Path

# ── Intent patterns ───────────────────────────────────────────────────────────
INTENTS = [
    # Emission queries
    ("top_ratio",         [r"emission.*burn ratio", r"burn.*ratio", r"top.*ratio", r"best ratio", r"highest ratio"]),
    ("top_emission",      [r"top.*emission", r"highest emission", r"most emission", r"best emission", r"emission rank"]),
    ("emission_threshold",[ r"emission.*above", r"emission.*over", r"emission.*greater", r"above.*emission", r"more than.*tao", r"over.*0\.\d+"]),
    ("zero_emission",     [r"zero emission", r"no emission", r"zero.*emit", r"emit.*zero"]),

    # Ratio queries

    # GPU / hardware queries
    ("gpu_ranking",       [r"3090", r"4090", r"5090", r"6000", r"rtx", r"gpu", r"vram", r"hardware", r"24gb", r"32gb", r"48gb", r"96gb", r"card", r"mine.*with", r"run.*on"]),

    # Validator trends
    ("validator_trends",  [r"validator", r"val trend", r"val count", r"who controls", r"stake concentration"]),

    # Movers / trends
    ("movers",            [r"mover", r"chang", r"trend", r"increas", r"decreas", r"up.*emission", r"down.*emission", r"momentum", r"24h", r"20h", r"last.*hour"]),

    # My subnets — ONLY when explicitly mentioned
    ("my_subnets",        [r"\bsn54\b", r"\bsn39\b", r"my subnet", r"miid", r"yanez", r"basilica", r"cathedral", r"my miner"]),

    # Single subnet queries
    ("subnet_detail",     [r"sn\d+", r"subnet \d+", r"netuid \d+"]),

    # Compare
    ("compare",           [r"compar", r"vs\b", r"versus", r"better.*subnet", r"which.*better", r"between"]),

    # Risk
    ("risk",              [r"risk", r"danger", r"warn", r"avoid", r"concern", r"problem", r"issue", r"flag"]),

    # Fill / capacity
    ("fill_rate",         [r"fill", r"capacity", r"slot", r"register", r"entry", r"space", r"room"]),

    # Status / summary
    ("network_summary",   [r"summary", r"overview", r"state of", r"landscape", r"network", r"brief", r"report"]),

    # Subnet reward / status lookup — triggered by subnet mention + reward/award/earn
    ("subnet_reward_lookup", [r"reward.*sn\d+", r"sn\d+.*reward", r"award.*sn\d+", r"sn\d+.*award",
                               r"earn.*sn\d+", r"sn\d+.*earn", r"payout.*sn\d+", r"emission.*sn\d+",
                               r"miner.*sn\d+", r"sn\d+.*miner"]),

    # Investment scan — speculative, returns candidates not winners
    ("investment_scan",   [r"best.*buy", r"buy.*best", r"invest", r"low.*value.*reward",
                            r"high.*reward", r"potential.*reward", r"undervalued", r"gem",
                            r"worth.*buy", r"which.*buy"]),
]

def detect_intent(question):
    """Detect primary intent from question text."""
    q = question.lower()
    for intent, patterns in INTENTS:
        for pat in patterns:
            if re.search(pat, q):
                return intent
    return "general"

def extract_netuid(question):
    """Extract netuid from question if present."""
    matches = re.findall(r'sn(\d+)|subnet\s+(\d+)|netuid\s+(\d+)', question.lower())
    netuids = []
    for m in matches:
        nid = next(n for n in m if n)
        netuids.append(int(nid))
    return netuids

def extract_threshold(question):
    """Extract emission threshold from question."""
    match = re.search(r'(\d+\.?\d*)\s*tao', question.lower())
    if match:
        return float(match.group(1))
    match = re.search(r'0\.(\d+)', question)
    if match:
        return float(f"0.{match.group(1)}")
    return 0.01  # default threshold

def extract_gpu_class(question):
    """Extract GPU class from question."""
    q = question.lower()
    if any(x in q for x in ["96gb", "pro 6000", "pro6000", "blackwell"]):
        return "96gb"
    if any(x in q for x in ["48gb", "a6000", "rtx 6000 amp", "6000 amp"]):
        return "48gb"
    if any(x in q for x in ["32gb", "5090", "rtx 5090"]):
        return "32gb"
    return "24gb"  # default — covers 3090/4090/6000Ada

# ── Deterministic answer builders ─────────────────────────────────────────────

def answer_top_emission(subnets, tao_usd, top_n=10):
    def em(s): return float(s.get("emission", 0) or 0)
    ranked = sorted([s for s in subnets if em(s) > 0], key=em, reverse=True)[:top_n]
    rows = []
    for i, s in enumerate(ranked, 1):
        emission = em(s)
        burn = float(s.get("burn_tao", 0) or 0)
        usd = round(emission * tao_usd, 4) if tao_usd > 0 else None
        rows.append({
            "rank": i, "netuid": s["netuid"], "name": s.get("name","Unknown"),
            "emission_tao": round(emission, 6),
            "emission_usd": usd,
            "burn_tao": round(burn, 4),
            "fill_pct": round((s.get("neurons",0) / s.get("max_neurons",256) * 100) if s.get("max_neurons",256) > 0 else 0, 1),
        })
    return {
        "intent": "top_emission",
        "question_type": "TOP_N_BY_EMISSION",
        "fields_used": ["netuid", "name", "emission (tao_in_emission)", "burn_tao", "neurons", "max_neurons"],
        "sort_field": "emission DESC",
        "computation": "Python sort — no LLM math",
        "confidence": "HIGH",
        "confidence_reason": "Deterministic sort from live chain snapshot",
        "result_count": len(rows),
        "results": rows,
        "tao_usd": tao_usd,
    }

def answer_emission_threshold(subnets, tao_usd, threshold=0.01):
    def em(s): return float(s.get("emission", 0) or 0)
    ranked = sorted([s for s in subnets if em(s) >= threshold],
                    key=em, reverse=True)
    rows = []
    for i, s in enumerate(ranked, 1):
        emission = em(s)
        usd = round(emission * tao_usd, 4) if tao_usd > 0 else None
        rows.append({
            "rank": i, "netuid": s["netuid"], "name": s.get("name","Unknown"),
            "emission_tao": round(emission, 6), "emission_usd": usd,
        })
    return {
        "intent": "emission_threshold",
        "question_type": "EMISSION_ABOVE_THRESHOLD",
        "threshold_tao": threshold,
        "fields_used": ["netuid", "name", "emission (tao_in_emission)"],
        "sort_field": "emission DESC",
        "computation": "Python filter + sort — no LLM math",
        "confidence": "HIGH",
        "confidence_reason": "Deterministic filter from live chain snapshot",
        "result_count": len(rows),
        "results": rows,
        "tao_usd": tao_usd,
    }

def answer_zero_emission(subnets):
    def em(s): return float(s.get("emission", 0) or 0)
    zeros = [s for s in subnets if em(s) == 0]
    rows = [{"netuid": s["netuid"], "name": s.get("name","Unknown"),
             "burn_tao": round(float(s.get("burn_tao",0) or 0), 4)} for s in zeros]
    return {
        "intent": "zero_emission",
        "question_type": "ZERO_EMISSION_SUBNETS",
        "fields_used": ["netuid", "name", "emission (tao_in_emission)", "burn_tao"],
        "computation": "Python filter emission == 0 — no LLM math",
        "confidence": "HIGH",
        "confidence_reason": "Direct chain data, zero emission is a hard fact",
        "result_count": len(rows),
        "results": rows,
    }

def answer_top_ratio(subnets, tao_usd, top_n=10):
    def em(s): return float(s.get("emission", 0) or 0)
    def burn(s): return float(s.get("burn_tao", 0) or 0)
    def ratio(s):
        b = burn(s)
        return em(s) / b if b > 0 and em(s) > 0 else 0.0
    ranked = sorted([s for s in subnets if ratio(s) > 0],
                    key=ratio, reverse=True)[:top_n]
    rows = []
    for i, s in enumerate(ranked, 1):
        r = ratio(s)
        emission = em(s)
        usd = round(emission * tao_usd, 4) if tao_usd > 0 else None
        rows.append({
            "rank": i, "netuid": s["netuid"], "name": s.get("name","Unknown"),
            "ratio": round(r, 2),
            "emission_tao": round(emission, 6),
            "emission_usd": usd,
            "burn_tao": round(burn(s), 4),
        })
    return {
        "intent": "top_ratio",
        "question_type": "TOP_N_BY_EMISSION_BURN_RATIO",
        "fields_used": ["netuid", "name", "emission (tao_in_emission)", "burn_tao"],
        "computation": "ratio = emission / burn_tao — Python sort — no LLM math",
        "sort_field": "ratio DESC",
        "confidence": "HIGH",
        "confidence_reason": "Deterministic computation from live chain snapshot",
        "result_count": len(rows),
        "results": rows,
        "tao_usd": tao_usd,
    }

def answer_gpu_ranking(subnets, gpu_class, tao_usd, movers=None, top_n=10):
    try:
        import analytics as AN
        from analytics import GPU_CLASSES
        if gpu_class not in GPU_CLASSES:
            gpu_class = "24gb"
        gpu_info = GPU_CLASSES[gpu_class]
        ranked = AN.rank_subnets_for_gpu_class(subnets, gpu_class, tao_usd, movers, top_n=top_n)
        rows = []
        for i, r in enumerate(ranked, 1):
            m = r.get("metrics", {})
            rows.append({
                "rank": i, "netuid": r["netuid"], "name": r["name"],
                "combined_score": r["combined_score"],
                "opportunity_score": r["opportunity_score"],
                "fit_score": r["fit_score"],
                "fit_label": r["fit_label"],
                "workload": r["workload"],
                "hw_notes": r["hw_notes"],
                "emission_tao": m.get("emission", 0),
                "emission_usd": m.get("emission_usd"),
                "burn_tao": m.get("burn_tao", 0),
                "fill_pct": m.get("fill_pct", 0),
                "ratio": m.get("ratio", 0),
                "score_breakdown": r.get("breakdown", {}),
            })
        return {
            "intent": "gpu_ranking",
            "question_type": "GPU_CLASS_RANKING",
            "gpu_class": gpu_class,
            "gpu_label": gpu_info["label"],
            "cards": gpu_info["cards"],
            "fields_used": ["netuid", "name", "emission", "burn_tao", "neurons", "max_neurons", "hardware_registry"],
            "computation": "combined_score = opportunity_score x (fit_score/100) — Python — no LLM math",
            "confidence": "HIGH",
            "confidence_reason": "Deterministic scoring from analytics engine",
            "result_count": len(rows),
            "results": rows,
            "tao_usd": tao_usd,
        }
    except ImportError:
        return {"error": "Analytics module not available"}

def answer_movers(movers, snapshot_count, tao_usd):
    if not movers:
        return {
            "intent": "movers",
            "question_type": "EMISSION_MOVERS",
            "available": False,
            "snapshot_count": snapshot_count,
            "confidence": "INSUFFICIENT",
            "confidence_reason": f"Only {snapshot_count} snapshots. Need 4+ for reliable mover data.",
            "results": [],
            "fields_used": ["netuid", "name", "emission (current)", "emission (previous snapshot)"],
        }
    real_movers = [m for m in movers if m.get("prev_emission", 0) > 0]
    rows = []
    for i, m in enumerate(real_movers[:10], 1):
        rows.append({
            "rank": i,
            "netuid": m["netuid"],
            "name": m.get("name", "Unknown"),
            "direction": "UP" if m.get("em_delta", 0) > 0 else "DOWN",
            "em_pct_change": round(m.get("em_pct_change", 0), 2),
            "em_delta": round(m.get("em_delta", 0), 6),
            "curr_emission": round(m.get("curr_emission", 0), 6),
            "prev_emission": round(m.get("prev_emission", 0), 6),
            "fill_delta": round(m.get("fill_delta", 0), 2),
        })
    return {
        "intent": "movers",
        "question_type": "EMISSION_MOVERS_24H",
        "available": True,
        "snapshot_count": snapshot_count,
        "fields_used": ["netuid", "name", "curr_emission", "prev_emission", "em_delta", "em_pct_change", "fill_delta"],
        "computation": "delta = curr_emission - prev_emission from SQLite snapshots — no LLM math",
        "confidence": "MEDIUM",
        "confidence_reason": "Computed from on-chain snapshot comparison",
        "result_count": len(rows),
        "results": rows,
        "new_subnets_excluded": len(movers) - len(real_movers),
    }

def answer_subnet_detail(subnets, netuids, tao_usd, movers=None):
    rows = []
    for nid in netuids[:3]:
        s = next((x for x in subnets if x.get("netuid") == nid), None)
        if not s:
            rows.append({"netuid": nid, "error": "not found in current snapshot"})
            continue
        em = float(s.get("emission", 0) or 0)
        b  = float(s.get("burn_tao", 0) or 0)
        r  = round(em / b, 2) if b > 0 and em > 0 else 0
        neurons = int(s.get("neurons", 0) or 0)
        max_n   = int(s.get("max_neurons", 256) or 256)
        fill    = round(neurons / max_n * 100, 1) if max_n > 0 else 0
        usd     = round(em * tao_usd, 4) if tao_usd > 0 else None

        try:
            import analytics as AN
            from analytics import GPU_CLASSES
            gpu_fits = {cls: AN.gpu_fit_score(nid, GPU_CLASSES[cls]["vram_gb"]) for cls in GPU_CLASSES}
            opp = AN.opportunity_score(s, tao_usd, movers)
        except Exception:
            gpu_fits = {}
            opp = {}

        mover_data = None
        if movers:
            mv = next((m for m in movers if m.get("netuid") == nid), None)
            if mv and mv.get("prev_emission", 0) > 0:
                mover_data = {
                    "direction": "UP" if mv.get("em_delta", 0) > 0 else "DOWN",
                    "em_pct_change": round(mv.get("em_pct_change", 0), 2),
                    "em_delta": round(mv.get("em_delta", 0), 6),
                }

        rows.append({
            "netuid": nid, "name": s.get("name","Unknown"),
            "emission_tao": round(em, 6), "emission_usd": usd,
            "burn_tao": round(b, 4), "ratio": r,
            "fill_pct": fill, "neurons": neurons, "max_neurons": max_n,
            "tempo": s.get("tempo", 360),
            "price": round(float(s.get("price", 0) or 0), 6),
            "opportunity_score": opp.get("score") if opp else None,
            "gpu_fits": gpu_fits,
            "mover_24h": mover_data,
        })
    return {
        "intent": "subnet_detail",
        "question_type": "SUBNET_DETAIL",
        "fields_used": ["netuid", "name", "emission", "burn_tao", "neurons", "max_neurons", "tempo", "price"],
        "computation": "Direct lookup from live chain snapshot",
        "confidence": "HIGH",
        "confidence_reason": "Direct chain data",
        "results": rows,
        "tao_usd": tao_usd,
    }

def answer_my_subnets(subnets, my_netuids, tao_usd, movers=None):
    return answer_subnet_detail(subnets, my_netuids, tao_usd, movers)

def answer_risk(subnets, my_netuids, snapshot_count, dereg_risk=None):
    try:
        import report as R
        section = R.build_risks(subnets, my_netuids, snapshot_count, dereg_risk)
        return {
            "intent": "risk",
            "question_type": "RISK_FLAGS",
            "fields_used": ["emission", "fill_pct", "neurons", "max_neurons", "dereg_risk_api"],
            "computation": "Factual flag scan — no LLM inference",
            "confidence": "HIGH",
            "confidence_reason": "Each flag backed by explicit data source",
            "results": section.get("rows", []),
            "note": section.get("note", ""),
        }
    except Exception as e:
        return {"intent": "risk", "error": str(e)}

def answer_fill_rate(subnets, tao_usd):
    def em(s): return float(s.get("emission", 0) or 0)
    def fill(s):
        mx = s.get("max_neurons", 256)
        return (s.get("neurons", 0) / mx * 100) if mx > 0 else 0
    low_fill = sorted(
        [s for s in subnets if em(s) > 0 and fill(s) < 80],
        key=lambda s: fill(s)
    )
    rows = []
    for s in low_fill[:10]:
        emission = em(s)
        usd = round(emission * tao_usd, 4) if tao_usd > 0 else None
        rows.append({
            "netuid": s["netuid"], "name": s.get("name","Unknown"),
            "fill_pct": round(fill(s), 1),
            "neurons": s.get("neurons", 0),
            "max_neurons": s.get("max_neurons", 256),
            "emission_tao": round(emission, 6),
            "emission_usd": usd,
            "burn_tao": round(float(s.get("burn_tao",0) or 0), 4),
        })
    return {
        "intent": "fill_rate",
        "question_type": "LOW_FILL_WITH_EMISSION",
        "fields_used": ["netuid", "name", "neurons", "max_neurons", "emission", "burn_tao"],
        "computation": "fill_pct = neurons/max_neurons*100, filter < 80%, emission > 0 — Python",
        "confidence": "HIGH",
        "confidence_reason": "Deterministic filter from live chain snapshot",
        "result_count": len(rows),
        "results": rows,
        "tao_usd": tao_usd,
    }

# ── Master router ─────────────────────────────────────────────────────────────

def route(question, chain_data, enrichment, my_netuids, snapshot_count, movers=None):
    """
    Route question to deterministic function.
    Returns structured payload for LLM formatting.
    """
    subnets  = chain_data.get("subnets", [])
    tao_usd  = enrichment.get("tao_price_usd", 0.0)
    dereg    = enrichment.get("dereg_risk", [])
    intent   = detect_intent(question)
    netuids  = extract_netuid(question)

    # My subnets check — only if explicitly mentioned
    is_my_subnet_q = any(nid in my_netuids for nid in netuids) or intent == "my_subnets"

    payload = None

    if intent == "top_emission":
        payload = answer_top_emission(subnets, tao_usd, top_n=10)

    elif intent == "emission_threshold":
        threshold = extract_threshold(question)
        payload = answer_emission_threshold(subnets, tao_usd, threshold)

    elif intent == "zero_emission":
        payload = answer_zero_emission(subnets)

    elif intent == "top_ratio":
        payload = answer_top_ratio(subnets, tao_usd, top_n=10)

    elif intent == "gpu_ranking":
        gpu_class = extract_gpu_class(question)
        payload = answer_gpu_ranking(subnets, gpu_class, tao_usd, movers, top_n=10)

    elif intent == "movers":
        payload = answer_movers(movers or [], snapshot_count, tao_usd)

    elif intent == "my_subnets" or (is_my_subnet_q and netuids):
        target = [n for n in netuids if n in my_netuids] or my_netuids
        payload = answer_my_subnets(subnets, target, tao_usd, movers)

    elif intent == "subnet_detail" and netuids:
        payload = answer_subnet_detail(subnets, netuids, tao_usd, movers)

    elif intent == "risk":
        payload = answer_risk(subnets, my_netuids, snapshot_count, dereg)

    elif intent == "fill_rate":
        payload = answer_fill_rate(subnets, tao_usd)

    elif intent == "subnet_reward_lookup" and netuids:
        # Subnet-specific reward lookup — center on mentioned subnet only
        payload = answer_subnet_detail(subnets, netuids[:1], tao_usd, movers)
        payload["intent"] = "subnet_reward_lookup"
        payload["question_type"] = "SUBNET_REWARD_LOOKUP"
        payload["note"] = (
            "Showing deterministic chain metrics for this subnet. "
            "If you mean emission rewards: use emission_tao field. "
            "If you mean alpha token rewards: use price field. "
            "TaoScout does not have a metric called miner_awards — emission is the closest equivalent."
        )

    elif intent == "validator_trends":
        payload = answer_validator_trends(subnets, tao_usd, top_n=10)

    elif intent == "investment_scan":
        # Speculative scan — candidates only, not winners
        em_data    = answer_top_emission(subnets, tao_usd, top_n=10)
        ratio_data = answer_top_ratio(subnets, tao_usd, top_n=10)
        # Find low-price high-ratio candidates
        candidates = []
        for s in subnets:
            em   = float(s.get("emission", 0) or 0)
            burn = float(s.get("burn_tao", 0) or 0)
            price = float(s.get("price", 0) or 0)
            ratio = em / burn if burn > 0 and em > 0 else 0
            if em > 0 and ratio > 5 and price < 0.01:
                candidates.append({
                    "netuid":       s["netuid"],
                    "name":         s.get("name","Unknown"),
                    "emission_tao": round(em, 6),
                    "emission_usd": round(em * tao_usd, 4) if tao_usd > 0 else None,
                    "burn_tao":     round(burn, 4),
                    "ratio":        round(ratio, 2),
                    "alpha_price":  round(price, 6),
                    "fill_pct":     round((s.get("neurons",0)/s.get("max_neurons",256)*100) if s.get("max_neurons",256) > 0 else 0, 1),
                })
        candidates.sort(key=lambda x: x["ratio"], reverse=True)
        payload = {
            "intent":           "investment_scan",
            "question_type":    "SPECULATIVE_CANDIDATE_SCAN",
            "fields_used":      ["emission", "burn_tao", "price (alpha)", "neurons", "max_neurons"],
            "computation":      "filter: emission>0 AND ratio>5 AND alpha_price<0.01, sort by ratio",
            "confidence":       "LOW",
            "confidence_reason": "Speculative scan based on current metrics only. Not investment advice.",
            "results":          candidates[:10],
            "result_count":     len(candidates[:10]),
            "warning":          "This is a speculative scan showing measurable candidates. TaoScout cannot predict future rewards. Not investment advice.",
            "tao_usd":          tao_usd,
        }

    elif intent == "general" and netuids and not is_my_subnet_q:
        # Subnet explicitly mentioned but no specific intent — center on that subnet
        payload = answer_subnet_detail(subnets, netuids[:1], tao_usd, movers)
        payload["intent"] = "subnet_detail"
        payload["note"] = "Specific subnet detected — showing deterministic metrics for this subnet only."

    else:
        # True general fallback — no subnet bias, no tracked subnet injection
        em_data    = answer_top_emission(subnets, tao_usd, top_n=5)
        ratio_data = answer_top_ratio(subnets, tao_usd, top_n=5)
        payload = {
            "intent":            "general",
            "question_type":     "GENERAL_QUERY",
            "top_emission":      em_data["results"],
            "top_ratio":         ratio_data["results"],
            "fields_used":       ["emission", "burn_tao", "neurons", "max_neurons"],
            "confidence":        "LOW",
            "confidence_reason": "Query type not specifically supported. Showing top emission/ratio context only.",
            "unsupported_note":  "This query type does not have a dedicated deterministic route. Answer is based on general chain context. For specific subnet data ask: SN[number] detail",
            "tao_usd":           tao_usd,
        }

    payload["question"]       = question
    payload["detected_intent"] = intent
    payload["block"]           = chain_data.get("block", "?")
    payload["snapshot_count"]  = snapshot_count

    return payload

# ── Validation layer ──────────────────────────────────────────────────────────

def validate_response(llm_text, payload):
    """
    Hard validation of LLM response against deterministic payload.
    Returns (valid: bool, issues: list)
    """
    issues = []
    results = payload.get("results", [])
    if not results:
        return True, []

    result_netuids = {r["netuid"] for r in results if isinstance(r, dict) and "netuid" in r}

    # Check for subnet name cross-contamination
    for r in results:
        if not isinstance(r, dict):
            continue
        nid  = r.get("netuid")
        name = r.get("name", "")
        if nid and name and name.lower() in llm_text.lower():
            # Only flag if wrong netuid appears DIRECTLY next to the name
            wrong = re.search(rf'sn(\d+)\s*[\(\-\s]+{re.escape(name.lower())}|{re.escape(name.lower())}\s*[\(\-\s]+sn(\d+)', llm_text.lower())
            if wrong:
                found_nid = int(wrong.group(1) or wrong.group(2))
                if found_nid != nid:
                    issues.append(f"Cross-contamination: {name} associated with wrong netuid in response")

    # Check zero emission claims
    for r in results:
        if not isinstance(r, dict):
            continue
        nid = r.get("netuid")
        em  = r.get("emission_tao", -1)
        if nid and em > 0:
            if re.search(rf'sn{nid}.*zero emission|zero.*sn{nid}', llm_text.lower()):
                issues.append(f"CRITICAL: LLM claims SN{nid} has zero emission but emission={em}")

    return len(issues) == 0, issues

# ── LLM format prompt ─────────────────────────────────────────────────────────

FORMAT_SYSTEM = """You are TaoScout, a Bittensor operator intelligence assistant.

You receive a structured JSON payload with pre-computed deterministic results.
Your ONLY job is to format this data into a clear operator response.

STRICT RULES:
1. NEVER invent, calculate, or infer data not present in the payload.
2. NEVER reorder results — they are already sorted correctly.
3. NEVER associate a subnet name with a different netuid than provided.
4. NEVER claim zero emission for a subnet with emission_tao > 0.
5. If a field is null or missing, say "not available" — never guess.
6. Always cite the exact fields_used from the payload in your BASIS.
7. Use this format exactly:

FINDING: [direct answer based only on payload data]
BASIS: [list the exact fields_used from payload, cite block height]
CONFIDENCE: [use confidence from payload] — [confidence_reason from payload]
GAPS: [what data is missing or would improve the answer]

Do not add commentary, opinions, or analysis beyond what the payload contains."""

def build_format_prompt(question, payload):
    import json
    try:
        import formatter
        table_str = formatter.format_payload(payload)
    except Exception as e:
        table_str = f"[Table unavailable: {e}]"

    compact = {
        "question":          question,
        "intent":            payload.get("intent"),
        "confidence":        payload.get("confidence"),
        "confidence_reason": payload.get("confidence_reason"),
        "fields_used":       payload.get("fields_used", []),
        "computation":       payload.get("computation",""),
        "block":             payload.get("block","?"),
        "tao_usd":           payload.get("tao_usd", 0),
        "result_count":      payload.get("result_count", len(payload.get("results",[]))),
        "top_results":       payload.get("results", [])[:3],
    }

    return f"""Question: {question}

Pre-formatted table (already shown to user — do NOT reproduce it):
{table_str}

Payload summary (use for FINDING/BASIS/CONFIDENCE/GAPS only):
{json.dumps(compact, indent=2, default=str)}

Write operator summary: FINDING / BASIS / CONFIDENCE / GAPS only. Under 6 lines. No tables."""

def answer_validator_trends(subnets, tao_usd, top_n=10):
    """Return subnets ranked by validator activity and flow."""
    rows = []
    for s in subnets:
        av = s.get("active_validators", 0) or 0
        am = s.get("active_miners", 0) or 0
        flow = s.get("net_flow_1d", 0) or 0
        em = float(s.get("emission", 0) or 0)
        if av > 0 or flow != 0:
            rows.append({
                "netuid":            s["netuid"],
                "name":              s.get("name", "Unknown"),
                "active_validators": av,
                "active_miners":     am,
                "net_flow_1d":       round(float(flow), 3),
                "emission_tao":      round(em, 6),
                "emission_usd":      round(em * tao_usd, 4) if tao_usd > 0 else None,
            })
    rows.sort(key=lambda x: x["active_validators"], reverse=True)
    return {
        "intent":           "validator_trends",
        "question_type":    "VALIDATOR_TRENDS",
        "fields_used":      ["active_validators", "active_miners", "net_flow_1d", "emission"],
        "computation":      "Sorted by active_validators DESC. Flow from TaoStats enrichment.",
        "confidence":       "HIGH",
        "confidence_reason":"Live data from TaoStats API enrichment",
        "results":          rows[:top_n],
        "result_count":     len(rows[:top_n]),
        "tao_usd":          tao_usd,
    }
