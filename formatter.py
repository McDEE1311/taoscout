#!/usr/bin/env python3
"""
TaoScout Response Formatter v1.0
Converts deterministic payloads into Bloomberg-terminal-style tables.
No prose. No paragraphs. Tables, lists, exact numbers.
"""

def pad(s, n, right=False):
    s = str(s)[:n]
    return s.rjust(n) if right else s.ljust(n)

def divider(char="─", width=72):
    return char * width

def header(title, confidence, source):
    lines = [
        divider(),
        f"  {title}",
        f"  Confidence: {confidence}  |  Source: {source}",
        divider(),
    ]
    return "\n".join(lines)

def table(rows, columns):
    """
    columns = list of (key, label, width, right_align)
    rows = list of dicts
    """
    if not rows:
        return "  No data available."

    # Header row
    header_parts = []
    sep_parts = []
    for key, label, width, right in columns:
        header_parts.append(pad(label, width, right))
        sep_parts.append("─" * width)

    lines = [
        "  " + "  ".join(header_parts),
        "  " + "  ".join(sep_parts),
    ]

    for r in rows:
        row_parts = []
        for key, label, width, right in columns:
            val = r.get(key, "—")
            if val is None:
                val = "—"
            elif isinstance(val, float):
                val = f"{val:.4f}" if val < 1 else f"{val:.2f}"
            row_parts.append(pad(str(val), width, right))
        lines.append("  " + "  ".join(row_parts))

    return "\n".join(lines)

def format_top_emission(payload):
    results = payload.get("results", [])
    n = len(results)
    tao_usd = payload.get("tao_usd", 0)

    rows = []
    for r in results:
        em = r.get("emission_tao", 0)
        burn = r.get("burn_tao", 0)
        ratio = round(em / burn, 1) if burn > 0 else 0
        usd = f"${r['emission_usd']:.3f}" if r.get("emission_usd") else "—"
        rows.append({
            "rank":     r.get("rank", ""),
            "netuid":   f"SN{r['netuid']}",
            "name":     r.get("name", "Unknown")[:16],
            "emit_tao": f"{em:.5f}",
            "emit_usd": usd,
            "burn":     f"{burn:.4f}",
            "ratio":    f"{ratio:.1f}x",
            "fill":     f"{r.get('fill_pct',0):.0f}%",
        })

    cols = [
        ("rank",     "#",        3,  True),
        ("netuid",   "Subnet",   8,  False),
        ("name",     "Name",    16,  False),
        ("emit_tao", "Emit(τ)", 10,  True),
        ("emit_usd", "USD",      9,  True),
        ("burn",     "Burn(τ)",  9,  True),
        ("ratio",    "Ratio",    7,  True),
        ("fill",     "Fill",     5,  True),
    ]

    lines = [
        header(f"TOP {n} BY EMISSION", payload.get("confidence","HIGH"),
               "tao_in_emission field, Python sort, no LLM math"),
        table(rows, cols),
        "",
        f"  Block: {payload.get('block','?')}  |  TAO/USD: ${tao_usd:.2f}  |  Fields: {', '.join(payload.get('fields_used',[])[:3])}",
    ]
    return "\n".join(lines)

def format_top_ratio(payload):
    results = payload.get("results", [])
    n = len(results)
    tao_usd = payload.get("tao_usd", 0)

    rows = []
    for r in results:
        usd = f"${r['emission_usd']:.3f}" if r.get("emission_usd") else "—"
        rows.append({
            "rank":     r.get("rank", ""),
            "netuid":   f"SN{r['netuid']}",
            "name":     r.get("name","Unknown")[:16],
            "ratio":    f"{r.get('ratio',0):.1f}x",
            "emit_tao": f"{r.get('emission_tao',0):.5f}",
            "emit_usd": usd,
            "burn":     f"{r.get('burn_tao',0):.4f}",
        })

    cols = [
        ("rank",     "#",       3,  True),
        ("netuid",   "Subnet",  8,  False),
        ("name",     "Name",   16,  False),
        ("ratio",    "Ratio",   8,  True),
        ("emit_tao", "Emit(τ)",10,  True),
        ("emit_usd", "USD",     9,  True),
        ("burn",     "Burn(τ)", 9,  True),
    ]

    lines = [
        header(f"TOP {n} BY EMISSION/BURN RATIO",
               payload.get("confidence","HIGH"),
               "ratio=emission/burn_tao, Python sort, no LLM math"),
        table(rows, cols),
        "",
        f"  Formula: ratio = emission / burn_tao  |  Block: {payload.get('block','?')}",
    ]
    return "\n".join(lines)

