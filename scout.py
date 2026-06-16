#!/usr/bin/env python3
"""
TaoScout v1.3 - Bittensor Intelligence Agent
Wired to analytics engine + SQLite DB + GitHub subnet data
"""
import json, sys, argparse, subprocess, urllib.request, urllib.parse, time
from datetime import datetime, timezone
from pathlib import Path

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    WHITE  = "\033[97m"
    DIM    = "\033[2m"
    BLUE   = "\033[94m"

def hdr(text):
    w = 64
    print(f"\n{C.CYAN}{C.BOLD}{'─'*w}{C.RESET}")
    print(f"{C.CYAN}{C.BOLD}  {text}{C.RESET}")
    print(f"{C.CYAN}{C.BOLD}{'─'*w}{C.RESET}")

def tag_ok(msg):   print(f"  {C.GREEN}{C.BOLD}[OK]{C.RESET}     {msg}")
def tag_warn(msg): print(f"  {C.YELLOW}{C.BOLD}[WARN]{C.RESET}   {msg}")
def tag_risk(msg): print(f"  {C.RED}{C.BOLD}[RISK]{C.RESET}   {msg}")
def tag_info(msg): print(f"  {C.CYAN}[INFO]{C.RESET}   {msg}")
def tag_act(msg):  print(f"  {C.WHITE}{C.BOLD}[ACTION]{C.RESET} {msg}")
def sep():         print(f"{C.DIM}{'─'*64}{C.RESET}")

def banner():
    print(f"""{C.CYAN}{C.BOLD}
  ████████╗ █████╗  ██████╗ ███████╗ ██████╗ ██████╗ ██╗   ██╗████████╗
     ██╔══╝██╔══██╗██╔═══██╗██╔════╝██╔════╝██╔═══██╗██║   ██║╚══██╔══╝
     ██║   ███████║██║   ██║███████╗██║     ██║   ██║██║   ██║   ██║
     ██║   ██╔══██║██║   ██║╚════██║██║     ██║   ██║██║   ██║   ██║
     ██║   ██║  ██║╚██████╔╝███████║╚██████╗╚██████╔╝╚██████╔╝   ██║
     ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚══════╝ ╚═════╝ ╚═════╝  ╚═════╝    ╚═╝
{C.RESET}{C.DIM}  Bittensor Operator Intelligence  |  v1.3  |  Finney mainnet{C.RESET}
""")

SCRIPT_DIR    = Path(__file__).parent
CFG_FILE      = SCRIPT_DIR / "config.json"
META_FILE     = SCRIPT_DIR / "subnet_meta.json"

def load_config():
    if not CFG_FILE.exists():
        print(f"{C.RED}[ERROR] Config not found{C.RESET}"); sys.exit(1)
    with open(CFG_FILE) as f:
        return json.load(f)

CFG           = load_config()
DATA_DIR      = Path(CFG["data_dir"])
LOG_DIR       = Path(CFG["log_dir"])
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
MY_SUBNETS    = CFG.get("my_subnets", [])
MODEL         = CFG["model"]
OLLAMA_URL    = CFG["ollama_url"]
BT_PYTHON     = str(Path(CFG.get("bt_venv", "/home/mcdeerig/bt-venv")) / "bin" / "python3")
TAOSTATS_KEY  = CFG.get("taostats_api_key", "")
GITHUB_TOKEN  = CFG.get("github_token", "")
TAOSTATS_BASE = "https://api.taostats.io/api/v1"

def load_meta():
    if META_FILE.exists():
        with open(META_FILE) as f:
            return json.load(f)
    return {}

META = load_meta()

def get_meta(netuid):
    return META.get(str(netuid), {
        "name": "Unknown", "type": "UNVERIFIED", "hardware": "unverified"
    })

# ── Import analytics + db ─────────────────────────────────────────────────────
try:
    import db as DB
    import analytics as AN
    DB.init_db()
    HAS_ANALYTICS = True
except Exception as e:
    HAS_ANALYTICS = False
    print(f"{C.YELLOW}[WARN] Analytics engine not loaded: {e}{C.RESET}")

# ── Cache helpers ──────────────────────────────────────────────────────────────
def save_data(name, data):
    p = DATA_DIR / f"{name}.json"
    with open(p, "w") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}, f)

def load_data(name):
    p = DATA_DIR / f"{name}.json"
    if not p.exists(): return None, None
    with open(p) as f:
        d = json.load(f)
    return d.get("data"), d.get("fetched_at")

def is_fresh(ts, mins=None):
    if not ts: return False
    if mins is None: mins = CFG.get("refresh_interval_minutes", 30)
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 60
        return age < mins
    except: return False

