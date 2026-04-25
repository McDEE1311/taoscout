#!/usr/bin/env python3
"""
TaoScout - Deterministic Analytics Engine
All scoring and ranking done in Python. LLM never touches raw math.

GPU Classes supported:
  24GB: RTX 3090, RTX 4090, RTX 6000 Ada
  32GB: RTX 5090
  48GB: RTX 6000 (Ampere), A6000
  96GB: Pro 6000 Blackwell
"""
from pathlib import Path
import json

SCRIPT_DIR = Path(__file__).parent

# ── GPU Registry ───────────────────────────────────────────────────────────────
GPU_CLASSES = {
    "24gb": {
        "label":   "24GB VRAM",
        "cards":   ["RTX 3090", "RTX 4090", "RTX 6000 Ada"],
        "vram_gb": 24,
        "tier":    "consumer_pro",
    },
    "32gb": {
        "label":   "32GB VRAM",
        "cards":   ["RTX 5090"],
        "vram_gb": 32,
        "tier":    "consumer_flagship",
    },
    "48gb": {
        "label":   "48GB VRAM",
        "cards":   ["RTX 6000 Ampere", "A6000"],
        "vram_gb": 48,
        "tier":    "workstation",
    },
    "96gb": {
        "label":   "96GB VRAM",
        "cards":   ["Pro 6000 Blackwell"],
        "vram_gb": 96,
        "tier":    "workstation_flagship",
    },
}

# ── Subnet Hardware Requirements Registry ────────────────────────────────────
# min_vram_gb = minimum VRAM to run a miner
# ideal_vram_gb = optimal VRAM for competitive performance
# workload = what type of work the subnet does
# notes = operator notes
SUBNET_HW_REGISTRY = {
    1:   {"min_vram_gb": 8,  "ideal_vram_gb": 24, "workload": "text_inference",     "notes": "General LLM inference. 24GB+ competitive."},
    4:   {"min_vram_gb": 16, "ideal_vram_gb": 24, "workload": "multimodal",          "notes": "Targon. Vision+text. 24GB+ viable."},
    8:   {"min_vram_gb": 8,  "ideal_vram_gb": 16, "workload": "time_series",         "notes": "Forecasting. Lower VRAM ok."},
    11:  {"min_vram_gb": 8,  "ideal_vram_gb": 16, "workload": "text_inference",      "notes": "General inference."},
    15:  {"min_vram_gb": 24, "ideal_vram_gb": 48, "workload": "vision",              "notes": "Image gen. 48GB preferred."},
    18:  {"min_vram_gb": 16, "ideal_vram_gb": 24, "workload": "audio",               "notes": "Audio processing. 24GB viable."},
    19:  {"min_vram_gb": 8,  "ideal_vram_gb": 24, "workload": "text_inference",      "notes": "LLM inference. Speed matters."},
    21:  {"min_vram_gb": 8,  "ideal_vram_gb": 16, "workload": "storage",             "notes": "Filecoin-style. CPU/storage primary."},
    22:  {"min_vram_gb": 8,  "ideal_vram_gb": 24, "workload": "text_inference",      "notes": "General inference."},
    23:  {"min_vram_gb": 16, "ideal_vram_gb": 48, "workload": "vision",              "notes": "NicheTensor. Image tasks."},
    25:  {"min_vram_gb": 24, "ideal_vram_gb": 48, "workload": "multimodal",          "notes": "Protein/science. High VRAM beneficial."},
    27:  {"min_vram_gb": 8,  "ideal_vram_gb": 16, "workload": "compute",             "notes": "General compute. Flexible."},
    32:  {"min_vram_gb": 16, "ideal_vram_gb": 24, "workload": "audio_video",         "notes": "Audio/video. 24GB good."},
    34:  {"min_vram_gb": 8,  "ideal_vram_gb": 24, "workload": "text_inference",      "notes": "Inference. 24GB competitive."},
    36:  {"min_vram_gb": 16, "ideal_vram_gb": 24, "workload": "text_inference",      "notes": "Inference tasks."},
    39:  {"min_vram_gb": 8,  "ideal_vram_gb": 24, "workload": "compute_rental",      "notes": "Cathedral. General compute rental."},
    44:  {"min_vram_gb": 8,  "ideal_vram_gb": 24, "workload": "text_inference",      "notes": "Score. Inference focused."},
    47:  {"min_vram_gb": 16, "ideal_vram_gb": 48, "workload": "vision",              "notes": "Vision tasks. 48GB ideal."},
    50:  {"min_vram_gb": 8,  "ideal_vram_gb": 16, "workload": "compute",             "notes": "General compute."},
    51:  {"min_vram_gb": 24, "ideal_vram_gb": 48, "workload": "multimodal",          "notes": "Lium. High perf multimodal."},
    54:  {"min_vram_gb": 16, "ideal_vram_gb": 24, "workload": "identity_mining",     "notes": "MIID. Identity tasks. 24GB good."},
    64:  {"min_vram_gb": 8,  "ideal_vram_gb": 24, "workload": "text_inference",      "notes": "Inference."},
    68:  {"min_vram_gb": 8,  "ideal_vram_gb": 16, "workload": "compute",             "notes": "General."},
}