def format_gpu_ranking(payload):
    results = payload.get("results", [])
    n = len(results)
    gpu_label = payload.get("gpu_label", "")
    cards = ", ".join(payload.get("cards", []))
    tao_usd = payload.get("tao_usd", 0)

    rows = []
    for r in results:
        bd = r.get("score_breakdown", {})
        usd = f"${r['emission_usd']:.3f}" if r.get("emission_usd") else "—"
        rows.append({
            "rank":    r.get("rank",""),
            "netuid":  f"SN{r['netuid']}",
            "name":    r.get("name","Unknown")[:14],
            "score":   f"{r.get('combined_score',0)}/100",
            "opp":     str(r.get("opportunity_score",0)),
            "fit":     str(r.get("fit_score",0)),
            "emit":    f"{r.get('emission_tao',0):.5f}",
            "usd":     usd,
            "fill":    f"{r.get('fill_pct',0):.0f}%",
            "workload": r.get("workload","UNVERIFIED")[:18],
        })

    cols = [
        ("rank",    "#",       3,  True),
        ("netuid",  "Subnet",  8,  False),
        ("name",    "Name",   14,  False),
        ("score",   "Score",   7,  True),
        ("opp",     "Opp",     4,  True),
        ("fit",     "Fit",     4,  True),
        ("emit",    "Emit(τ)", 10, True),
        ("usd",     "USD",      9,  True),
        ("fill",    "Fill",     5,  True),
        ("workload","Workload",18,  False),
    ]

    lines = [
        header(f"TOP {n} FOR {gpu_label} ({cards})",
               payload.get("confidence","HIGH"),
               "combined=opp*(fit/100), hardware registry, no LLM math"),
        f"  Formula: combined_score = opportunity_score × (fit_score / 100)",
        f"  Opp weights: emission 30% | burn_eff 20% | fill 20% | ratio 20% | momentum 10%",
        "",
        table(rows, cols),
        "",
        f"  Block: {payload.get('block','?')}  |  TAO/USD: ${tao_usd:.2f}",
    ]
    return "\n".join(lines)

def format_movers(payload):
    if not payload.get("available"):
        return (
            f"{divider()}\n"
            f"  MOVERS — INSUFFICIENT DATA\n"
            f"  {payload.get('confidence_reason','')}\n"
            f"  Snapshots: {payload.get('snapshot_count',0)}  |  Need: 4+\n"
            f"{divider()}"
        )

    results = payload.get("results", [])
    n = len(results)

    rows = []
    for r in results:
        direction = "▲" if r.get("direction") == "UP" else "▼"
        rows.append({
            "rank":   r.get("rank",""),
            "netuid": f"SN{r['netuid']}",
            "name":   r.get("name","Unknown")[:16],
            "dir":    direction,
            "pct":    f"{r.get('em_pct_change',0):+.1f}%",
            "curr":   f"{r.get('curr_emission',0):.5f}",
            "prev":   f"{r.get('prev_emission',0):.5f}",
            "delta":  f"{r.get('em_delta',0):+.5f}",
            "fill_d": f"{r.get('fill_delta',0):+.1f}%",
        })

    cols = [
        ("rank",   "#",       3,  True),
        ("netuid", "Subnet",  8,  False),
        ("name",   "Name",   16,  False),
        ("dir",    "↕",       2,  False),
        ("pct",    "Chg%",    7,  True),
        ("curr",   "Current", 10, True),
        ("prev",   "Previous",10, True),
        ("delta",  "Delta",   10, True),
        ("fill_d", "Fill Δ",   7, True),
    ]

    lines = [
        header(f"TOP {n} EMISSION MOVERS",
               payload.get("confidence","MEDIUM"),
               "curr-prev from SQLite snapshots, no LLM math"),
        f"  Snapshots: {payload.get('snapshot_count',0)}  |  New subnets excluded: {payload.get('new_subnets_excluded',0)}",
        "",
        table(rows, cols),
    ]
    return "\n".join(lines)

