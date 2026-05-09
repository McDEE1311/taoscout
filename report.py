#!/usr/bin/env python3
"""
TaoScout - Deterministic Report Builder v1.0
All sections built in Python. LLM only formats plain-English summaries.
No LLM ranking. No LLM ordering. No unsupported claims.
"""
from datetime import datetime, timezone
from pathlib import Path

# ── Claim guards ──────────────────────────────────────────────────────────────
# These are the ONLY conditions under which categorical labels are allowed.
# If condition not met, label is replaced with neutral description.

BANNED_CLAIMS = ["deprecated", "ideal fit", "best", "strong performance",
                 "top performer", "excellent", "outstanding"]

CONFIDENCE_THRESHOLDS = {
    "HIGH":   {"min_snapshots": 1,  "requires": "deterministic_metric"},
    "MEDIUM": {"min_snapshots": 2,  "requires": "partial_data"},
    "LOW":    {"min_snapshots": 4,  "requires": "trend_data"},
    "INSUFFICIENT": {"min_snapshots": 8, "requires": "historical_trend"},
}

def safe_label(label, condition_met, fallback="UNVERIFIED"):
    """Only return label if condition is met. Never guess."""
    if not condition_met:
        return fallback
    for banned in BANNED_CLAIMS:
        if banned.lower() in label.lower():
            return fallback
    return label

def section_confidence(data_type, snapshot_count):
    """Per-section confidence based on data type and snapshot count."""
    gates = {
        "current_price":    ("HIGH",         1),
        "current_emission": ("HIGH",         1),
        "current_ranking":  ("HIGH",         1),
        "gpu_fit_known":    ("HIGH",         1),
        "gpu_fit_unknown":  ("LOW",          1),
        "24h_delta":        ("MEDIUM",       4),
        "7d_trend":         ("LOW",         24),
        "validator_trend":  ("INSUFFICIENT", 8),
        "movers":           ("MEDIUM",       4),
    }
    label, required = gates.get(data_type, ("LOW", 99))
    if snapshot_count < required:
        return "INSUFFICIENT"
    return label

# ── Section builders (all deterministic) ─────────────────────────────────────

def build_global_emissions(subnets, tao_usd, top_n=10):
    """
    Top N subnets by emission. Sorted numerically in Python.
    Returns exactly top_n items or fewer if not enough have emission > 0.
    Section title reflects actual count returned.
    """
    def em(s): return float(s.get("emission", 0) or 0)

    ranked = sorted(
        [s for s in subnets if em(s) > 0],
        key=em, reverse=True
    )[:top_n]

    actual_n = len(ranked)
    rows = []
    for i, s in enumerate(ranked, 1):
        emission = em(s)
        burn     = float(s.get("burn_tao", 0) or 0)
        neurons  = int(s.get("neurons", 0) or 0)
        max_n    = int(s.get("max_neurons", 256) or 256)
        fill     = round(neurons / max_n * 100, 1) if max_n > 0 else 0
        usd      = round(emission * tao_usd, 4) if tao_usd > 0 else None
        rows.append({
            "rank":         i,
            "netuid":       s["netuid"],
            "name":         s.get("name", "Unknown"),
            "emission_tao": round(emission, 6),
            "emission_usd": usd,
            "burn_tao":     round(burn, 4),
            "fill_pct":     fill,
            "neurons":      neurons,
            "max_neurons":  max_n,
        })

    return {
        "section":      f"TOP {actual_n} SUBNETS BY EMISSION",
        "actual_count": actual_n,
        "requested":    top_n,
        "confidence":   "HIGH",
        "basis":        "Sorted by tao_in_emission field from live chain snapshot. Python sort.",
        "rows":         rows,
    }