def get_hw_info(netuid):
    """Load hardware info from subnet_meta.json. Falls back to hardcoded registry."""
    import json
    from pathlib import Path
    meta_file = Path(__file__).parent / "subnet_meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            entry = meta.get(str(netuid))
            if entry and entry.get("min_vram_gb") is not None:
                return {
                    "min_vram_gb":   entry["min_vram_gb"],
                    "ideal_vram_gb": entry.get("ideal_vram_gb", entry["min_vram_gb"]),
                    "workload":      entry.get("workload", "UNVERIFIED"),
                    "notes":         entry.get("description", ""),
                    "team":          entry.get("team", "Unknown"),
                    "verified":      entry.get("verified", False),
                    "source":        entry.get("source", "none"),
                }
        except Exception:
            pass
    return SUBNET_HW_REGISTRY.get(netuid, {
        "min_vram_gb":   None,
        "ideal_vram_gb": None,
        "workload":      "UNVERIFIED",
        "notes":         "Hardware requirements not verified for this subnet.",
    })

def gpu_fit_score(netuid, vram_gb):
    """
    Score 0-100 for how well a GPU class fits a subnet.
    100 = perfect fit
    0   = not viable
    """
    hw = get_hw_info(netuid)
    min_v   = hw.get("min_vram_gb")
    ideal_v = hw.get("ideal_vram_gb")

    if min_v is None:
        return {"score": 0, "label": "UNVERIFIED", "viable": False,
                "reason": "Hardware requirements not verified for this subnet."}

    if vram_gb < min_v:
        return {"score": 0, "label": "NOT VIABLE",  "viable": False,
                "reason": f"Requires {min_v}GB minimum. This card has {vram_gb}GB."}

    if vram_gb >= ideal_v:
        score = 100
        label = "IDEAL FIT"
    elif vram_gb >= min_v:
        # Linear scale between min and ideal
        score = int(50 + 50 * (vram_gb - min_v) / max(ideal_v - min_v, 1))
        label = "VIABLE"
    else:
        score = 0
        label = "NOT VIABLE"

    return {
        "score":   score,
        "label":   label,
        "viable":  score > 0,
        "reason":  hw["notes"],
        "workload": hw["workload"],
    }

