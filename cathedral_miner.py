"""
TaoScout Cathedral Miner — v1.0
Mines regulatory intelligence cards on Cathedral/SN39.
Fetches sources, synthesizes Card JSON via Ollama, signs with hotkey, submits.
"""
import json, time, hashlib, zipfile, io, base64, urllib.request, urllib.parse
import subprocess, tempfile, os, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PUBLISHER_URL  = "https://api.cathedral.computer"
SKILL_URL      = f"{PUBLISHER_URL}/skill.md"
OLLAMA_URL     = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL   = "llama3.1:latest"

# Your wallet config
WALLET_NAME    = "tao_wallet"
HOTKEY_NAME    = "sn39_miner"
HOTKEY_SS58    = "5G9byaaFsMrDgutGqcsrRCDpDnqCmUEChZ3WFWSLQp9szbPH"
DISPLAY_NAME   = "TaoScout-SN39"
BIO            = "Bittensor operator intelligence agent by McDEE — Tennessee"

# Cards to mine (start with one)
CARDS = ["eu-ai-act", "us-ai-eo", "uk-ai-whitepaper"]

def fetch_url(url, timeout=30):
    """Fetch a URL and return (content_bytes, status_code, resolved_url)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TaoScout-Cathedral/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            # Follow 202 redirect pattern for EUR-Lex
            if r.status == 202 and not data:
                time.sleep(2)
                with urllib.request.urlopen(req, timeout=timeout) as r2:
                    return r2.read(), r2.status, r2.url
            return data, r.status, r.url
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, 'read') else b""
        return body, e.code, url
    except Exception as e:
        return b"", 0, url

def blake3_hex(data: bytes) -> str:
    """Compute BLAKE3 hash of bytes. Falls back to SHA256 if blake3 not installed."""
    try:
        import blake3
        return blake3.blake3(data).hexdigest()
    except ImportError:
        # Fallback to sha256 — note: validator may reject this
        return hashlib.sha256(data).hexdigest()

def get_eval_spec(card_id: str) -> dict:
    """Fetch eval spec for a card."""
    url = f"{PUBLISHER_URL}/api/cathedral/v1/cards/{card_id}/eval-spec"
    data, status, _ = fetch_url(url)
    if status == 200:
        return json.loads(data.decode())
    return {}

def fetch_sources(source_pool: list, max_sources=8) -> list:
    """Fetch source URLs and compute BLAKE3 hashes. Prioritize regulator/government over listing pages."""
    # Sort: regulator and government first, official_journal listing pages last
    priority = {"regulator": 0, "government": 1, "parliament": 2, "law_text": 3, "official_journal": 4, "other": 5}
    sorted_pool = sorted(source_pool, key=lambda x: priority.get(x.get("class","other"), 5))
    citations = []
    for src in sorted_pool[:max_sources]:
        url = src.get("url", "")
        cls = src.get("class", "other")
        print(f"  Fetching: {url[:60]}...")
        content, status, resolved = fetch_url(url, timeout=15)
        content_hash = blake3_hex(content) if content else ""
        citations.append({
            "url":          resolved or url,
            "class":        cls,
            "fetched_at":   datetime.now(timezone.utc).isoformat(),
            "status":       status,
            "content_hash": content_hash,
            "_content":     content[:8000].decode("utf-8", errors="replace") if content else "",
        })
        time.sleep(0.5)
    return citations

def synthesize_card(card_id: str, spec: dict, citations: list, task: str) -> dict:
    """Use Ollama to synthesize a Card JSON from fetched sources."""
    jurisdiction_map = {
        "eu-ai-act":        "eu",
        "us-ai-eo":         "us",
        "uk-ai-whitepaper": "uk",
        "singapore-pdpc":   "sg",
        "japan-meti-mic":   "jp",
    }
    jurisdiction = jurisdiction_map.get(card_id, "other")
    topic        = spec.get("display_name", card_id)

    # Build source context
    source_context = ""
    for c in citations[:2]:
        if c.get("_content"):
            source_context += f"\n\nSOURCE [{c['class']}] {c['url']}:\n{c['_content'][:800]}"

    prompt = f"""You are TaoScout, a regulatory intelligence analyst. Using the source content below, produce a regulatory intelligence card.

Job: {task}
Topic: {topic}
Jurisdiction: {jurisdiction}

IMPORTANT: Even if no breaking news exists today, summarize the most material CURRENT obligations, requirements, or guidance from these sources. Never say "no developments found". Always produce substantive content.

Sources:
{source_context}