def build_gpu_rankings(subnets, gpu_class, tao_usd, movers=None, top_n=10):
    """
    GPU-specific rankings. All math in Python via analytics module.
    Returns exactly top_n viable results or fewer.
    """
    try:
        import analytics as AN
        from analytics import GPU_CLASSES
        if gpu_class not in GPU_CLASSES:
            return {"error": f"Unknown GPU class: {gpu_class}"}

        gpu_info = GPU_CLASSES[gpu_class]
        ranked   = AN.rank_subnets_for_gpu_class(
            subnets, gpu_class, tao_usd, movers, top_n=top_n
        )
        actual_n = len(ranked)

        rows = []
        for i, r in enumerate(ranked, 1):
            m = r.get("metrics", {})
            rows.append({
                "rank":             i,
                "netuid":           r["netuid"],
                "name":             r["name"],
                "combined_score":   r["combined_score"],
                "opportunity_score": r["opportunity_score"],
                "fit_score":        r["fit_score"],
                "workload":         r["workload"],
                "hw_notes":         r["hw_notes"],
                "emission_tao":     m.get("emission", 0),
                "emission_usd":     m.get("emission_usd"),
                "burn_tao":         m.get("burn_tao", 0),
                "fill_pct":         m.get("fill_pct", 0),
                "ratio":            m.get("ratio", 0),
                "score_breakdown":  r.get("breakdown", {}),
            })

        return {
            "section":      f"TOP {actual_n} FOR {gpu_info['label']} ({', '.join(gpu_info['cards'])})",
            "gpu_class":    gpu_class,
            "gpu_label":    gpu_info["label"],
            "cards":        gpu_info["cards"],
            "actual_count": actual_n,
            "confidence":   "HIGH",
            "basis":        "Combined score = opportunity_score x (fit_score/100). Python computation.",
            "rows":         rows,
        }
    except ImportError:
        return {"error": "Analytics module not available"}

def build_emission_burn_ratios(subnets, tao_usd, top_n=10):
    """
    Global emission/burn ratio ranking. Sorted numerically in Python.
    Only includes subnets with emission > 0 AND burn > 0.
    Does NOT mix with my_subnets — this is global only.
    """
    def ratio(s):
        em = float(s.get("emission", 0) or 0)
        b  = float(s.get("burn_tao", 0) or 0)
        return em / b if b > 0 and em > 0 else 0.0

    ranked = sorted(
        [s for s in subnets if ratio(s) > 0],
        key=ratio, reverse=True
    )[:top_n]

    actual_n = len(ranked)
    rows = []
    for i, s in enumerate(ranked, 1):
        em  = float(s.get("emission", 0) or 0)
        b   = float(s.get("burn_tao", 0) or 0)
        r   = ratio(s)
        usd = round(em * tao_usd, 4) if tao_usd > 0 else None
        rows.append({
            "rank":         i,
            "netuid":       s["netuid"],
            "name":         s.get("name", "Unknown"),
            "ratio":        round(r, 2),
            "emission_tao": round(em, 6),
            "emission_usd": usd,
            "burn_tao":     round(b, 4),
        })

    return {
        "section":      f"TOP {actual_n} BY EMISSION/BURN RATIO (GLOBAL)",
        "actual_count": actual_n,
        "confidence":   "HIGH",
        "basis":        "ratio = emission / burn_tao. Python sort. Global subnets only.",
        "rows":         rows,
    }

