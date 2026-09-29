"""python -m market --help"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from .engine import Rules, read_csv, replay, timestamp
from .ledger import history, publish


def audit_snapshots(path):
    """Read the existing production DB without creating/modifying it."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        count, first, last, priced = conn.execute('''SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at),
            SUM(CASE WHEN tao_price_usd > 0 THEN 1 ELSE 0 END) FROM snapshots''').fetchone()
        return {"snapshot_count": count, "first_timestamp": first, "last_timestamp": last,
                "positive_price_rows": priced or 0, "eligible_for_price_backtest": False,
                "reason": "Legacy schema has no price observation time, OHLC, or import provenance. Cached prices and rewritten import timestamps may exist. Do not treat these as exchange candles."}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="TaoScout research foundation; never places trades")
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit-snapshots")
    audit.add_argument("--db", required=True)
    backtest = commands.add_parser("backtest")
    backtest.add_argument("--csv", required=True)
    backtest.add_argument("--source", required=True)
    backtest.add_argument("--evaluation-start", required=True)
    backtest.add_argument("--fee-bps", type=float, default=10)
    backtest.add_argument("--slippage-bps", type=float, default=10)
    backtest.add_argument("--output", required=True)
    live = commands.add_parser("publish")
    live.add_argument("--csv", required=True)
    live.add_argument("--source", required=True)
    live.add_argument("--ledger", default="data/market-research.db")
    inspect = commands.add_parser("history")
    inspect.add_argument("--ledger", default="data/market-research.db")
    inspect.add_argument("--before", type=int)
    args = parser.parse_args()
    try:
        if args.command == "audit-snapshots":
            result = audit_snapshots(args.db)
        elif args.command == "backtest":
            bars = read_csv(args.csv)
            if bars[-1].end > datetime.now(timezone.utc):
                raise ValueError("Dataset contains future or incomplete candles")
            result = replay(bars, args.evaluation_start, args.source,
                            Rules(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps))
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Explicit output path; exclusive creation prevents replacing a prior report.
            with destination.open("x") as handle:
                json.dump(result, handle, indent=2, allow_nan=False)
            result = {k: v for k, v in result.items() if k not in ("signals", "trades", "equity")}
            result["saved_to"] = str(destination)
        elif args.command == "publish":
            payload, created = publish(args.ledger, read_csv(args.csv), args.source)
            result = {"created": created, "record": payload}
        else:
            result = history(args.ledger, before=args.before)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Research unavailable: {exc}\n")


if __name__ == "__main__":
    main()