Produce a JSON object with EXACTLY these fields:
- "jurisdiction": "{jurisdiction}"
- "topic": "{topic}"
- "title": headline summary of most material development (under 120 chars)
- "summary": 80-800 chars, 1-6 sentences, plain English, what happened
- "what_changed": concrete change since last refresh, specific
- "why_it_matters": who is affected and what the implication is
- "action_notes": what a compliance officer should do this week
- "risks": material penalties, deadlines, exposure
- "confidence": float 0.0-1.0 based on source quality
- "no_legal_advice": true
- "last_refreshed_at": "{datetime.now(timezone.utc).isoformat()}"
- "refresh_cadence_hours": 24

Return ONLY the JSON object. No preamble. No explanation. No markdown."""

    payload = json.dumps({
        "model":    OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream":   False
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.loads(r.read().decode())
            text = resp["message"]["content"].strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
    except Exception as e:
        print(f"  Ollama error: {e}")
        return {}

def build_card(card_id: str, spec: dict, citations: list, synthesized: dict) -> dict:
    """Build final Card JSON."""
    clean_citations = []
    for c in citations:
        cc = {k: v for k, v in c.items() if k != "_content"}
        clean_citations.append(cc)

    def nonempty(val, fallback):
        if val and str(val).strip() and str(val).lower() not in ("null","none","n/a"):
            return str(val).strip()
        return fallback

    card = {
        "jurisdiction":        synthesized.get("jurisdiction", "other"),
        "topic":               synthesized.get("topic", card_id),
        "title":               nonempty(synthesized.get("title"), "Regulatory intelligence update"),
        "summary":             nonempty(synthesized.get("summary"), "Regulatory sources reviewed. No material changes detected in the current refresh window."),
        "what_changed":        nonempty(synthesized.get("what_changed"), "No significant changes detected in this refresh cycle."),
        "why_it_matters":      nonempty(synthesized.get("why_it_matters"), "Ongoing monitoring of regulatory developments is recommended."),
        "action_notes":        nonempty(synthesized.get("action_notes"), "Continue monitoring official sources for updates."),
        "risks":               nonempty(
            str(synthesized.get("risks", "")).replace("'", '"') 
            if not isinstance(synthesized.get("risks"), str) 
            else synthesized.get("risks"),
            "No immediate risks identified in current sources."
        ),
        "citations":           clean_citations,
        "confidence":          float(synthesized.get("confidence", 0.7)),
        "no_legal_advice":     True,
        "last_refreshed_at":   synthesized.get("last_refreshed_at",
                                datetime.now(timezone.utc).isoformat()),
        "refresh_cadence_hours": 24,
    }
    return card

def sign_submission(bundle_hash: str, card_id: str) -> tuple:
    """Sign submission. Returns (signature_b64, submitted_at) tuple."""
    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    payload = json.dumps({
        "bundle_hash":   bundle_hash,
        "card_id":       card_id,
        "miner_hotkey":  HOTKEY_SS58,
        "submitted_at":  submitted_at,
    }, sort_keys=True, separators=(",", ":"))

    print(f"  submitted_at: {submitted_at}")
    print(f"  Signing payload: {payload}")

    try:
        from substrateinterface import Keypair
        hotkey_path = Path.home() / ".bittensor/wallets" / WALLET_NAME / "hotkeys" / HOTKEY_NAME
        with open(hotkey_path) as f:
            hotkey_data = json.load(f)
        keypair = Keypair.create_from_mnemonic(hotkey_data["secretPhrase"])
        print(f"  Keypair ss58: {keypair.ss58_address}")
        canonical_bytes = payload.encode("utf-8")
        print(f"  Payload bytes ({len(canonical_bytes)}): {canonical_bytes[:60]}")
        sig = keypair.sign(canonical_bytes)
        result = base64.b64encode(bytes(sig)).decode()
        print(f"  Signature ({len(bytes(sig))} bytes): {result[:30]}...")
        return result, submitted_at
    except Exception as e:
        print(f"  Sign error: {e}")
        return "", ""

def build_bundle(card: dict) -> tuple:
    """Build Hermes-shaped agent bundle zip."""
    soul_md = f"""# TaoScout Cathedral Agent

You are TaoScout, a Bittensor operator intelligence agent specializing in regulatory intelligence.

## Current Task
Produce a regulatory intelligence card for: {card.get('topic', 'regulatory intelligence')}

## Output Format
Return a Card JSON matching the Cathedral schema exactly.

## Style
- Cite only official sources
- Be specific about what changed
- Focus on actionable compliance implications
- Keep summary 80-800 characters
"""
    agents_md = """# TaoScout Agent Index

## Skills
- regulatory_intelligence: EU AI Act, US AI EO, UK AI Whitepaper, Singapore PDPC, Japan METI/MIC
- source_fetching: Fetch and hash regulatory documents
- card_synthesis: Synthesize structured Card JSON from sources
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("soul.md",   soul_md)
        zf.writestr("AGENTS.md", agents_md)
        zf.writestr("card.json", json.dumps(card, indent=2))
    bundle_bytes = buf.getvalue()
    bundle_hash  = blake3_hex(bundle_bytes)
    return bundle_bytes, bundle_hash

