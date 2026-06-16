#!/usr/bin/env python3
"""
TaoScout TaoStats Enrichment v2.0
Pulls live subnet data from TaoStats API for all 129 subnets.
Adds validator counts, projected emission, burn, alpha flow to chain data.
"""
import json, requests, sys
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent
CFG      = json.loads((BASE_DIR / "config.json").read_text())
API_KEY  = CFG.get("taostats_api_key", "")

def fetch_all_subnets():
    url = "https://api.taostats.io/api/subnet/latest/v1?limit=200"
    r = requests.get(url, headers={"Authorization": API_KEY}, timeout=15)
    r.raise_for_status()
    return {str(s["netuid"]): s for s in r.json().get("data", [])}

def enrich_chain_data():
    chain_file = BASE_DIR / "data" / "chain.json"
    if not chain_file.exists():
        print("No chain.json found")
        return False

    chain = json.loads(chain_file.read_text())
    subnets = chain.get("data", {}).get("subnets", [])

    print(f"Fetching TaoStats data for all subnets...")
    ts_data = fetch_all_subnets()
    print(f"Got {len(ts_data)} subnets from TaoStats")

    enriched = 0
    for s in subnets:
        nid = str(s["netuid"])
        ts  = ts_data.get(nid, {})
        if not ts:
            continue

        # Validator data
        s["validators"]        = ts.get("validators", 0)
        s["active_validators"] = ts.get("active_validators", 0)
        s["active_miners"]     = ts.get("active_miners", 0)
        s["active_keys"]       = ts.get("active_keys", s.get("neurons", 0))
        s["max_validators"]    = ts.get("max_validators", 64)

        # Better emission (projected in TAO)
        proj = ts.get("projected_emission", "0")
        try:
            s["emission_projected"] = float(proj)
        except Exception:
            s["emission_projected"] = 0.0

        # Burn costs in TAO
        reg_cost = ts.get("registration_cost", "0")
        try:
            s["burn_tao_taostats"] = int(reg_cost) / 1e9
        except Exception:
            pass

        neuron_cost = ts.get("neuron_registration_cost", "0")
        try:
            s["neuron_reg_cost_tao"] = int(neuron_cost) / 1e9
        except Exception:
            pass

        # Alpha flow (momentum signal)
        try:
            s["net_flow_1d"]  = float(ts.get("net_flow_1_day", 0) or 0) / 1e9
            s["net_flow_7d"]  = float(ts.get("net_flow_7_days", 0) or 0) / 1e9
            s["net_flow_30d"] = float(ts.get("net_flow_30_days", 0) or 0) / 1e9
        except Exception:
            pass

        # Blocks timing
        s["blocks_until_epoch"]      = ts.get("blocks_until_next_epoch", 0)
        s["blocks_since_last_step"]  = ts.get("blocks_since_last_step", 0)
        s["registration_allowed"]    = ts.get("registration_allowed", True)
        s["pow_registration_allowed"]= ts.get("pow_registration_allowed", False)

        # Alpha price from chain (already in s["price"]) — keep as-is
        # But add alpha high/low
        try:
            s["alpha_high"] = float(ts.get("alpha_high", 0) or 0) / 1e9
            s["alpha_low"]  = float(ts.get("alpha_low", 0) or 0) / 1e9
        except Exception:
            pass

        # Registrations this interval
        s["regs_this_interval"] = ts.get("neuron_registrations_this_interval", 0)

        enriched += 1

    chain["data"]["subnets"]         = subnets
    chain["data"]["taostats_enriched_at"] = datetime.now(timezone.utc).isoformat()
    chain_file.write_text(json.dumps(chain))

    print(f"Enriched {enriched}/129 subnets with TaoStats validator + flow data")
    return True

if __name__ == "__main__":
    try:
        success = enrich_chain_data()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"Enrichment failed: {e}")
        sys.exit(1)