# ── Opportunity Score ─────────────────────────────────────────────────────────
def opportunity_score(subnet, tao_price_usd=0.0, movers=None):
    """
    Composite opportunity score 0-100 for a subnet.
    Weights:
      30% emission opportunity
      20% burn efficiency (low burn = better entry)
      20% fill rate (lower = more room to enter)
      20% emission/burn ratio
      10% momentum (from movers if available)
    All math done here. LLM never sees raw calculation.
    """
    emission = float(subnet.get("emission", 0) or 0)
    burn     = float(subnet.get("burn_tao", 0) or 0)
    neurons  = int(subnet.get("neurons", 0) or 0)
    max_n    = int(subnet.get("max_neurons", 256) or 256)
    netuid   = subnet.get("netuid", 0)

    if emission == 0:
        return {
            "score": 0, "label": "NO EMISSION", "avoid": True,
            "breakdown": {"emission": 0, "burn_eff": 0, "fill": 0, "ratio": 0, "momentum": 0},
            "reason": "Zero emission — no rewards available.",
        }

    # Emission score (0-30): normalize against a reasonable max
    MAX_EMISSION = 0.05
    em_score = min(30, int((emission / MAX_EMISSION) * 30))

    # Burn efficiency score (0-20): lower burn = higher score
    MAX_BURN = 1.0
    burn_score = int((1 - min(burn / MAX_BURN, 1)) * 20) if burn > 0 else 10

    # Fill score (0-20): lower fill = more entry room
    fill_pct  = (neurons / max_n * 100) if max_n > 0 else 100
    fill_score = int((1 - fill_pct / 100) * 20)

    # Ratio score (0-20)
    ratio     = emission / burn if burn > 0 else 0
    MAX_RATIO = 10.0
    ratio_score = min(20, int((ratio / MAX_RATIO) * 20))

    # Momentum score (0-10): from movers data if available
    momentum_score = 5  # neutral default
    if movers:
        mover_map = {m["netuid"]: m for m in movers}
        if netuid in mover_map:
            em_pct = mover_map[netuid].get("em_pct_change", 0) or 0
            if em_pct > 5:
                momentum_score = 10
            elif em_pct > 0:
                momentum_score = 7
            elif em_pct < -5:
                momentum_score = 2
            else:
                momentum_score = 5

    total = em_score + burn_score + fill_score + ratio_score + momentum_score

    if total >= 75:
        label = "STRONG"
    elif total >= 50:
        label = "MODERATE"
    elif total >= 25:
        label = "WEAK"
    else:
        label = "AVOID"

    return {
        "score": total,
        "label": label,
        "avoid": total < 15,
        "breakdown": {
            "emission":   em_score,
            "burn_eff":   burn_score,
            "fill":       fill_score,
            "ratio":      ratio_score,
            "momentum":   momentum_score,
        },
        "metrics": {
            "emission":   round(emission, 6),
            "burn_tao":   round(burn, 4),
            "fill_pct":   round(fill_pct, 1),
            "ratio":      round(ratio, 2),
            "emission_usd": round(emission * tao_price_usd, 4) if tao_price_usd > 0 else None,
        },
        "reason": f"Score {total}/100 — emission:{em_score} burn:{burn_score} fill:{fill_score} ratio:{ratio_score} momentum:{momentum_score}",
    }

# ── GPU Rankings ──────────────────────────────────────────────────────────────
def rank_subnets_for_gpu_class(subnets, gpu_class_key, tao_price_usd=0.0, movers=None, top_n=10):
    """
    Rank all subnets for a specific GPU class.
    Combined score = opportunity_score * gpu_fit_weight
    Returns top_n results.
    """
    gpu = GPU_CLASSES.get(gpu_class_key)
    if not gpu:
        return []

    vram_gb = gpu["vram_gb"]
    results = []

    for s in subnets:
        netuid  = s.get("netuid", 0)
        opp     = opportunity_score(s, tao_price_usd, movers)
        fit     = gpu_fit_score(netuid, vram_gb)

        if opp["avoid"] or not fit["viable"]:
            continue

        combined = int(opp["score"] * (fit["score"] / 100))
        hw = get_hw_info(netuid)

        results.append({
            "netuid":           netuid,
            "name":             s.get("name", "Unknown"),
            "combined_score":   combined,
            "opportunity_score": opp["score"],
            "opportunity_label": opp["label"],
            "fit_score":        fit["score"],
            "fit_label":        fit["label"],
            "workload":         hw.get("workload", "UNVERIFIED"),
            "hw_notes":         hw.get("notes", ""),
            "metrics":          opp["metrics"],
            "breakdown":        opp["breakdown"],
        })

    results.sort(key=lambda x: x["combined_score"], reverse=True)
    return results[:top_n]

def rank_all_gpu_classes(subnets, tao_price_usd=0.0, movers=None, top_n=5):
    """Rank subnets for all GPU classes at once."""
    return {
        key: rank_subnets_for_gpu_class(
            subnets, key, tao_price_usd, movers, top_n
        )
        for key in GPU_CLASSES
    }

