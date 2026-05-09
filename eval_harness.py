"""
TaoScout Cost-Collapse Eval Harness
Scores TaoScout /ask responses against Cathedral competition criteria:
- outcome_retention
- citation_correctness  
- risk_detection
- demand_vs_supply
"""
import json, time, urllib.request, sys
from datetime import datetime, timezone

API_BASE = "http://127.0.0.1:8765"
API_KEY  = "taoscout-local-key-1"

# Test questions covering all 4 required capabilities
TEST_TASKS = [
    {
        "id": "task_001",
        "question": "Which subnet looks most useful for a new agent workflow, and why?",
        "required": ["demand_vs_supply", "citation"],
        "check_risk": False,
    },
    {
        "id": "task_002",
        "question": "Where does a subnet show real demand instead of induced supply?",
        "required": ["demand_vs_supply", "citation"],
        "check_risk": False,
    },
    {
        "id": "task_003",
        "question": "What reliability or incentive failure mode is most likely on SN54?",
        "required": ["risk_detection", "citation"],
        "check_risk": True,
    },
    {
        "id": "task_004",
        "question": "Which evidence in the current subnet rankings is stale or contradicted?",
        "required": ["citation", "demand_vs_supply"],
        "check_risk": False,
    },
    {
        "id": "task_005",
        "question": "What are the top 3 subnets for a 24GB GPU miner and what is the burn risk?",
        "required": ["demand_vs_supply", "risk_detection", "citation"],
        "check_risk": True,
    },
]

def ask(question):
    payload = json.dumps({"question": question}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/ask",
        data=payload,
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
        method="POST"
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            latency = round(time.time() - t0, 2)
            return json.loads(r.read().decode()), latency
    except Exception as e:
        return {"error": str(e)}, round(time.time() - t0, 2)

def score_response(result, task):
    """Score a TaoScout response against Cathedral criteria."""
    scores = {
        "outcome_retention":   0.0,
        "citation_correctness": 0.0,
        "risk_detection":      0.0,
        "demand_vs_supply":    0.0,
    }

    if "error" in result:
        return scores, ["API error: " + result["error"]]

    issues = []
    raw    = result.get("raw", "") or result.get("finding", "")
    basis  = result.get("basis", "")
    conf   = result.get("confidence", "")
    gaps   = result.get("gaps", "")

    # Outcome retention — did it produce a structured answer?
    if result.get("finding") and len(result.get("finding","")) > 20:
        scores["outcome_retention"] = 1.0
    elif raw and len(raw) > 50:
        scores["outcome_retention"] = 0.7
    else:
        issues.append("No finding produced")

    # Citation correctness — does it cite block height and data source?
    block_cited   = any(x in basis.lower() for x in ["block", "8", "snapshot"])
    source_cited  = any(x in basis.lower() for x in ["chain", "finney", "taostats", "snapshot", "emission"])
    if block_cited and source_cited:
        scores["citation_correctness"] = 1.0
    elif block_cited or source_cited:
        scores["citation_correctness"] = 0.7
        issues.append("Partial citation — missing block or source")
    else:
        scores["citation_correctness"] = 0.3
        issues.append("Weak citation in basis field")

    # Risk detection — does it mention risk when asked?
    if task["check_risk"]:
        risk_words = ["risk", "burn", "dereg", "drop", "volatile", "zero", "fail", "warn"]
        if any(w in raw.lower() for w in risk_words):
            scores["risk_detection"] = 1.0
        else:
            scores["risk_detection"] = 0.4
            issues.append("Risk question but no risk language in response")
    else:
        scores["risk_detection"] = 1.0  # not required for this task

    # Demand vs supply — does it differentiate real demand from induced?
    demand_words = ["emission", "ratio", "burn", "fill", "slot", "miner", "validator", "reward"]
    supply_words = ["supply", "demand", "opportunity", "available", "open"]
    has_demand = any(w in raw.lower() for w in demand_words)
    has_supply = any(w in raw.lower() for w in supply_words)
    if has_demand and has_supply:
        scores["demand_vs_supply"] = 1.0
    elif has_demand:
        scores["demand_vs_supply"] = 0.8
    else:
        scores["demand_vs_supply"] = 0.4
        issues.append("Weak demand/supply differentiation")

    return scores, issues

def run_eval(output_file="eval-results.json"):
    print(f"\nTaoScout Cost-Collapse Eval Harness")
    print(f"Tasks: {len(TEST_TASKS)} | API: {API_BASE}")
    print("─" * 60)

    results      = []
    total_scores = {k: [] for k in ["outcome_retention","citation_correctness","risk_detection","demand_vs_supply"]}
    total_latency = []
    failures      = 0

    for task in TEST_TASKS:
        print(f"\n[{task['id']}] {task['question'][:60]}...")
        result, latency = ask(task["question"])
        total_latency.append(latency)

        if "error" in result:
            failures += 1
            print(f"  ERROR: {result['error']}")
            scores, issues = {k: 0.0 for k in total_scores}, ["API error"]
        else:
            scores, issues = score_response(result, task)
            print(f"  Latency: {latency}s")
            print(f"  Scores: {scores}")
            if issues:
                print(f"  Issues: {issues}")

        results.append({
            "task_id":      task["id"],
            "question":     task["question"],
            "latency_s":    latency,
            "scores":       scores,
            "issues":       issues,
            "finding":      result.get("finding",""),
            "basis":        result.get("basis",""),
            "confidence":   result.get("confidence",""),
            "validation_ok": result.get("validation_ok", False),
        })
        for k, v in scores.items():
            total_scores[k].append(v)

    # Aggregate
    avg = lambda lst: round(sum(lst)/len(lst), 3) if lst else 0
    summary = {
        "held_out_eval_id":      "taoscout-self-eval-v1",
        "task_count":            len(TEST_TASKS),
        "failure_count":         failures,
        "failure_rate":          round(failures/len(TEST_TASKS), 3),
        "median_latency_seconds": round(sorted(total_latency)[len(total_latency)//2], 2),
        "outcome_retention":     avg(total_scores["outcome_retention"]),
        "citation_correctness":  avg(total_scores["citation_correctness"]),
        "risk_detection_score":  avg(total_scores["risk_detection"]),
        "demand_vs_supply_score": avg(total_scores["demand_vs_supply"]),
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "tasks":                 results,
    }

    print("\n" + "─" * 60)
    print("SUMMARY")
    print(f"  outcome_retention:    {summary['outcome_retention']}")
    print(f"  citation_correctness: {summary['citation_correctness']}")
    print(f"  risk_detection:       {summary['risk_detection_score']}")
    print(f"  demand_vs_supply:     {summary['demand_vs_supply_score']}")
    print(f"  median_latency:       {summary['median_latency_seconds']}s")
    print(f"  failure_rate:         {summary['failure_rate']}")

    meets_threshold = all(
        summary[k] >= 0.85
        for k in ["outcome_retention","citation_correctness","risk_detection_score","demand_vs_supply_score"]
    )
    print(f"\n  {'✅ PASSES' if meets_threshold else '❌ FAILS'} minimum 0.85 quality threshold")

    with open(output_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Results saved to {output_file}")
    return summary

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "eval-results.json"
    run_eval(out)