def build_my_subnets(subnets, my_netuids, tao_usd, snapshot_count, movers=None):
    """
    Tracked subnets section. Completely separate from global rankings.
    Shows raw metrics only — no unsupported labels.
    """
    my = [s for s in subnets if s.get("netuid") in my_netuids]
    mover_map = {m["netuid"]: m for m in (movers or [])}

    rows = []
    for s in my:
        nid     = s["netuid"]
        em      = float(s.get("emission", 0) or 0)
        b       = float(s.get("burn_tao", 0) or 0)
        r       = round(em / b, 2) if b > 0 and em > 0 else 0.0
        neurons = int(s.get("neurons", 0) or 0)
        max_n   = int(s.get("max_neurons", 256) or 256)
        fill    = round(neurons / max_n * 100, 1) if max_n > 0 else 0
        usd     = round(em * tao_usd, 4) if tao_usd > 0 else None

        # Mover data if available
        delta = None
        if nid in mover_map:
            mv = mover_map[nid]
            delta = {
                "em_delta":      mv.get("em_delta", 0),
                "em_pct_change": round(mv.get("em_pct_change", 0), 2),
                "fill_delta":    round(mv.get("fill_delta", 0), 2),
            }

        # Emission status — only factual
        if em == 0:
            status = "ZERO EMISSION"
        elif em < 0.001:
            status = "LOW EMISSION"
        elif em < 0.01:
            status = "MODERATE EMISSION"
        else:
            status = "HIGH EMISSION"

        rows.append({
            "netuid":       nid,
            "name":         s.get("name", "Unknown"),
            "emission_tao": round(em, 6),
            "emission_usd": usd,
            "burn_tao":     round(b, 4),
            "ratio":        r,
            "fill_pct":     fill,
            "neurons":      neurons,
            "max_neurons":  max_n,
            "tempo":        s.get("tempo", 360),
            "price":        round(float(s.get("price", 0) or 0), 6),
            "emission_status": status,
            "delta_24h":    delta,
        })

    conf = section_confidence("24h_delta", snapshot_count) if movers else "HIGH"

    return {
        "section":    "MY TRACKED SUBNETS",
        "confidence": conf,
        "basis":      "Direct chain metrics. Delta from SQLite snapshot comparison.",
        "rows":       rows,
        "note":       "This section is separate from global rankings.",
    }

def build_movers(movers, snapshot_count, tao_usd=0):
    """
    Top emission movers. Only shown if sufficient snapshot data exists.
    If insufficient data — returns unavailable section, not filler.
    """
    conf = section_confidence("movers", snapshot_count)

    if conf == "INSUFFICIENT" or not movers:
        return {
            "section":      "TOP MOVERS",
            "available":    False,
            "confidence":   "INSUFFICIENT",
            "basis":        f"Need 4+ snapshots for reliable mover data. Have {snapshot_count}.",
            "rows":         [],
            "note":         "Section omitted — insufficient history. Check back after more snapshots accumulate.",
        }

    # Sort by absolute emission change
    ranked = sorted(movers, key=lambda m: abs(m.get("em_delta", 0)), reverse=True)[:10]
    actual_n = len(ranked)

    rows = []
    for i, m in enumerate(ranked, 1):
        em_delta  = m.get("em_delta", 0)
        em_pct    = round(m.get("em_pct_change", 0), 2)
        direction = "UP" if em_delta > 0 else "DOWN"
        rows.append({
            "rank":          i,
            "netuid":        m["netuid"],
            "name":          m.get("name", "Unknown"),
            "direction":     direction,
            "em_delta":      round(em_delta, 6),
            "em_pct_change": em_pct,
            "curr_emission": round(m.get("curr_emission", 0), 6),
            "prev_emission": round(m.get("prev_emission", 0), 6),
            "fill_delta":    round(m.get("fill_delta", 0), 2),
        })

    return {
        "section":      f"TOP {actual_n} EMISSION MOVERS (24H)",
        "available":    True,
        "actual_count": actual_n,
        "confidence":   conf,
        "basis":        "Computed from SQLite snapshot comparison. Python sort by |em_delta|.",
        "rows":         rows,
    }