# ── Top Opportunities ─────────────────────────────────────────────────────────
def top_opportunities(subnets, tao_price_usd=0.0, movers=None, top_n=10):
    """Universal top opportunities regardless of GPU class."""
    scored = []
    for s in subnets:
        opp = opportunity_score(s, tao_price_usd, movers)
        if not opp["avoid"]:
            scored.append({
                "netuid":  s.get("netuid", 0),
                "name":    s.get("name", "Unknown"),
                **opp,
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]

# ── Confidence Gating ─────────────────────────────────────────────────────────
def confidence_gate(question_type, snapshot_count, has_movers=False):
    """
    Determine if we have enough data to answer a question type.
    Returns: (can_answer: bool, confidence: str, reason: str)
    """
    gates = {
        "current_price":     (True,              "HIGH",   "Live price from TaoStats/CoinGecko."),
        "current_emission":  (True,              "HIGH",   "Deterministic from live chain data."),
        "current_ranking":   (True,              "HIGH",   "Computed from live chain snapshot."),
        "hardware_fit":      (True,              "HIGH",   "Scored against GPU registry."),
        "trend_1h":          (snapshot_count>=2, "MEDIUM", "Requires 2+ snapshots."),
        "trend_24h":         (snapshot_count>=4, "MEDIUM", "Requires 4+ snapshots (~2h of data)."),
        "trend_7d":          (snapshot_count>=24,"LOW",    "Requires 24+ snapshots (~12h of data)."),
        "historical":        (snapshot_count>=2, "LOW",    "Limited history available."),
    }
    gate = gates.get(question_type, (True, "LOW", "Insufficient data for this query type."))
    can_answer, confidence, reason = gate
    if not can_answer:
        reason = f"Cannot answer — {reason}"
    return can_answer, confidence, reason

# ── Summary Builder ───────────────────────────────────────────────────────────
def build_analytics_summary(subnets, tao_price_usd=0.0, movers=None, snapshot_count=1):
    """
    Build complete analytics summary for LLM context.
    All numbers computed here. LLM only explains results.
    """
    top_opps  = top_opportunities(subnets, tao_price_usd, movers, top_n=5)
    gpu_ranks = rank_all_gpu_classes(subnets, tao_price_usd, movers, top_n=3)

    lines = []
    lines.append(f"=== ANALYTICS SUMMARY ===")
    lines.append(f"TAO/USD: ${tao_price_usd:.2f}" if tao_price_usd > 0 else "TAO/USD: unavailable")
    lines.append(f"Snapshots in DB: {snapshot_count}")
    lines.append(f"Historical data: {'YES' if snapshot_count >= 2 else 'NO - need more snapshots'}")
    lines.append("")

    lines.append("--- TOP 5 OPPORTUNITIES (all hardware) ---")
    for i, o in enumerate(top_opps, 1):
        m = o.get("metrics", {})
        lines.append(
            f"  {i}. SN{o['netuid']} {o['name']}: score={o['score']}/100 [{o['label']}]"
            f" emit={m.get('emission',0):.5f}t"
            + (f" (${m['emission_usd']:.3f})" if m.get('emission_usd') else "")
            + f" burn={m.get('burn_tao',0):.4f}t"
            f" fill={m.get('fill_pct',0):.0f}%"
            f" ratio={m.get('ratio',0):.1f}"
        )

    lines.append("")
    for cls_key, cls_results in gpu_ranks.items():
        gpu = GPU_CLASSES[cls_key]
        lines.append(f"--- TOP 3 FOR {gpu['label']} ({', '.join(gpu['cards'])}) ---")
        if not cls_results:
            lines.append("  No viable subnets for this GPU class.")
            continue
        for i, r in enumerate(cls_results, 1):
            m = r.get("metrics", {})
            lines.append(
                f"  {i}. SN{r['netuid']} {r['name']}: "
                f"combined={r['combined_score']}/100"
                f" opp={r['opportunity_score']} fit={r['fit_score']}"
                f" [{r['fit_label']}] workload={r['workload']}"
                + (f" (${m['emission_usd']:.3f}/tempo)" if m.get('emission_usd') else "")
            )
        lines.append("")

    if movers:
        lines.append("--- TOP MOVERS ---")
        for m in movers[:5]:
            direction = "UP" if m.get("em_delta", 0) > 0 else "DOWN"
            lines.append(
                f"  SN{m['netuid']} {m['name']}: "
                f"emission {direction} {m.get('em_pct_change',0):.1f}%"
                f" fill {m.get('fill_delta',0):+.1f}%"
            )

    return "\n".join(lines)