def submit_card(card_id: str, card: dict, bundle_bytes: bytes,
                bundle_hash: str, signature: str, submitted_at: str) -> dict:
    """Submit card bundle to Cathedral publisher using requests for exact multipart."""
    import requests

    # Write bundle to temp file for exact byte submission
    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.write(bundle_bytes)
    tmp.flush()
    tmp.close()

    try:
        # Verify local hash matches what we will send
        try:
            import blake3 as bk
            verify_hash = bk.blake3(bundle_bytes).hexdigest()
        except ImportError:
            import hashlib
            verify_hash = hashlib.sha256(bundle_bytes).hexdigest()

        print(f"  Pre-send hash verify: {verify_hash[:16]}... == {bundle_hash[:16]}... match={verify_hash==bundle_hash}")

        with open(tmp.name, "rb") as f:
            zip_bytes_verify = f.read()
        print(f"  File bytes match: {zip_bytes_verify == bundle_bytes}")

        resp = requests.post(
            f"{PUBLISHER_URL}/v1/agents/submit",
            headers={
                "X-Cathedral-Signature": signature,
                "X-Cathedral-Hotkey":    HOTKEY_SS58,
            },
            data={
                "card_id":          card_id,
                "display_name":     DISPLAY_NAME,
                "bio":              BIO,
                "attestation_mode": "polaris",
                "submitted_at":     submitted_at,
            },
            files={
                "bundle": ("agent.zip", zip_bytes_verify, "application/zip"),
            },
            timeout=120
        )
        print(f"  Response headers: {dict(resp.headers).get('content-type')}")
        try:
            return resp.json(), resp.status_code
        except Exception:
            return {"error": resp.text}, resp.status_code
    except Exception as e:
        return {"error": str(e)}, 0
    finally:
        os.unlink(tmp.name)

def mine_card(card_id: str, dry_run=False):
    """Full mining loop for one card."""
    print(f"\n{'='*60}")
    print(f"Mining: {card_id}")
    print(f"{'='*60}")

    # 1. Get eval spec
    print("Fetching eval spec...")
    spec = get_eval_spec(card_id)
    if not spec:
        print(f"ERROR: Could not fetch eval spec for {card_id}")
        return None

    task = spec.get("task_templates", ["Summarize recent developments."])[0]
    print(f"Task: {task[:80]}")

    # 2. Fetch sources
    print("\nFetching sources...")
    source_pool = spec.get("source_pool", [])
    citations   = fetch_sources(source_pool, max_sources=5)
    print(f"Fetched {len(citations)} sources")

    # 3. Synthesize card
    print("\nSynthesizing card via Ollama...")
    synthesized = synthesize_card(card_id, spec, citations, task)
    if not synthesized:
        print("ERROR: Card synthesis failed")
        return None

    # 4. Build card
    card = build_card(card_id, spec, citations, synthesized)
    print(f"Card built: {card['title'][:60]}")
    print(f"Summary length: {len(card['summary'])} chars")
    print(f"Citations: {len(card['citations'])}")
    print(f"Confidence: {card['confidence']}")

    if dry_run:
        print("\n[DRY RUN] Card JSON:")
        print(json.dumps(card, indent=2)[:2000])
        return card

    # 5. Build bundle
    print("\nBuilding bundle...")
    bundle_bytes, bundle_hash = build_bundle(card)
    print(f"Bundle: {len(bundle_bytes)} bytes | hash: {bundle_hash[:16]}...")

    # 6. Sign
    print("Signing submission...")
    signature, submitted_at = sign_submission(bundle_hash, card_id)
    if not signature:
        print("ERROR: Signing failed")
        return None
    print(f"Signed: {signature[:20]}...")

    # 7. Submit
    print("Submitting to Cathedral publisher...")
    result, status = submit_card(card_id, card, bundle_bytes, bundle_hash, signature, submitted_at)
    print(f"HTTP {status}: {json.dumps(result, indent=2)[:500]}")

    return result

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="TaoScout Cathedral Miner")
    p.add_argument("--card",    default="eu-ai-act", help="Card ID to mine")
    p.add_argument("--dry-run", action="store_true",  help="Synthesize but do not submit")
    p.add_argument("--all",     action="store_true",  help="Mine all cards")
    args = p.parse_args()

    if args.all:
        for card_id in CARDS:
            mine_card(card_id, dry_run=args.dry_run)
            time.sleep(5)
    else:
        mine_card(args.card, dry_run=args.dry_run)