def build_risks(subnets, my_netuids, snapshot_count, dereg_risk=None):
    """
    Factual risk flags only. No inferred risks. No unsupported claims.
    """
    risks = []

    # Zero emission subnets in my tracked list
    my = [s for s in subnets if s.get("netuid") in my_netuids]
    for s in my:
        em = float(s.get("emission", 0) or 0)
        if em == 0:
            risks.append({
                "netuid":   s["netuid"],
                "name":     s.get("name", "Unknown"),
                "flag":     "ZERO EMISSION",
                "metric":   f"emission={em}",
                "severity": "HIGH",
                "source":   "live chain data",
            })

    # High fill tracked subnets (hard to register)
    for s in my:
        neurons = int(s.get("neurons", 0) or 0)
        max_n   = int(s.get("max_neurons", 256) or 256)
        fill    = neurons / max_n * 100 if max_n > 0 else 0
        if fill >= 100:
            risks.append({
                "netuid":   s["netuid"],
                "name":     s.get("name", "Unknown"),
                "flag":     "SUBNET AT CAPACITY",
                "metric":   f"fill={fill:.0f}% ({neurons}/{max_n})",
                "severity": "MEDIUM",
                "source":   "live chain data",
            })

    # Deregistration risk from TaoStats
    if dereg_risk and isinstance(dereg_risk, list):
        risk_netuids = {int(r.get("netuid", r.get("net_uid", -1))): r for r in dereg_risk}
        for nid in my_netuids:
            if nid in risk_netuids:
                r = risk_netuids[nid]
                risks.append({
                    "netuid":   nid,
                    "flag":     "DEREGISTRATION RISK",
                    "metric":   f"rank={r.get('rank','?')}",
                    "severity": "HIGH",
                    "source":   "TaoStats deregistration-ranking API",
                })

    # Insufficient history warning
    if snapshot_count < 4:
        risks.append({
            "flag":     "LIMITED HISTORY",
            "metric":   f"snapshot_count={snapshot_count} (need 4+ for trend analysis)",
            "severity": "LOW",
            "source":   "local DB",
        })

    return {
        "section":    "RISK FLAGS",
        "confidence": "HIGH",
        "basis":      "Factual flags only. No inferred risks. Source cited per flag.",
        "rows":       risks,
        "note":       "Only includes flags backed by live data or TaoStats API.",
    }

def build_recommendations(global_emissions, gpu_rankings, my_subnets_section,
                           movers_section, snapshot_count):
    """
    Recommendations tied to exact metrics. No hand-waving.
    Each recommendation cites the specific metric that triggered it.
    """
    recs = []

    # Rec 1: Best global opportunity for 24GB GPU
    gpu_rows = gpu_rankings.get("rows", [])
    if gpu_rows:
        top = gpu_rows[0]
        recs.append({
            "priority": 1,
            "action":   f"Evaluate SN{top['netuid']} {top['name']} for 24GB GPU deployment",
            "basis":    f"Highest combined score for 24GB class: {top['combined_score']}/100 "
                        f"(opportunity={top['opportunity_score']} fit={top['fit_score']}) "
                        f"emit={top['emission_tao']}t/tempo"
                        + (f" (${top['emission_usd']:.3f})" if top.get('emission_usd') else ""),
            "confidence": "HIGH",
            "metric_source": "analytics.rank_subnets_for_gpu_class()",
        })

    # Rec 2: Best emission/burn ratio globally
    em_rows = global_emissions.get("rows", [])
    if em_rows:
        # Find highest ratio from top emissions
        best_ratio = max(em_rows, key=lambda r: (
            r["emission_tao"] / r["burn_tao"] if r["burn_tao"] > 0 else 0
        ))
        ratio_val = round(best_ratio["emission_tao"] / best_ratio["burn_tao"], 1) if best_ratio["burn_tao"] > 0 else 0
        recs.append({
            "priority": 2,
            "action":   f"Monitor SN{best_ratio['netuid']} {best_ratio['name']} for registration timing",
            "basis":    f"Highest emission/burn ratio in top 10: {ratio_val}x "
                        f"(emit={best_ratio['emission_tao']}t burn={best_ratio['burn_tao']}t "
                        f"fill={best_ratio['fill_pct']}%)",
            "confidence": "HIGH",
            "metric_source": "live chain snapshot",
        })

    # Rec 3: Mover-based if available
    mover_rows = movers_section.get("rows", [])
    if mover_rows and movers_section.get("available"):
        # Skip movers where prev was 0 (new subnets, not real movers)
        real_movers = [m for m in mover_rows if m.get("prev_emission", 0) > 0]
        top_mover = real_movers[0] if real_movers else None
    if mover_rows and movers_section.get("available") and top_mover:
        direction = top_mover["direction"]
        recs.append({
            "priority": 3,
            "action":   f"Investigate SN{top_mover['netuid']} {top_mover['name']} — emission {direction} {top_mover['em_pct_change']}% in 24h",
            "basis":    f"Largest 24h emission delta: {top_mover['em_delta']:+.5f}t "
                        f"({top_mover['em_pct_change']:+.1f}%) "
                        f"curr={top_mover['curr_emission']}t prev={top_mover['prev_emission']}t",
            "confidence": "MEDIUM",
            "metric_source": "SQLite snapshot delta",
        })
    elif snapshot_count < 4:
        recs.append({
            "priority": 3,
            "action":   "Allow 2+ hours for snapshot history to accumulate for mover analysis",
            "basis":    f"Current snapshot count: {snapshot_count}. Need 4+ for reliable 24h deltas.",
            "confidence": "HIGH",
            "metric_source": "local DB",
        })

    return {
        "section":    "ACTIONABLE RECOMMENDATIONS",
        "confidence": "HIGH",
        "basis":      "Each recommendation tied to exact computed metric. No unsupported claims.",
        "rows":       recs,
    }

