"""Deterministic, hourly TAO/USD baseline. Python standard library only."""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

VERSION = "tao-spot-trend-v1"
HOUR = timedelta(hours=1)


def timestamp(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return dt.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Candle:
    start: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def end(self):
        return self.start + HOUR

    def record(self):
        return {"start": iso(self.start), "available_at": iso(self.available_at),
                "open": self.open, "high": self.high, "low": self.low, "close": self.close}


@dataclass(frozen=True)
class Rules:
    fast_hours: int = 24
    slow_hours: int = 72
    update_hours: int = 8
    fee_bps: float = 10
    slippage_bps: float = 10

    def __post_init__(self):
        if not 1 <= self.fast_hours < self.slow_hours or self.slow_hours > 8760:
            raise ValueError("Require 1 <= fast_hours < slow_hours <= 8760")
        if self.update_hours not in (8, 12, 24):
            raise ValueError("Updates must be every 8, 12, or 24 UTC hours")
        for cost in (self.fee_bps, self.slippage_bps):
            if not math.isfinite(cost) or not 0 <= cost <= 1000:
                raise ValueError("Costs must be finite and between 0 and 1000 basis points")

    @property
    def cost(self):
        return (self.fee_bps + self.slippage_bps) / 10000


def validate(candles):
    if not candles:
        raise ValueError("No candles supplied")
    previous = None
    for bar in candles:
        if bar.start.tzinfo is None or bar.available_at.tzinfo is None:
            raise ValueError("Timezone required")
        if bar.start.minute or bar.start.second or bar.start.microsecond:
            raise ValueError("Candle starts must be on UTC hour boundaries")
        if not all(math.isfinite(p) and p > 0 for p in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError("OHLC prices must be positive and finite")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("Invalid OHLC bounds")
        if bar.available_at < bar.end:
            raise ValueError("A completed candle cannot be available before its close")
        if previous and bar.start != previous.start + HOUR:
            raise ValueError("Require ordered, unique, contiguous hourly candles; do not fill gaps")
        previous = bar
    return candles


def read_csv(path):
    with Path(path).open(newline="") as handle:
        rows = csv.DictReader(handle)
        required = {"start", "available_at", "open", "high", "low", "close"}
        if not required.issubset(rows.fieldnames or []):
            raise ValueError("CSV requires: " + ", ".join(sorted(required)))
        bars = [Candle(timestamp(r["start"]), timestamp(r["available_at"]),
                       *(float(r[k]) for k in ("open", "high", "low", "close"))) for r in rows]
    return validate(bars)


def dataset_hash(bars):
    return hashlib.sha256(canonical([b.record() for b in bars]).encode()).hexdigest()


def condition(bars, at, rules=Rules()):
    """Only the exact trailing window of completed AND available candles is used."""
    if at.tzinfo is None:
        raise ValueError("Decision time must include timezone")
    at = at.astimezone(timezone.utc)
    closed_hour = at.replace(minute=0, second=0, microsecond=0)
    first = closed_hour - timedelta(hours=rules.slow_hours)
    window = [b for b in bars if first <= b.start < closed_hour]
    result = {"version": VERSION, "as_of": iso(at), "condition": "no_clear_signal",
              "confidence": None, "confidence_note": "No calibrated win probability available.",
              "reason": "Insufficient, stale, or not-yet-available hourly history",
              "price": None, "fast_average": None, "slow_average": None,
              "support": None, "invalidation": None}
    if len(window) != rules.slow_hours or any(b.available_at > at for b in window):
        return result
    if any(b.start != first + i * HOUR for i, b in enumerate(window)):
        return result
    fast = mean(b.close for b in window[-rules.fast_hours:])
    slow = mean(b.close for b in window)
    last = window[-1].close
    state = "bullish" if last > fast > slow else "bearish" if last < fast < slow else "neutral"
    result.update(condition=state, reason="Fixed price / fast-average / slow-average alignment",
                  price=last, fast_average=fast, slow_average=slow,
                  support=min(b.low for b in window[-rules.fast_hours:]),
                  invalidation=fast if state == "bullish" else None)
    return result


def drawdown(equity):
    peak, worst = 1.0, 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return -worst


def replay(bars, evaluation_start, source, rules=Rules()):
    """Locked-rule chronological evaluation, not optimized or certified OOS evidence.

    Decisions use complete candles at UTC schedule boundaries. Orders fill one
    hour later, at that candle's open. Both strategies liquidate at final close.
    Bullish = spot long; every other condition = cash. No shorting/leverage.
    """
    validate(bars)
    if not source.strip():
        raise ValueError("A dataset source description is required")
    evaluation_start = timestamp(evaluation_start)
    if evaluation_start.minute or evaluation_start.second or evaluation_start.microsecond:
        raise ValueError("Evaluation start must be on an hourly boundary")
    if evaluation_start < bars[0].start + rules.slow_hours * HOUR:
        raise ValueError("Evaluation needs a full warm-up period before the start")
    evaluation = [b for b in bars if b.start >= evaluation_start]
    if len(evaluation) < 2:
        raise ValueError("Evaluation needs at least two candles")
    cash, units, entry = 1.0, 0.0, None
    signals, trades, equity = [], [], []
    pending = None
    for bar in evaluation:
        if pending is not None:
            target, signal_time = pending
            if target and not units:
                entry = {"signal_time": signal_time, "entry_at": iso(bar.start),
                         "entry_price": bar.open, "capital_before": cash}
                units, cash = cash * (1 - rules.cost) / bar.open, 0.0
            elif not target and units:
                cash, units = units * bar.open * (1 - rules.cost), 0.0
                trades.append({**entry, "exit_at": iso(bar.start), "exit_price": bar.open,
                               "net_return": cash / entry["capital_before"] - 1,
                               "exit_reason": "scheduled condition change"})
                entry = None
            pending = None
        if bar.start.hour % rules.update_hours == 0:
            signal = condition(bars, bar.start, rules)
            signals.append(signal)
            pending = (signal["condition"] == "bullish", signal["as_of"])
        equity.append(cash + units * bar.close)
    if units:
        cash = units * evaluation[-1].close * (1 - rules.cost)
        trades.append({**entry, "exit_at": iso(evaluation[-1].end),
                       "exit_price": evaluation[-1].close,
                       "net_return": cash / entry["capital_before"] - 1,
                       "exit_reason": "end-of-evaluation liquidation"})
    equity[-1] = cash
    hold_units = (1 - rules.cost) / evaluation[0].open
    hold_equity = [hold_units * b.close for b in evaluation]
    hold_equity[-1] *= 1 - rules.cost
    return {"record_type": "historical_simulation", "version": VERSION,
            "source": source, "dataset_sha256": dataset_hash(bars), "rules": asdict(rules),
            "evaluation_start": iso(evaluation[0].start), "evaluation_end": iso(evaluation[-1].end),
            "warmup_candles": sum(b.start < evaluation_start for b in bars),
            "evaluation_candles": len(evaluation), "net_return": cash - 1,
            "buy_hold_net_return": hold_equity[-1] - 1,
            "excess_return": cash - hold_equity[-1],
            "max_drawdown": drawdown(equity), "buy_hold_max_drawdown": drawdown(hold_equity),
            "closed_trades": len(trades),
            "win_rate": sum(t["net_return"] > 0 for t in trades) / len(trades) if trades else None,
            "signals": signals, "trades": trades,
            "equity": [{"at": iso(b.end), "strategy": e, "buy_hold": h}
                       for b, e, h in zip(evaluation, equity, hold_equity)],
            "limitations": ["Simulated spot long/cash baseline, not actual account profits.",
                            "Not independently verified out-of-sample evidence; no parameter search performed.",
                            "OHLC and availability timestamps supplied by the operator; provenance must be verified.",
                            "One-hour execution delay; costs applied per side, including final liquidation.",
                            "Excludes taxes, funding, staking yield, market impact beyond fixed slippage, and outages.",
                            "Mining emissions and GPU-fit scores are not trading predictors in this model."]}