def format_subnet_detail(payload):
    results = payload.get("results", [])
    if not results:
        return "  No subnet data found."

    lines = []
    tao_usd = payload.get("tao_usd", 0)

    for r in results:
        if "error" in r:
            lines.append(f"  SN{r['netuid']}: {r['error']}")
            continue

        nid = r["netuid"]
        name = r.get("name","Unknown")
        em = r.get("emission_tao", 0)
        burn = r.get("burn_tao", 0)
        ratio = r.get("ratio", 0)
        fill = r.get("fill_pct", 0)
        usd = r.get("emission_usd")
        opp = r.get("opportunity_score")
        mv = r.get("mover_24h")
        gpu_fits = r.get("gpu_fits", {})

        lines.append(divider())
        lines.append(f"  SN{nid} — {name}")
        lines.append(divider("─", 40))

        metrics = [
            ("Emission",     f"{em:.6f} τ" + (f"  (${usd:.4f}/tempo)" if usd else "")),
            ("Burn cost",    f"{burn:.4f} τ"),
            ("Em/Burn ratio",f"{ratio:.2f}x"),
            ("Fill rate",    f"{fill:.1f}%  ({r.get('neurons',0)}/{r.get('max_neurons',256)})"),
            ("Tempo",        f"{r.get('tempo',360)} blocks"),
            ("Alpha price",  f"{r.get('price',0):.6f} τ"),
        ]
        if opp is not None:
            metrics.append(("Opp score", f"{opp}/100"))
        if mv:
            arrow = "▲" if mv["direction"] == "UP" else "▼"
            metrics.append(("24h change", f"{arrow} {mv['em_pct_change']:+.1f}%  (delta {mv['em_delta']:+.6f} τ)"))

        for label, val in metrics:
            lines.append(f"  {pad(label,16)} {val}")

        if gpu_fits:
            lines.append("")
            lines.append("  GPU Fit Scores:")
            fit_cols = [
                ("class",  "Class",  6,  False),
                ("cards",  "Cards",  30, False),
                ("score",  "Score",  6,  True),
                ("label",  "Fit",    12, False),
            ]
            fit_rows = []
            from analytics import GPU_CLASSES
            for cls, fit in gpu_fits.items():
                gpu = GPU_CLASSES.get(cls, {})
                fit_rows.append({
                    "class": cls,
                    "cards": ", ".join(gpu.get("cards", []))[:30],
                    "score": f"{fit.get('score',0)}/100",
                    "label": fit.get("label","—"),
                })
            lines.append(table(fit_rows, fit_cols))

    lines.append(divider())
    lines.append(f"  Block: {payload.get('block','?')}  |  Fields: {', '.join(payload.get('fields_used',[])[:4])}")
    return "\n".join(lines)

def format_risk(payload):
    results = payload.get("results", [])
    n = len(results)

    if not results:
        lines = [
            header("RISK FLAGS", "HIGH", "live chain data"),
            "  No risk flags detected in current data.",
        ]
        return "\n".join(lines)

    rows = []
    for r in results:
        sn = f"SN{r['netuid']}" if r.get("netuid") else "NETWORK"
        rows.append({
            "sn":       sn,
            "severity": r.get("severity","—"),
            "flag":     r.get("flag","—")[:24],
            "metric":   r.get("metric","—")[:20],
            "source":   r.get("source","—")[:20],
        })

    cols = [
        ("sn",       "Subnet",   8,  False),
        ("severity", "Severity", 8,  False),
        ("flag",     "Flag",    24,  False),
        ("metric",   "Metric",  20,  False),
        ("source",   "Source",  20,  False),
    ]

    lines = [
        header(f"{n} RISK FLAG(S) DETECTED", "HIGH",
               "factual flags only, each backed by explicit data source"),
        table(rows, cols),
    ]
    return "\n".join(lines)

def format_emission_threshold(payload):
    results = payload.get("results", [])
    threshold = payload.get("threshold_tao", 0.01)
    n = len(results)
    tao_usd = payload.get("tao_usd", 0)

    rows = []
    for r in results:
        usd = f"${r['emission_usd']:.3f}" if r.get("emission_usd") else "—"
        rows.append({
            "rank":   r.get("rank",""),
            "netuid": f"SN{r['netuid']}",
            "name":   r.get("name","Unknown")[:18],
            "emit":   f"{r.get('emission_tao',0):.5f}",
            "usd":    usd,
        })

    cols = [
        ("rank",  "#",      3,  True),
        ("netuid","Subnet", 8,  False),
        ("name",  "Name",  18,  False),
        ("emit",  "Emit(τ)",10, True),
        ("usd",   "USD",    9,  True),
    ]

    lines = [
        header(f"{n} SUBNETS WITH EMISSION ≥ {threshold:.3f} τ",
               payload.get("confidence","HIGH"),
               "Python filter on tao_in_emission, no LLM math"),
        table(rows, cols),
        "",
        f"  Threshold: {threshold:.4f} τ  |  Block: {payload.get('block','?')}  |  TAO/USD: ${tao_usd:.2f}",
    ]
    return "\n".join(lines)

def format_zero_emission(payload):
    results = payload.get("results", [])
    n = len(results)

    rows = [{"netuid": f"SN{r['netuid']}", "name": r.get("name","Unknown")[:20],
             "burn": f"{r.get('burn_tao',0):.4f}"} for r in results[:20]]

    cols = [
        ("netuid","Subnet", 8,  False),
        ("name",  "Name",  20,  False),
        ("burn",  "Burn(τ)",9,  True),
    ]

    lines = [
        header(f"{n} SUBNETS WITH ZERO EMISSION", "HIGH",
               "emission == 0 filter from live chain snapshot"),
        table(rows, cols),
    ]
    if n > 20:
        lines.append(f"  ... and {n-20} more")
    return "\n".join(lines)