# ── Master report builder ─────────────────────────────────────────────────────

def build_full_report(chain_data, enrichment, my_netuids, snapshot_count, movers=None):
    """
    Build complete deterministic report.
    Returns structured dict — LLM only writes summaries, not data.
    """
    subnets   = chain_data.get("subnets", [])
    block     = chain_data.get("block", "?")
    tao_usd   = enrichment.get("tao_price_usd", 0.0)
    dereg     = enrichment.get("dereg_risk", [])

    # Build all sections deterministically
    global_em   = build_global_emissions(subnets, tao_usd, top_n=10)
    gpu_24      = build_gpu_rankings(subnets, "24gb", tao_usd, movers, top_n=10)
    gpu_32      = build_gpu_rankings(subnets, "32gb", tao_usd, movers, top_n=5)
    gpu_48      = build_gpu_rankings(subnets, "48gb", tao_usd, movers, top_n=5)
    gpu_96      = build_gpu_rankings(subnets, "96gb", tao_usd, movers, top_n=5)
    em_ratios   = build_emission_burn_ratios(subnets, tao_usd, top_n=10)
    my_subs     = build_my_subnets(subnets, my_netuids, tao_usd, snapshot_count, movers)
    movers_sec  = build_movers(movers or [], snapshot_count, tao_usd)
    risks       = build_risks(subnets, my_netuids, snapshot_count, dereg)
    recs        = build_recommendations(global_em, gpu_24, my_subs, movers_sec, snapshot_count)

    return {
        "metadata": {
            "generated_at":   datetime.now(timezone.utc).isoformat(),
            "block":          block,
            "tao_usd":        tao_usd,
            "snapshot_count": snapshot_count,
            "subnets_total":  len(subnets),
            "version":        "1.0.0",
        },
        "sections": {
            "global_emissions":    global_em,
            "gpu_24gb":            gpu_24,
            "gpu_32gb":            gpu_32,
            "gpu_48gb":            gpu_48,
            "gpu_96gb":            gpu_96,
            "emission_burn_ratio": em_ratios,
            "my_subnets":          my_subs,
            "movers":              movers_sec,
            "risks":               risks,
            "recommendations":     recs,
        },
    }

