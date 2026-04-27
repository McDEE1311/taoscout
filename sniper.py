"""
TaoScout Sniper Mode — v1.0
Best subnet entry opportunity right now.
Inputs: gpu_class, risk_tolerance, budget_tao
Output: ranked entry recommendations with reasoning
Pure deterministic Python. No LLM needed.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import db as DB
import analytics as AN
from scout import get_chain_data, load_enrichment, MY_SUBNETS

RISK_PROFILES = {
    "low":    {"min_fill": 0,   "max_fill": 60,  "min_ratio": 5,  "max_burn": 0.01,  "label": "Low Risk"},
    "medium": {"min_fill": 0,   "max_fill": 85,  "min_ratio": 3,  "max_burn": 0.05,  "label": "Medium Risk"},
    "high":   {"min_fill": 0,   "max_fill": 100, "min_ratio": 1,  "max_burn": 10.0,  "label": "High Risk"},
}

ENTRY_SIGNALS = {
    "STRONG_BUY":  "Strong entry — high emission, low competition, good ratio",
    "BUY":         "Good entry — solid metrics, manageable burn",
    "WATCH":       "Worth watching — one metric weak, check again in 6h",
    "AVOID":       "Avoid — burn too high or emission too low for risk profile",
    "FULL":        "Full subnet — no open slots",
}

def sniper_score(subnet, tao_usd, movers, gpu_class, risk_profile):
    """
    Score a subnet for sniper entry.
    Returns score 0-100 and signal.
    """
    em     = float(subnet.get("emission", 0) or 0)
    burn   = float(subnet.get("burn_tao", 0) or 0)
    neurons = int(subnet.get("neurons", 0) or 0)
    max_n  = int(subnet.get("max_neurons", 256) or 256)
    nid    = subnet.get("netuid")
    fill   = round(100 * neurons / max(max_n, 1), 1)
    ratio  = round(em / burn if burn > 0 else 0, 1)
    open_slots = max_n - neurons
    usd    = round(em * tao_usd, 3)

    profile = RISK_PROFILES.get(risk_profile, RISK_PROFILES["medium"])

    # Hard filters
    if em == 0:
        return 0, "AVOID", "Zero emission"
    if burn > profile["max_burn"]:
        return 0, "AVOID", f"Burn {burn}τ exceeds {risk_profile} limit {profile['max_burn']}τ"
    if ratio < profile["min_ratio"]:
        return 0, "AVOID", f"Ratio {ratio}x below {risk_profile} minimum {profile['min_ratio']}x"
    if open_slots == 0:
        return 0, "FULL", "No open slots"

    # GPU fit
    try:
        fit_raw = AN.gpu_fit_score(nid, AN.GPU_CLASSES.get(gpu_class, {}).get("vram_gb", 24))
        fit = fit_raw if isinstance(fit_raw, int) else fit_raw.get("score", 50) if isinstance(fit_raw, dict) else 50
    except Exception:
        fit = 50

    if fit < 50:
        return 0, "AVOID", f"Poor GPU fit ({fit}/100) for {gpu_class}"

    # Momentum from movers
    mv = next((m for m in movers if m.get("netuid") == nid), {})
    chg = float(mv.get("em_pct_change", 0) or 0)

    # Scoring
    score = 0

    # Emission value (30pts)
    if usd >= 5.0:   score += 30
    elif usd >= 2.0: score += 20
    elif usd >= 0.5: score += 10
    else:            score += 5

    # Burn efficiency (25pts)
    if ratio >= 50:   score += 25
    elif ratio >= 20: score += 20
    elif ratio >= 10: score += 15
    elif ratio >= 5:  score += 10
    else:             score += 5

    # Open slots / competition (25pts)
    slot_pct = round(100 * open_slots / max(max_n, 1), 1)
    if slot_pct >= 50:   score += 25
    elif slot_pct >= 25: score += 20
    elif slot_pct >= 10: score += 12
    elif slot_pct >= 5:  score += 6
    else:                score += 2

    # Momentum (10pts)
    if chg >= 20:    score += 10
    elif chg >= 0:   score += 5
    elif chg >= -15: score += 2
    else:            score += 0

    # GPU fit bonus (10pts)
    score += int(fit / 10)

    # Signal
    if score >= 75:   signal = "STRONG_BUY"
    elif score >= 55: signal = "BUY"
    elif score >= 35: signal = "WATCH"
    else:             signal = "AVOID"

    return min(score, 100), signal, None


def build_sniper(gpu_class="24gb", risk="medium", budget_tao=0.1, top_n=10):
    """
    Scan all subnets and return best entry opportunities.
    """
    d, src = get_chain_data()
    if not d:
        return {"error": "No chain data available"}

    enrich    = load_enrichment()
    tao_usd   = enrich.get("tao_price_usd", 0.0)
    subnets   = d.get("subnets", [])
    block     = d.get("block", "?")
    snap_count = DB.get_snapshot_count()
    movers    = DB.get_movers(hours=24, top_n=200) if snap_count >= 2 else []

    # Validate gpu_class
    from analytics import GPU_CLASSES
    if gpu_class not in GPU_CLASSES:
        gpu_class = "24gb"
    gpu_info = GPU_CLASSES[gpu_class]

    # Validate risk
    if risk not in RISK_PROFILES:
        risk = "medium"
    profile = RISK_PROFILES[risk]

    results = []
    avoided = {"zero_emission": 0, "burn_too_high": 0,
               "ratio_too_low": 0, "full": 0, "poor_fit": 0}

    for s in subnets:
        nid   = s.get("netuid")
        name  = s.get("name", f"SN{nid}")
        em    = float(s.get("emission", 0) or 0)
        burn  = float(s.get("burn_tao", 0) or 0)
        neurons = int(s.get("neurons", 0) or 0)
        max_n = int(s.get("max_neurons", 256) or 256)
        fill  = round(100 * neurons / max(max_n, 1), 1)
        ratio = round(em / burn if burn > 0 else 0, 1)
        open_slots = max_n - neurons
        usd   = round(em * tao_usd, 3)
        is_mine = nid in MY_SUBNETS

        # Check budget
        if budget_tao > 0 and burn > budget_tao:
            avoided["burn_too_high"] += 1
            continue

        score, signal, reason = sniper_score(s, tao_usd, movers, gpu_class, risk)

        if signal in ("AVOID", "FULL"):
            if signal == "FULL":
                avoided["full"] += 1
            elif em == 0:
                avoided["zero_emission"] += 1
            elif burn > profile["max_burn"]:
                avoided["burn_too_high"] += 1
            elif ratio < profile["min_ratio"]:
                avoided["ratio_too_low"] += 1
            else:
                avoided["poor_fit"] += 1
            continue

        mv = next((m for m in movers if m.get("netuid") == nid), {})
        chg = float(mv.get("em_pct_change", 0) or 0)

        results.append({
            "rank":         0,
            "netuid":       nid,
            "name":         name,
            "signal":       signal,
            "score":        score,
            "is_mine":      is_mine,
            "emission_tao": round(em, 5),
            "emission_usd": usd,
            "burn_tao":     round(burn, 4),
            "ratio":        ratio,
            "fill_pct":     fill,
            "open_slots":   open_slots,
            "momentum_24h": round(chg, 1),
            "signal_text":  ENTRY_SIGNALS.get(signal, ""),
        })

    # Sort by score desc
    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results[:top_n], 1):
        r["rank"] = i

    # Top pick reasoning
    top = results[0] if results else None
    top_pick = None
    if top:
        reasons = []
        if top["emission_usd"] >= 5:
            reasons.append(f"high emission ${top['emission_usd']}/tempo")
        if top["ratio"] >= 20:
            reasons.append(f"excellent ratio {top['ratio']}x")
        if top["open_slots"] > 10:
            reasons.append(f"{top['open_slots']} open slots")
        if top["momentum_24h"] > 10:
            reasons.append(f"momentum +{top['momentum_24h']}%")
        top_pick = {
            "netuid":    top["netuid"],
            "name":      top["name"],
            "signal":    top["signal"],
            "score":     top["score"],
            "reasoning": ", ".join(reasons) if reasons else "Best available by combined score",
            "entry_cost_tao": top["burn_tao"],
            "entry_cost_usd": round(top["burn_tao"] * tao_usd, 2),
            "expected_per_tempo_usd": top["emission_usd"],
        }

    # Build watchlist — full subnets worth monitoring for openings
    watchlist = []
    for s in subnets:
        nid   = s.get("netuid")
        name  = s.get("name", f"SN{nid}")
        em    = float(s.get("emission", 0) or 0)
        burn  = float(s.get("burn_tao", 0) or 0)
        neurons = int(s.get("neurons", 0) or 0)
        max_n = int(s.get("max_neurons", 256) or 256)
        fill  = round(100 * neurons / max(max_n, 1), 1)
        ratio = round(em / burn if burn > 0 else 0, 1)
        open_slots = max_n - neurons
        usd   = round(em * tao_usd, 3)
        # Full but high value — worth watching
        if open_slots == 0 and em > 0 and ratio >= 5 and usd >= 1.0 and burn <= budget_tao:
            mv = next((m for m in movers if m.get("netuid") == nid), {})
            chg = float(mv.get("em_pct_change", 0) or 0)
            watchlist.append({
                "netuid": nid, "name": name,
                "emission_usd": usd, "burn_tao": round(burn,4),
                "ratio": ratio, "fill_pct": fill,
                "momentum_24h": round(chg, 1),
                "note": "Full — watch for deregistrations"
            })
    watchlist.sort(key=lambda x: x["emission_usd"], reverse=True)

    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "block":         block,
        "tao_usd":       tao_usd,
        "gpu_class":     gpu_class,
        "gpu_label":     gpu_info["label"],
        "risk_profile":  risk,
        "budget_tao":    budget_tao,
        "snap_count":    snap_count,
        "subnets_scanned": len(subnets),
        "filtered_out":  avoided,
        "results_count": len(results),
        "top_pick":      top_pick,
        "results":       results[:top_n],
        "watchlist":     watchlist[:10],
        "market_note":   f"Network is {round(100*(len(subnets)-avoided.get('full',0))/max(len(subnets),1),0):.0f}% saturated. {avoided.get('full',0)} subnets full.",
        "disclaimer":    "TaoScout sniper mode. Entry signals are algorithmic estimates. Not financial advice.",
    }


def format_sniper_table(result):
    """Format sniper results as readable table."""
    if "error" in result:
        return f"ERROR: {result['error']}"

    lines = [
        "─" * 72,
        f"  TAOSCOUT SNIPER MODE — {result['gpu_label']} | Risk: {result['risk_profile'].upper()}",
        f"  Block {result['block']} | TAO ${result['tao_usd']:.2f} | Budget: {result['budget_tao']}τ",
        "─" * 72,
    ]

    tp = result.get("top_pick")
    if tp:
        lines += [
            f"  🎯 TOP PICK: SN{tp['netuid']} {tp['name']} — {tp['signal']} ({tp['score']}/100)",
            f"     Entry cost: {tp['entry_cost_tao']}τ (${tp['entry_cost_usd']})",
            f"     Expected:   ${tp['expected_per_tempo_usd']}/tempo",
            f"     Reason:     {tp['reasoning']}",
            "─" * 72,
        ]

    signals = {"STRONG_BUY": "🟢", "BUY": "🔵", "WATCH": "🟡"}
    lines.append(
        f"  {'#':3}  {'Subnet':8}  {'Name':16}  {'Sig':11}  "
        f"{'Score':5}  {'Emit$':7}  {'Burn':8}  {'Ratio':6}  {'Open':5}  {'Mom%':6}"
    )
    lines.append("  " + "─" * 68)

    for r in result.get("results", []):
        icon = signals.get(r["signal"], "⚪")
        mine = "★" if r["is_mine"] else " "
        lines.append(
            f"  {r['rank']:3}  SN{r['netuid']:<6}  {r['name'][:16]:16}  "
            f"{icon}{r['signal']:10}  {r['score']:5}  "
            f"${r['emission_usd']:6.2f}  {r['burn_tao']:8.4f}  "
            f"{r['ratio']:6.1f}x  {r['open_slots']:5}  "
            f"{r['momentum_24h']:+6.1f}% {mine}"
        )

    # Watchlist section
    watchlist = result.get("watchlist", [])
    if watchlist:
        lines += ["─" * 72, "  👀 WATCHLIST — Full subnets worth monitoring for openings:", ""]
        for w in watchlist[:5]:
            lines.append(
                f"     SN{w['netuid']:<5} {w['name'][:16]:16}  "
                f"${w['emission_usd']:6.3f}/tempo  ratio {w['ratio']}x  {w['momentum_24h']:+.1f}%"
            )
        lines.append("")

    f = result.get("filtered_out", {})
    note = result.get("market_note", "")
    lines += [
        "─" * 72,
        f"  {note}",
        f"  Filtered: {f.get('zero_emission',0)} zero-emit | "
        f"{f.get('full',0)} full | "
        f"{f.get('burn_too_high',0)} over budget | "
        f"{f.get('ratio_too_low',0)} low ratio",
        "  ★ = your subnet  |  Not financial advice",
    ]
    return "\n".join(lines)