def format_fill_rate(payload):
    results = payload.get("results", [])
    n = len(results)
    tao_usd = payload.get("tao_usd", 0)

    rows = []
    for r in results:
        usd = f"${r['emission_usd']:.3f}" if r.get("emission_usd") else "—"
        rows.append({
            "netuid": f"SN{r['netuid']}",
            "name":   r.get("name","Unknown")[:16],
            "fill":   f"{r.get('fill_pct',0):.1f}%",
            "slots":  f"{r.get('neurons',0)}/{r.get('max_neurons',256)}",
            "emit":   f"{r.get('emission_tao',0):.5f}",
            "usd":    usd,
            "burn":   f"{r.get('burn_tao',0):.4f}",
        })

    cols = [
        ("netuid","Subnet", 8,  False),
        ("name",  "Name",  16,  False),
        ("fill",  "Fill",   6,  True),
        ("slots", "Slots",  9,  False),
        ("emit",  "Emit(τ)",10, True),
        ("usd",   "USD",    9,  True),
        ("burn",  "Burn(τ)",9,  True),
    ]

    lines = [
        header(f"{n} LOW-FILL SUBNETS WITH ACTIVE EMISSION",
               payload.get("confidence","HIGH"),
               "fill<80% filter + emission>0, Python sort by fill asc"),
        table(rows, cols),
        "",
        f"  Entry windows: lower fill = easier registration  |  Block: {payload.get('block','?')}",
    ]
    return "\n".join(lines)

def format_general(payload):
    lines = [
        header("OPERATOR CONTEXT", payload.get("confidence","MEDIUM"),
               "top emission + ratio from live snapshot"),
    ]
    top_em = payload.get("top_emission", [])
    if top_em:
        lines.append("  TOP 5 BY EMISSION:")
        for r in top_em[:5]:
            em = r.get("emission_tao",0)
            usd = f" (${r['emission_usd']:.3f})" if r.get("emission_usd") else ""
            lines.append(f"    {r['rank']}. SN{r['netuid']} {r.get('name',''):<16} {em:.5f}τ{usd}")
    top_ratio = payload.get("top_ratio", [])
    if top_ratio:
        lines.append("")
        lines.append("  TOP 5 BY RATIO:")
        for r in top_ratio[:5]:
            lines.append(f"    {r['rank']}. SN{r['netuid']} {r.get('name',''):<16} ratio={r.get('ratio',0):.1f}x")
    return "\n".join(lines)

# ── Master formatter ──────────────────────────────────────────────────────────

FORMAT_SYSTEM = """You are TaoScout, a Bittensor operator intelligence terminal.

You receive a pre-formatted table string and a structured JSON payload.
Your job is to write a 2-3 line operator summary ONLY.

Rules:
1. Start with FINDING: [one sentence direct answer]
2. Then BASIS: [cite exact field names from fields_used array]
3. Then CONFIDENCE: [use confidence from payload] — [confidence_reason]
4. Then GAPS: [missing data or limitations]
5. DO NOT reproduce the table — it is already shown separately
6. DO NOT add analysis, opinions, or unsupported claims
7. Keep it under 6 lines total
8. Reference specific subnet IDs and values from the payload"""

def format_payload(payload):
    """Route payload to correct formatter. Returns formatted table string."""
    intent = payload.get("intent", "general")
    formatters = {
        "top_emission":       format_top_emission,
        "top_ratio":          format_top_ratio,
        "gpu_ranking":        format_gpu_ranking,
        "movers":             format_movers,
        "subnet_detail":      format_subnet_detail,
        "my_subnets":         format_subnet_detail,
        "risk":               format_risk,
        "emission_threshold": format_emission_threshold,
        "zero_emission":      format_zero_emission,
        "fill_rate":          format_fill_rate,
        "general":            format_general,
        "compare":            format_general,
        "network_summary":    format_general,
    }
    fn = formatters.get(intent, format_general)
    return fn(payload)

def build_llm_summary_prompt(question, payload, formatted_table):
    import json
    compact = {
        "question":         question,
        "intent":           payload.get("intent"),
        "confidence":       payload.get("confidence"),
        "confidence_reason":payload.get("confidence_reason"),
        "fields_used":      payload.get("fields_used", []),
        "computation":      payload.get("computation",""),
        "block":            payload.get("block","?"),
        "tao_usd":          payload.get("tao_usd", 0),
        "result_count":     payload.get("result_count", len(payload.get("results",[]))),
        "top_results":      payload.get("results", [])[:3],
    }
    return f"""Question: {question}

Pre-formatted table (already shown to user — do NOT reproduce):
[TABLE OMITTED]

Payload summary:
{json.dumps(compact, indent=2, default=str)}

Write a 2-3 line operator summary following the FORMAT_SYSTEM rules."""