def format_report_for_terminal(report):
    """
    Format report dict for clean terminal display.
    Deterministic formatting — no LLM involved.
    """
    lines = []
    meta  = report["metadata"]
    secs  = report["sections"]

    lines.append(f"\n{'='*64}")
    lines.append(f"  TAOSCOUT OPERATOR REPORT")
    lines.append(f"  Block: {meta['block']}  |  TAO: ${meta['tao_usd']:.2f}  |  Snapshots: {meta['snapshot_count']}")
    lines.append(f"  Generated: {meta['generated_at'][:19]} UTC")
    lines.append(f"{'='*64}")

    def section_header(s, conf):
        return f"\n  [{conf}] {s}\n  {'─'*54}"

    def fmt_row(r, fields):
        parts = []
        for f, label in fields:
            v = r.get(f)
            if v is not None:
                if isinstance(v, float):
                    parts.append(f"{label}={v:.4f}")
                else:
                    parts.append(f"{label}={v}")
        return "  " + "  ".join(parts)

    # Global emissions
    ge = secs["global_emissions"]
    lines.append(section_header(ge["section"], ge["confidence"]))
    for r in ge["rows"]:
        usd = f" (${r['emission_usd']:.3f})" if r.get("emission_usd") else ""
        lines.append(f"  {r['rank']:2}. SN{r['netuid']:3} {r['name'][:18]:<18} "
                     f"emit={r['emission_tao']:.5f}t{usd}  "
                     f"burn={r['burn_tao']:.4f}t  "
                     f"fill={r['fill_pct']:.0f}%")
    lines.append(f"  Basis: {ge['basis'][:70]}")

    # GPU 24GB
    g24 = secs["gpu_24gb"]
    if "rows" in g24:
        lines.append(section_header(g24["section"], g24["confidence"]))
        for r in g24["rows"]:
            usd = f" (${r['emission_usd']:.3f})" if r.get("emission_usd") else ""
            lines.append(f"  {r['rank']:2}. SN{r['netuid']:3} {r['name'][:18]:<18} "
                         f"score={r['combined_score']:3}/100  "
                         f"emit={r['emission_tao']:.5f}t{usd}  "
                         f"fill={r['fill_pct']:.0f}%  "
                         f"wl={r['workload']}")

    # Emission/burn ratios
    eb = secs["emission_burn_ratio"]
    lines.append(section_header(eb["section"], eb["confidence"]))
    for r in eb["rows"]:
        usd = f" (${r['emission_usd']:.3f})" if r.get("emission_usd") else ""
        lines.append(f"  {r['rank']:2}. SN{r['netuid']:3} {r['name'][:18]:<18} "
                     f"ratio={r['ratio']:6.1f}x  "
                     f"emit={r['emission_tao']:.5f}t{usd}  "
                     f"burn={r['burn_tao']:.4f}t")

    # My subnets
    ms = secs["my_subnets"]
    lines.append(section_header(ms["section"], ms["confidence"]))
    for r in ms["rows"]:
        usd = f" (${r['emission_usd']:.4f})" if r.get("emission_usd") else ""
        delta_str = ""
        if r.get("delta_24h"):
            d = r["delta_24h"]
            delta_str = f"  24h:{d['em_pct_change']:+.1f}%"
        lines.append(f"  SN{r['netuid']:3} {r['name'][:18]:<18} "
                     f"emit={r['emission_tao']:.5f}t{usd}  "
                     f"ratio={r['ratio']:.1f}  "
                     f"fill={r['fill_pct']:.0f}%  "
                     f"[{r['emission_status']}]{delta_str}")

    # Movers
    mv = secs["movers"]
    if mv.get("available") and mv["rows"]:
        lines.append(section_header(mv["section"], mv["confidence"]))
        for r in mv["rows"]:
            lines.append(f"  {r['rank']:2}. SN{r['netuid']:3} {r['name'][:18]:<18} "
                         f"{r['direction']:4}  {r['em_pct_change']:+.1f}%  "
                         f"curr={r['curr_emission']:.5f}t  "
                         f"prev={r['prev_emission']:.5f}t")
    else:
        lines.append(section_header("MOVERS", "INSUFFICIENT"))
        lines.append(f"  {mv.get('note', 'Insufficient data')}")

    # Risks
    rk = secs["risks"]
    lines.append(section_header(rk["section"], rk["confidence"]))
    if rk["rows"]:
        for r in rk["rows"]:
            sn = f"SN{r['netuid']} " if r.get("netuid") else ""
            lines.append(f"  [{r['severity']}] {sn}{r['flag']}  {r.get('metric','')}  src:{r.get('source','')}")
    else:
        lines.append("  No risk flags detected in current data.")

    # Recommendations
    rc = secs["recommendations"]
    lines.append(section_header(rc["section"], rc["confidence"]))
    for r in rc["rows"]:
        lines.append(f"\n  {r['priority']}. {r['action']}")
        lines.append(f"     Basis: {r['basis'][:90]}")
        lines.append(f"     Confidence: {r['confidence']}  |  Source: {r['metric_source']}")

    lines.append(f"\n{'='*64}")
    lines.append("  DISCLAIMER: TaoScout is automated. Informational only.")
    lines.append("  Not financial advice. Verify independently.")
    lines.append(f"{'='*64}\n")

    return "\n".join(lines)