def save_snapshot(data):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    p  = DATA_DIR / f"snapshot_{ts}.json"
    with open(p, "w") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}, f)
    snaps = sorted(DATA_DIR.glob("snapshot_*.json"))
    for old in snaps[:-48]:
        old.unlink()

def load_previous_snapshot():
    snaps = sorted(DATA_DIR.glob("snapshot_*.json"))
    if len(snaps) < 2: return None, None
    with open(snaps[-2]) as f:
        d = json.load(f)
    return d.get("data"), d.get("fetched_at")

def snapshot_age_minutes():
    _, ts = load_data("chain")
    if not ts: return None
    try:
        return round((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 60, 1)
    except: return None

# ── TaoStats API ──────────────────────────────────────────────────────────────
def taostats_get(path, params=None):
    if not TAOSTATS_KEY: return None
    url = f"{TAOSTATS_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TAOSTATS_KEY}",
        "accept": "application/json"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except: return None

def fetch_tao_price():
    data = taostats_get("/tao/price/latest")
    if data:
        try:
            price = float(data.get("price", 0) or data.get("data", {}).get("price", 0))
            if price > 0:
                save_data("tao_price", {"usd": price})
                return price
        except: pass
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"User-Agent": "TaoScout/1.3"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
            price = float(data["bittensor"]["usd"])
            save_data("tao_price", {"usd": price})
            return price
    except: pass
    cached, _ = load_data("tao_price")
    if cached: return float(cached.get("usd", 0))
    return 0.0

def fetch_deregistration_risk():
    cached, ts = load_data("dereg_risk")
    if cached and is_fresh(ts, mins=60): return cached
    data = taostats_get("/subnet/deregistration-ranking")
    if data:
        try:
            rankings = data.get("data", data) if isinstance(data, dict) else data
            if rankings:
                save_data("dereg_risk", rankings)
                return rankings
        except: pass
    return cached or []

def fetch_enrichment_data():
    enrichment = {}
    tag_info("Fetching TAO price...")
    enrichment["tao_price_usd"] = fetch_tao_price()
    if enrichment["tao_price_usd"] > 0:
        tag_ok(f"TAO price: ${enrichment['tao_price_usd']:.2f}")
    else:
        tag_warn("TAO price unavailable")
    time.sleep(0.5)
    tag_info("Fetching deregistration risk...")
    enrichment["dereg_risk"] = fetch_deregistration_risk()
    if enrichment["dereg_risk"]:
        tag_ok(f"Dereg risk: {len(enrichment['dereg_risk'])} subnets")
    else:
        tag_warn("Dereg risk data unavailable")
    save_data("enrichment", enrichment)
    return enrichment

def load_enrichment():
    cached, ts = load_data("enrichment")
    if cached and is_fresh(ts, mins=30): return cached
    return fetch_enrichment_data()

# ── GitHub subnet search ──────────────────────────────────────────────────────
def github_search_subnet(netuid, subnet_name=""):
    # Check curated registry first
    import json as _json
    from pathlib import Path as _Path
    repos_file = _Path(__file__).parent / "subnet_repos.json"
    if repos_file.exists():
        try:
            curated = _json.loads(repos_file.read_text())
            if str(netuid) in curated:
                r = curated[str(netuid)]
                return [{
                    "name":                 r.get("repo_name", ""),
                    "url":                  r.get("repo_url", ""),
                    "description":          r.get("readme_summary", ""),
                    "stars":                0,
                    "verified":             r.get("verified", False),
                    "source":               r.get("source", "curated_registry"),
                    "hardware_requirements":r.get("hardware_requirements", ""),
                    "last_checked":         r.get("last_checked", ""),
                }]
        except Exception:
            pass
    # Fallback to GitHub search — label as unverified
    """
    Search GitHub for public repos related to a Bittensor subnet.
    Returns list of repos with stars, description, url.
    Uses unauthenticated API (60 req/hr) or token if configured.
    """
    cached, ts = load_data(f"github_sn{netuid}")
    if cached and is_fresh(ts, mins=120):
        return cached

    queries = [f"bittensor subnet {netuid}"]
    if subnet_name and subnet_name.lower() not in ("unknown", ""):
        queries.append(f"bittensor {subnet_name}")

    headers = {"User-Agent": "TaoScout/1.3", "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    results = []
    seen    = set()

    for query in queries[:2]:
        try:
            url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({
                "q": query, "sort": "stars", "order": "desc", "per_page": 5
            })
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
                for item in data.get("items", []):
                    if item["full_name"] not in seen:
                        seen.add(item["full_name"])
                        results.append({
                            "name":        item["full_name"],
                            "description": item.get("description", "") or "",
                            "stars":       item.get("stargazers_count", 0),
                            "url":         item["html_url"],
                            "updated":     item.get("updated_at", "")[:10],
                        })
            time.sleep(0.3)
        except Exception:
            pass

    results.sort(key=lambda x: x["stars"], reverse=True)
    results = results[:5]
    save_data(f"github_sn{netuid}", results)
    return results

def format_github_context(netuid, subnet_name=""):
    """Format GitHub results as LLM context."""
    results = github_search_subnet(netuid, subnet_name)
    if not results:
        return f"GITHUB: No public repos found for SN{netuid}"
    lines = [f"GITHUB REPOS FOR SN{netuid} {subnet_name}:"]
    for r in results:
        lines.append(f"  {r['name']} ({r['stars']} stars) — {r['description'][:80]}")
        lines.append(f"    {r['url']} | updated {r['updated']}")
    return "\n".join(lines)

# ── Chain fetcher ─────────────────────────────────────────────────────────────
FETCH_SCRIPT = """
import json
import bittensor as bt

sub = bt.Subtensor(network='finney')
subnets = []
prices = {}
try:
    raw = sub.get_subnet_prices()
    for k, v in raw.items():
        try:
            s = str(v)
            for ch in [chr(964), 't', ' ']:
                s = s.replace(ch, '')
            prices[str(k)] = float(s)
        except:
            pass
except:
    pass

infos = sub.get_all_metagraphs_info()
for info in infos:
    if info is None:
        continue
    try:
        nid = info.netuid
        tao_em_raw = str(getattr(info, 'tao_in_emission', 0) or 0)
        for ch in [chr(964), 't', ' ', ',']:
            tao_em_raw = tao_em_raw.replace(ch, '')
        tao_em = float(tao_em_raw)
        burn_raw = str(info.burn or '0.0005')
        for ch in [chr(964), 't', ' ']:
            burn_raw = burn_raw.replace(ch, '')
        burn_tao = float(burn_raw)
        subnets.append({
            'netuid': nid,
            'name': str(getattr(info, 'name', '') or ''),
            'emission': tao_em,
            'burn_tao': burn_tao,
            'neurons': int(info.num_uids or 0),
            'max_neurons': int(info.max_uids or 256),
            'tempo': int(getattr(info, 'tempo', 360) or 360),
            'price': prices.get(str(nid), 0.0),
        })
    except:
        pass

print(json.dumps({'block': sub.block, 'subnets': subnets}))
"""

def fetch_chain_data():
    try:
        proc = subprocess.run(
            [BT_PYTHON, "-c", FETCH_SCRIPT],
            capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            err = proc.stderr[:300] if proc.stderr else "no output"
            return None, f"bt query failed: {err}"
        return json.loads(proc.stdout.strip()), None
    except subprocess.TimeoutExpired:
        return None, "bt query timed out"
    except Exception as e:
        return None, str(e)

def get_chain_data(force=False):
    cached, ts = load_data("chain")
    if cached and not force and is_fresh(ts):
        return cached, f"cached ({ts[:16]})"
    tag_info("Querying Bittensor Finney mainnet...")
    data, err = fetch_chain_data()
    if err:
        tag_warn(f"Chain fetch failed: {err}")
        if cached:
            tag_warn("Falling back to cached data")
            return cached, "cached (fetch failed)"
        return None, err
    save_data("chain", data)
    save_snapshot(data)
    # Insert into SQLite DB
    if HAS_ANALYTICS:
        enrich_cached, _ = load_data("enrichment")
        tao_price = (enrich_cached or {}).get("tao_price_usd", 0.0)
        snap_id = DB.insert_snapshot(data, tao_price)
        if snap_id:
            tag_ok(f"Snapshot saved to DB (id={snap_id})")
    n = len(data.get("subnets", []))
    tag_ok(f"Block {data.get('block','?')} | {n} subnets loaded")
    return data, "live"

# ── Analytics context builder ─────────────────────────────────────────────────
def build_context(data, include_deltas=True, enrichment=None):
    if enrichment is None:
        enrichment = load_enrichment()

    tao_price = enrichment.get("tao_price_usd", 0.0)
    subnets   = data.get("subnets", [])
    block     = data.get("block", "?")

    if HAS_ANALYTICS:
        snap_count = DB.get_snapshot_count()
        movers     = DB.get_movers(hours=24, top_n=10) if snap_count >= 2 else []
        ctx = AN.build_analytics_summary(subnets, tao_price, movers, snap_count)
        ctx += f"\n\nBLOCK: {block}"
        ctx += f"\nSNAPSHOT_COUNT: {snap_count}"
        ctx += f"\nTAO_USD: ${tao_price:.2f}" if tao_price > 0 else "\nTAO_USD: unavailable"

        # Add MY subnets detail
        my = [s for s in subnets if s.get("netuid") in MY_SUBNETS]
        if my:
            ctx += "\n\n=== MY SUBNETS ==="
            for s in my:
                nid = s["netuid"]
                em  = float(s.get("emission", 0) or 0)
                b   = float(s.get("burn_tao", 0) or 0)
                r   = em / b if b > 0 else 0
                ctx += (
                    f"\n  SN{nid} {s.get('name','')}: "
                    f"emission={em:.5f}t burn={b:.4f}t ratio={r:.1f} "
                    f"fill={s.get('neurons','?')}/{s.get('max_neurons','?')}"
                )

        # Add dereg risk
        risk = enrichment.get("dereg_risk", [])
        if risk and isinstance(risk, list):
            ctx += "\n\n=== DEREGISTRATION RISK ==="
            for item in risk[:5]:
                nid = item.get("netuid") or item.get("net_uid", "?")
                ctx += f"\n  SN{nid}: rank={item.get('rank','?')}"

    else:
        # Fallback to simple context if analytics not loaded
        lines = [
            f"Block {block} | TAO/USD: ${tao_price:.2f}",
            f"Subnets: {len(subnets)}",
        ]
        by_em = sorted(subnets, key=lambda s: float(s.get("emission",0) or 0), reverse=True)
        lines.append("Top 10 by emission:")
        for s in by_em[:10]:
            em = float(s.get("emission",0) or 0)
            b  = float(s.get("burn_tao",0) or 0)
            lines.append(f"  SN{s['netuid']} {s.get('name','')}: emit={em:.5f} burn={b:.4f}")
        ctx = "\n".join(lines)

    return ctx

# ── Grounding block ───────────────────────────────────────────────────────────
def build_grounding(data, enrichment=None):
    _, cached_ts = load_data("chain")
    snap_time    = (cached_ts or datetime.now(timezone.utc).isoformat())[:19]
    snap_time    = snap_time.replace("T", " ") + " UTC"
    block        = data.get("block", "unknown") if isinstance(data, dict) else "unknown"
    n            = len(data.get("subnets", [])) if isinstance(data, dict) else 0
    tao_price    = (enrichment or {}).get("tao_price_usd", 0)
    snaps        = sorted(DATA_DIR.glob("snapshot_*.json"))
    db_count     = DB.get_snapshot_count() if HAS_ANALYTICS else len(snaps)
    return {
        "data_source":       "Finney mainnet + TaoStats API + GitHub",
        "snapshot_time":     snap_time,
        "block_height":      str(block),
        "subnets_analyzed":  n,
        "metadata_coverage": f"{sum(1 for k in META)} of 129 subnets verified",
        "tao_price_usd":     f"${tao_price:.2f}" if tao_price > 0 else "unavailable",
        "db_snapshots":      db_count,
        "has_delta_data":    db_count >= 2,
        "model":             MODEL,
        "version":           "1.3.0",
        "analytics_engine":  HAS_ANALYTICS,
    }

def print_report_header(title, data, src, enrichment=None):
    hdr(title)
    g = build_grounding(data, enrichment)
    tag_info(f"Snapshot  : {g['snapshot_time']}")
    tag_info(f"Block     : {g['block_height']}")
    tag_info(f"TAO/USD   : {g['tao_price_usd']}")
    tag_info(f"Source    : {src}")
    tag_info(f"Subnets   : {g['subnets_analyzed']} | Coverage: {g['metadata_coverage']}")
    tag_info(f"DB snaps  : {g['db_snapshots']} | Delta: {'YES' if g['has_delta_data'] else 'NO'}")
    tag_info(f"Analytics : {'ON' if g['analytics_engine'] else 'OFF'}")
    sep()
    print()

# ── LLM ──────────────────────────────────────────────────────────────────────
SYSTEM = """You are TaoScout, a Bittensor operator intelligence assistant.

STRICT OUTPUT FORMAT - every response must use this exactly:

FINDING: [one sentence direct answer]
BASIS: [exact data used - subnet IDs, scores, block height, TAO/USD if relevant]
CONFIDENCE: [HIGH / MEDIUM / LOW] - [one sentence reason]
GAPS: [what you cannot verify or what data is missing]

STRICT RULES:
1. All rankings and scores are pre-computed by Python analytics engine. Never recalculate.
2. Scores are 0-100. Combined score = opportunity x hardware fit.
3. Historical/trend questions: check SNAPSHOT_COUNT. If <2 say "Insufficient snapshots for trend data."
4. Hardware fit: only use scores from analytics summary. Never guess.
5. If type=UNVERIFIED or workload=UNVERIFIED say so explicitly.
6. USD values: only include if TAO/USD is in context.
7. Always cite block height and snapshot time in BASIS.
8. HIGH confidence = deterministic metric. MEDIUM = partial data. LOW = inference only.

GPU classes available: 24GB (3090/4090/6000Ada), 32GB (5090), 48GB (6000/A6000), 96GB (Pro6000 Blackwell)

Operator context: Runs SN54 (MIID identity, 3090 GPU) and SN39 (Cathedral compute rental)."""

DISCLAIMER_TEXT = (
    "\n" + "-"*64 + "\n"
    "WARNING: TaoScout is automated. Output is informational only.\n"
    "Not financial or operational advice. Verify independently.\n"
    "Scores are algorithmic estimates, not guarantees.\n"
    + "-"*64 + "\n"
)

def ollama_ok():
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3)
        return True
    except: return False

def chat(messages, silent=False):
    payload = json.dumps({"model": MODEL, "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    full = ""
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            for line in r:
                line = line.strip()
                if not line: continue
                try:
                    chunk = json.loads(line)
                    t = chunk.get("message", {}).get("content", "")
                    if not silent: print(t, end="", flush=True)
                    full += t
                    if chunk.get("done"): break
                except: pass
        if not silent: print()
    except Exception as e:
        if not silent: print(f"\n[Ollama error: {e}]")
    return full

def ask(question, context="", print_disclaimer=True, silent=False, intent=""):
    content = f"{context}\n\n---\n\nQuestion: {question}" if context else question
    result = chat_routed([
        {"role": "system", "content": SYSTEM},
        {"role": "user",   "content": content}
    ], intent=intent, silent=silent)
    if print_disclaimer and not silent:
        print(DISCLAIMER_TEXT)
    return result

def parse_structured(text):
    out = {"finding": "", "basis": "", "confidence": "", "gaps": "", "raw": text}
    current = None
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("FINDING:"):
            current = "finding"; out["finding"] = line[8:].strip()
        elif line.startswith("BASIS:"):
            current = "basis";   out["basis"]   = line[6:].strip()
        elif line.startswith("CONFIDENCE:"):
            current = "confidence"; out["confidence"] = line[11:].strip()
        elif line.startswith("GAPS:"):
            current = "gaps";    out["gaps"]    = line[5:].strip()
        elif current and line:
            out[current] += " " + line
    return out

# ── API functions ─────────────────────────────────────────────────────────────
def api_ask(question, force=False):
    d, src = get_chain_data(force)
    if not d: return {"error": f"No chain data: {src}"}
    enrich = load_enrichment()
    try:
        import query_router as QR
        import db as DB
        movers     = DB.get_movers(hours=20, top_n=10) if DB.get_snapshot_count() >= 2 else []
        snap_count = DB.get_snapshot_count()
        payload    = QR.route(question, d, enrich, MY_SUBNETS, snap_count, movers)
        try:
            import formatter
            formatted_table = formatter.format_payload(payload)
        except Exception as e:
            formatted_table = None
        format_prompt = QR.build_format_prompt(question, payload)
        raw = ask(format_prompt, context="", print_disclaimer=False, silent=True)
        valid, issues = QR.validate_response(raw, payload)
        parsed = parse_structured(raw)
        parsed["router_intent"]      = payload.get("detected_intent")
        parsed["fields_used"]        = payload.get("fields_used", [])
        parsed["computation"]        = payload.get("computation", "")
        parsed["validation_ok"]      = valid
        parsed["validation_issues"]  = issues
        parsed["formatted_table"]    = formatted_table
        parsed["result_count"]       = payload.get("result_count", 0)
        parsed["results"]            = payload.get("results", [])
        if not valid:
            parsed["warning"] = f"Validation issues: {issues}"
        parsed.update(build_grounding(d, enrich))
        parsed["disclaimer"] = "TaoScout is automated. Informational only. Not financial advice."
        return parsed
    except Exception as e:
        ctx = build_context(d, enrichment=enrich)
        raw = ask(question, ctx, print_disclaimer=False, silent=True)
        parsed = parse_structured(raw)
        parsed["router_error"] = str(e)
        parsed.update(build_grounding(d, enrich))
        parsed["disclaimer"] = "TaoScout is automated. Informational only. Not financial advice."
        return parsed

def api_brief(force=False):
    d, src = get_chain_data(force)
    if not d: return {"error": f"No chain data: {src}"}
    enrich = load_enrichment()
    try:
        import query_router as QR
        import formatter
        import db as DB
        import risk_engine as RE
        subnets    = d.get("subnets", [])
        tao_usd    = enrich.get("tao_price_usd", 0)
        movers     = DB.get_movers(hours=20, top_n=10) if DB.get_snapshot_count() >= 2 else []
        snap_count = DB.get_snapshot_count()
        em_payload     = QR.answer_top_emission(subnets, tao_usd, top_n=5)
        gpu_payload    = QR.answer_gpu_ranking(subnets, "24gb", tao_usd, movers, top_n=5)
        risk_payload   = RE.build_risk_payload(subnets, movers, tao_usd, MY_SUBNETS, snap_count)
        my_payload     = QR.answer_my_subnets(subnets, MY_SUBNETS, tao_usd, movers)
        movers_payload = QR.answer_movers(movers, snap_count, tao_usd)
        em_table     = formatter.format_top_emission(em_payload)
        gpu_table    = formatter.format_gpu_ranking(gpu_payload)
        risk_table   = RE.format_risk_table(risk_payload)
        movers_table = formatter.format_movers(movers_payload)
        my_table     = formatter.format_subnet_detail(my_payload)
        import participation as PART
        part_payload   = PART.build_participation_payload(hours=20)
        part_table     = PART.format_participation_table(part_payload)
        combined     = "\n\n".join([em_table, gpu_table, movers_table, part_table, my_table, risk_table])
        grounding    = build_grounding(d, enrich)
        return {
            "finding":         f"Operator brief — Block {d.get('block','?')} | TAO ${tao_usd:.2f} | {snap_count} snapshots",
            "basis":           "top_emission + gpu_24gb + movers + my_subnets + risk — all deterministic Python",
            "confidence":      "HIGH",
            "gaps":            "Movers require 4+ snapshots to calculate delta.",
            "formatted_table": combined,
            "router_intent":   "brief",
            "fields_used":     ["emission", "burn_tao", "neurons", "max_neurons", "em_pct_change"],
            "computation":     "All sections deterministic. No LLM math.",
            "validation_ok":   True,
            "disclaimer":      "TaoScout is automated. Informational only. Not financial advice.",
            **grounding,
        }
    except Exception as e:
        ctx = build_context(d, enrichment=enrich)
        raw = ask(
            "Operator brief: top 3 emission opportunities with IDs ratios USD value. "
            "Low fill subnets. Status of SN54 and SN39. One action this week.",
            ctx, print_disclaimer=False, silent=True
        )
        parsed = parse_structured(raw)
        parsed["router_error"] = str(e)
        parsed.update(build_grounding(d, enrich))
        parsed["disclaimer"] = "TaoScout is automated. Informational only. Not financial advice."
        return parsed
def api_scan(force=False):
    d, src = get_chain_data(force)
    if not d: return {"error": f"No chain data: {src}"}
    enrich   = load_enrichment()
    ctx      = build_context(d, enrichment=enrich)
    tao_price = enrich.get("tao_price_usd", 0.0)
    subnets  = d.get("subnets", [])

    if HAS_ANALYTICS:
        movers    = DB.get_movers(hours=24) if DB.get_snapshot_count() >= 2 else []
        top_opps  = AN.top_opportunities(subnets, tao_price, movers, top_n=5)
        opportunities = [{
            "netuid":           o["netuid"],
            "name":             o["name"],
            "score":            o["score"],
            "label":            o["label"],
            "emission_tao":     o["metrics"]["emission"],
            "emission_usd":     o["metrics"].get("emission_usd"),
            "burn_tao":         o["metrics"]["burn_tao"],
            "fill_pct":         o["metrics"]["fill_pct"],
            "ratio":            o["metrics"]["ratio"],
        } for o in top_opps]
    else:
        opportunities = []

    raw    = ask(
        "Scan top 5 opportunities. State score, emission, burn, fill, ratio. "
        "Flag UNVERIFIED workloads.",
        ctx, print_disclaimer=False, silent=True
    )
    parsed = parse_structured(raw)
    parsed["opportunities"] = opportunities
    parsed.update(build_grounding(d, enrich))
    parsed["disclaimer"] = "TaoScout is automated. Informational only. Not financial advice."
    return parsed

def api_rankings(gpu_class="24gb", force=False):
    d, src = get_chain_data(force)
    if not d: return {"error": f"No chain data: {src}"}
    if not HAS_ANALYTICS:
        return {"error": "Analytics engine not loaded"}
    enrich    = load_enrichment()
    tao_price = enrich.get("tao_price_usd", 0.0)
    subnets   = d.get("subnets", [])
    movers    = DB.get_movers(hours=24) if DB.get_snapshot_count() >= 2 else []

    from analytics import GPU_CLASSES
    if gpu_class not in GPU_CLASSES:
        return {"error": f"Unknown GPU class. Valid: {list(GPU_CLASSES.keys())}"}

    rankings = AN.rank_subnets_for_gpu_class(subnets, gpu_class, tao_price, movers, top_n=10)
    gpu_info = GPU_CLASSES[gpu_class]

    return {
        "gpu_class":  gpu_class,
        "gpu_label":  gpu_info["label"],
        "cards":      gpu_info["cards"],
        "rankings":   rankings,
        "snapshot_time": build_grounding(d, enrich)["snapshot_time"],
        "block_height":  build_grounding(d, enrich)["block_height"],
        "disclaimer": "Scores are algorithmic estimates. Verify hardware requirements independently.",
    }

def api_movers(hours=24):
    if not HAS_ANALYTICS:
        return {"error": "Analytics engine not loaded"}
    snap_count = DB.get_snapshot_count()
    if snap_count < 2:
        return {
            "error": "Insufficient snapshots for mover analysis",
            "snapshot_count": snap_count,
            "needed": 2,
            "message": "Need at least 2 snapshots. Cron runs every 30min."
        }
    movers = DB.get_movers(hours=hours, top_n=15)
    return {
        "window_hours": hours,
        "snapshot_count": snap_count,
        "movers": movers,
        "disclaimer": "Emission changes computed from on-chain snapshots. Not financial advice.",
    }

def api_subnet(netuid, include_github=True):
    d, src = get_chain_data()
    if not d: return {"error": f"No chain data: {src}"}
    enrich    = load_enrichment()
    tao_price = enrich.get("tao_price_usd", 0.0)
    subnets   = d.get("subnets", [])

    subnet = next((s for s in subnets if s.get("netuid") == netuid), None)
    if not subnet:
        return {"error": f"Subnet {netuid} not found in latest snapshot"}

    result = {"netuid": netuid, "data": subnet}

    if HAS_ANALYTICS:
        from analytics import GPU_CLASSES
        movers = DB.get_movers(hours=24) if DB.get_snapshot_count() >= 2 else []
        opp    = AN.opportunity_score(subnet, tao_price, movers)
        result["opportunity"] = opp
        result["gpu_fit"] = {
            cls: AN.gpu_fit_score(netuid, GPU_CLASSES[cls]["vram_gb"])
            for cls in GPU_CLASSES
        }
        history = DB.get_subnet_history(netuid, hours=24)
        result["history_24h"] = history[:10]

    if include_github:
        result["github"] = github_search_subnet(netuid, subnet.get("name", ""))

    result.update(build_grounding(d, enrich))
    result["disclaimer"] = "TaoScout is automated. Informational only. Not financial advice."
    return result

def api_status():
    age   = snapshot_age_minutes()
    _, ts = load_data("chain")
    snaps = sorted(DATA_DIR.glob("snapshot_*.json"))
    enrich, _ = load_data("enrichment")
    db_info   = DB.db_status() if HAS_ANALYTICS else {}
    return {
        "status":               "ok",
        "version":              "1.3.0",
        "model":                MODEL,
        "ollama":               ollama_ok(),
        "snapshot_time":        ts[:19].replace("T"," ")+" UTC" if ts else None,
        "snapshot_age_minutes": age,
        "snapshot_count":       len(snaps),
        "db_snapshots":         db_info.get("snapshot_count", 0),
        "has_delta_data":       db_info.get("snapshot_count", 0) >= 2,
        "metadata_coverage":    f"{sum(1 for k in META)} of 129",
        "taostats_connected":   bool(TAOSTATS_KEY),
        "github_connected":     bool(GITHUB_TOKEN),
        "analytics_engine":     HAS_ANALYTICS,
        "tao_price_usd":        enrich.get("tao_price_usd") if enrich else None,
    }

# ── CLI Modes ─────────────────────────────────────────────────────────────────
def brief(force=False):
    d, src = get_chain_data(force)
    if not d: tag_risk(f"No data: {src}"); return
    enrich = load_enrichment()
    print_report_header("DAILY OPERATOR BRIEF", d, src, enrich)
    ctx = build_context(d, enrichment=enrich)
    ask(
        "Give complete operator brief: "
        "1) top 3 scored opportunities with IDs and scores "
        "2) best subnet for 24GB GPU (3090/4090/6000Ada) "
        "3) movers if available "
        "4) SN54 and SN39 status "
        "5) one concrete action. Flag UNVERIFIED workloads.",
        ctx
    )

def scan(force=False):
    d, src = get_chain_data(force)
    if not d: tag_risk(f"No data: {src}"); return
    enrich = load_enrichment()
    print_report_header("OPPORTUNITY SCAN", d, src, enrich)
    ctx = build_context(d, enrichment=enrich)
    ask(
        "Scan top 5 opportunities by combined score. "
        "For each: score breakdown, emission TAO+USD, burn, fill, hardware fit. "
        "Flag zero-emission subnets. Flag UNVERIFIED.",
        ctx
    )

def interactive(force=False):
    banner()
    d, src = get_chain_data(force)
    enrich = load_enrichment()
    ctx    = build_context(d, enrichment=enrich) if d else ""
    if d:
        g = build_grounding(d, enrich)
        hdr("SESSION READY")
        tag_ok(f"Block     : {g['block_height']}")
        tag_ok(f"TAO/USD   : {g['tao_price_usd']}")
        tag_ok(f"DB snaps  : {g['db_snapshots']}")
        tag_ok(f"Analytics : {'ON' if g['analytics_engine'] else 'OFF (fallback mode)'}")
        tag_info(f"Delta data: {'YES' if g['has_delta_data'] else 'NO - need 2+ snapshots'}")
        sep()
        print("\n  Commands: brief | scan | refresh | quit\n")
    else:
        tag_warn("No chain data available")

    history  = []
    ctx_used = False
    while True:
        try:
            q = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye."); break
        if not q: continue
        if q.lower() in ("quit","exit","q"): break
        if q.lower() == "brief":   brief();  continue
        if q.lower() == "scan":    scan();   continue
        if q.lower() == "refresh":
            d, src = get_chain_data(True)
            enrich = load_enrichment()
            ctx    = build_context(d, enrichment=enrich) if d else ""
            ctx_used = False
            tag_ok(f"Refreshed: {src}"); continue

        full_q = f"{ctx}\n\n---\n\nQuestion: {q}" if (not ctx_used and ctx) else q
        ctx_used = True
        history.append({"role": "user", "content": full_q})
        msgs = [{"role": "system", "content": SYSTEM}] + history[-10:]
        print("\nTaoScout: ", end="")
        r = chat(msgs)
        history.append({"role": "assistant", "content": r})
        print(DISCLAIMER_TEXT)
        print()

def main():
    global MODEL
    p = argparse.ArgumentParser(description="TaoScout v1.3")
    p.add_argument("--brief",    action="store_true")
    p.add_argument("--scan",     action="store_true")
    p.add_argument("--refresh",  action="store_true")
    p.add_argument("--question", type=str)
    p.add_argument("--model",    type=str, default=MODEL)
    args = p.parse_args()
    MODEL = args.model

    if not ollama_ok():
        print("[ERROR] Ollama not reachable at 127.0.0.1:11434"); sys.exit(1)

    if args.brief:
        brief(args.refresh)
    elif args.scan:
        scan(args.refresh)
    elif args.question:
        d, src = get_chain_data(args.refresh)
        enrich = load_enrichment()
        ctx    = build_context(d, enrichment=enrich) if d else ""
        print_report_header("QUESTION", d or {}, src, enrich)
        ask(args.question, ctx)
    else:
        interactive(args.refresh)

if __name__ == "__main__":
    main()


# ── Multi-GPU Router ──────────────────────────────────────────────────────────
HELPER_URL   = CFG.get("ollama_helper_url", OLLAMA_URL)
WORKER_URL   = CFG.get("ollama_worker_url", OLLAMA_URL)
HELPER_MODEL = CFG.get("helper_model", MODEL)
WORKER_MODEL = CFG.get("worker_model", MODEL)

HELPER_INTENTS = {"classify", "route", "intent", "short", "extract"}
WORKER_INTENTS = {"brief", "batch", "daily", "scan", "compare", "background", "summary"}

def route_ollama(intent=""):
    i = (intent or "").lower()
    if any(k in i for k in HELPER_INTENTS):
        return HELPER_URL, HELPER_MODEL
    if any(k in i for k in WORKER_INTENTS):
        return WORKER_URL, WORKER_MODEL
    return OLLAMA_URL, MODEL

def chat_routed(messages, intent="", silent=False):
    url, model = route_ollama(intent)
    payload = json.dumps({
        "model": model, "messages": messages, "stream": True
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    full = ""
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            for line in r:
                line = line.strip()
                if not line: continue
                try:
                    chunk = json.loads(line)
                    t = chunk.get("message", {}).get("content", "")
                    if not silent: print(t, end="", flush=True)
                    full += t
                    if chunk.get("done"): break
                except: pass
        if not silent: print()
    except Exception as e:
        if not silent: print(f"\n[Ollama error on {url}: {e}]")
    return full