def format_report_context_for_llm(report):
    """
    Format report as LLM context.
    LLM receives pre-ranked data and writes plain-English summaries only.
    It cannot reorder, recalculate, or add unsupported claims.
    """
    meta = report["metadata"]
    secs = report["sections"]
    lines = []

    lines.append("PRE-COMPUTED REPORT DATA (do not reorder or recalculate):")
    lines.append(f"Block: {meta['block']} | TAO/USD: ${meta['tao_usd']:.2f} | Snapshots: {meta['snapshot_count']}")
    lines.append("")

    ge = secs["global_emissions"]
    lines.append(f"=== {ge['section']} [confidence={ge['confidence']}] ===")
    for r in ge["rows"]:
        usd = f" (${r['emission_usd']:.3f})" if r.get("emission_usd") else ""
        lines.append(f"  {r['rank']}. SN{r['netuid']} {r['name']}: "
                     f"emit={r['emission_tao']:.5f}t{usd} burn={r['burn_tao']:.4f}t fill={r['fill_pct']:.0f}%")

    g24 = secs["gpu_24gb"]
    if "rows" in g24:
        lines.append(f"\n=== {g24['section']} [confidence={g24['confidence']}] ===")
        for r in g24["rows"]:
            usd = f" (${r['emission_usd']:.3f})" if r.get("emission_usd") else ""
            lines.append(f"  {r['rank']}. SN{r['netuid']} {r['name']}: "
                         f"score={r['combined_score']}/100 emit={r['emission_tao']:.5f}t{usd} "
                         f"fill={r['fill_pct']:.0f}% workload={r['workload']}")

    eb = secs["emission_burn_ratio"]
    lines.append(f"\n=== {eb['section']} [confidence={eb['confidence']}] ===")
    for r in eb["rows"]:
        lines.append(f"  {r['rank']}. SN{r['netuid']} {r['name']}: "
                     f"ratio={r['ratio']}x emit={r['emission_tao']:.5f}t burn={r['burn_tao']:.4f}t")

    ms = secs["my_subnets"]
    lines.append(f"\n=== {ms['section']} [confidence={ms['confidence']}] ===")
    for r in ms["rows"]:
        usd = f" (${r['emission_usd']:.4f})" if r.get("emission_usd") else ""
        d = r.get("delta_24h")
        delta = f" 24h:{d['em_pct_change']:+.1f}%" if d else ""
        lines.append(f"  SN{r['netuid']} {r['name']}: "
                     f"emit={r['emission_tao']:.5f}t{usd} ratio={r['ratio']} "
                     f"fill={r['fill_pct']:.0f}% status={r['emission_status']}{delta}")

    mv = secs["movers"]
    if mv.get("available") and mv["rows"]:
        lines.append(f"\n=== {mv['section']} [confidence={mv['confidence']}] ===")
        for r in mv["rows"]:
            lines.append(f"  {r['rank']}. SN{r['netuid']} {r['name']}: "
                         f"{r['direction']} {r['em_pct_change']:+.1f}% "
                         f"curr={r['curr_emission']:.5f}t prev={r['prev_emission']:.5f}t")
    else:
        lines.append(f"\n=== MOVERS: {mv.get('note', 'Unavailable')} ===")

    rk = secs["risks"]
    lines.append(f"\n=== {rk['section']} [confidence={rk['confidence']}] ===")
    for r in rk["rows"]:
        sn = f"SN{r['netuid']} " if r.get("netuid") else ""
        lines.append(f"  [{r['severity']}] {sn}{r['flag']}: {r.get('metric','')} ({r.get('source','')})")

    rc = secs["recommendations"]
    lines.append(f"\n=== {rc['section']} [confidence={rc['confidence']}] ===")
    for r in rc["rows"]:
        lines.append(f"  {r['priority']}. {r['action']}")
        lines.append(f"     Basis: {r['basis']}")

    lines.append("\nINSTRUCTION: Summarize each section in plain English. "
                 "Do not reorder items. Do not recalculate scores. "
                 "Do not add claims not present in the data above. "
                 "Use section confidence levels as provided.")

    return "\n".join(lines)
