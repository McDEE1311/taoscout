# Market research foundation — recovery handoff

This is the first research-engine PR described in the interrupted session. It is
**not a completed paid Pro product**, not a proven trading strategy, and not a
production deployment. There are no customer accounts, Stripe checkout,
entitlements, wallet access, order execution, or push notifications in this PR.
Existing operator routes are unchanged; the optional `/market` mount is off by
default. The public app can also run as an independent process.

## What exists

- Hourly TAO/USD spot trend baseline (24-hour / 72-hour averages), with bullish,
  bearish, neutral, and unavailable states. This is a baseline to evaluate, not
  the proposed multi-factor consensus product. No invented confidence percentage.
- Deterministic historical replay with explicit evaluation start, input hash,
  every scheduled signal, every closed trade, hourly equity, per-side costs,
  drawdown, and a same-period buy-and-hold comparison.
- Paper publication ledger with one entry per 8-hour UTC slot, idempotent retries,
  SQL update/delete guards, hash-chain validation, and paginated public history.
- Responsive public research page with manifest, icons, and a static-only service
  worker. No research/API responses are cached. Installation needs HTTPS or
  localhost. Device installation has not been verified on iPhone or Android.
- Isolated tests and GitHub Actions. No Bittensor node or LLM is needed for them.

## Why the existing database is not trading-grade price history

`db.insert_snapshot` records insertion time. `db.import_json_snapshots` sends old
JSON through that function and can apply one supplied price to all imported rows.
`scout.get_chain_data` also uses cached enrichment price with no separate price
observation timestamp. Thus, historical snapshots may describe import time or
stale prices, rather than contemporaneous tradable TAO prices.

The new engine does **not** silently consume those rows. It leaves the existing
DB untouched. They may be useful for operator research after provenance review;
they are not sufficient OHLC evidence for a credible TAO price backtest.

Read-only audit on the rig:

```sh
python3 -m market audit-snapshots --db /home/mcdeerig/taoscout/data/taoscout.db
```

This prints coverage only, no secrets or wallet data. The code does not repair
old timestamps by guessing.

## Verified input contract

Obtain continuous hourly TAO/USD OHLC from a documented provider. TAO/USDT is a
different quote market; it requires explicit labeling and treatment, not silent
renaming to USD. The provider/feed integration remains to be built once the
venue, quote currency, available credentials, and history are confirmed.

CSV columns:

```csv
start,available_at,open,high,low,close
2025-01-01T00:00:00+00:00,2025-01-01T01:00:05+00:00,100,102,99,101
```

That row is an **illustrative schema example**, not actual TAO data.
`start` is the UTC hourly bar start; `available_at` is when its complete contents
became available. Never invent historical availability times. If a vendor
archive only records bar-close times, describe that limitation and the assumed
publication delay before interpreting results. Live data should record actual
first observation times. Keep input files private under `data/`.

The parser rejects missing columns, naive timestamps, duplicate/unsorted/missing
hours, nonfinite/nonpositive prices, impossible OHLC, and availability before
close. It never interpolates gaps. A delayed candle makes that decision
unavailable; later decisions can use it once it is known.

## Reproduce a historical simulation

The engine and CLI require only Python 3.11+.

```sh
python3 -m market backtest \
  --csv data/tao-usd-hourly.csv \
  --source 'Provider / exact market / export date / availability assumptions' \
  --evaluation-start '2026-08-01T00:00:00+00:00' \
  --fee-bps 10 --slippage-bps 10 \
  --output data/market-report-2026-08.json
```

Choose an evaluation start with at least 72 earlier hours. The example date is
not a claim that those data are present. The CLI rejects future/incomplete price
bars and refuses to overwrite an existing output file.

Rules are fixed, not tuned by this command. A chronological split alone is not
proof of genuine out-of-sample performance if rules were selected after viewing
that period. Freeze the methodology, record dataset provenance, and evaluate on
untouched data before asserting an edge.

At 00:00/08:00/16:00 UTC, the strategy uses only completed and available prior
candles. It acts at the following hour's open, not on the signal candle's price.
Bullish enters/holds spot TAO; all other states exit/remain in cash. Every side
costs `(fee_bps + slippage_bps) / 10000`; terminal positions are liquidated at the
last close with costs. Buy-and-hold buys at evaluation start and liquidates at
the same end with the same costs. Costs are configurable assumptions, not quotes
from an exchange. There is no leverage, shorting, funding, interest, or staking.
Drawdown is sampled hourly at closes and does not capture intrabar extremes.

## Start the public preview locally

```sh
python3 -m venv .venv-market
.venv-market/bin/pip install -r requirements-market.txt
.venv-market/bin/uvicorn market.web:app --host 127.0.0.1 --port 8766
```

Open `http://127.0.0.1:8766/`. An empty preview is expected until research is
published. Environment variables:

- `TAOSCOUT_MARKET_LEDGER`: absolute path to the separate research DB.
- `TAOSCOUT_MARKET_REPORT`: optional absolute path to a vetted simulation JSON.
  Anything configured here is public, including the full signal/trade history
  and source description. Do not put credentials or personal data in it.

For the existing server, add these to local `config.json` only after installing
the market dependencies and testing in staging:

```json
{
  "market_research_enabled": true,
  "market_ledger_path": "/home/mcdeerig/taoscout/data/market-research.db",
  "market_report_path": "/home/mcdeerig/taoscout/data/market-report-2026-08.json"
}
```

Merge those keys into the existing object, preserving all current settings.
Omit `market_report_path` until there is a vetted report. The optional mount
serves `/market/`. Existing operator API keys do not become customer logins.
Use HTTPS and appropriate edge rate limits before making this public; this
read-only preview does not implement a paid-data boundary.

## Start a forward paper record

After an automated, monitored feed is ready, publish shortly after each scheduled
boundary, e.g. 00:05, 08:05, 16:05 UTC:

```sh
python3 -m market publish \
  --csv data/tao-usd-hourly.csv \
  --source 'Documented live source and market' \
  --ledger data/market-research.db
python3 -m market history --ledger data/market-research.db
```

The CLI uses the current clock, permits only the first 30 minutes of each slot,
and offers no historical time override. A retry returns the existing record;
changed input cannot replace it. Publication needs no-clear-signal records too.
The Python `now` parameter is only a test seam; possession of the server/code
means an administrator can fabricate history. SQL triggers and hashes are not
external notarization: archive the public log independently as it is published.
Forward records are research publications, not executed positions or realized
PnL. A forward execution/outcome evaluator is still required before displaying
forward strategy performance. The displayed simulation remains separately
labeled and never fills the paper log.

## Verify

```sh
python3 -m venv .venv-market
.venv-market/bin/pip install -r requirements-market-test.txt
.venv-market/bin/python -m unittest discover -s tests -v
```

Tests cover future-data exclusion, availability delays, stale/gapped data,
execution lag, both-side fees, same-period benchmark, losing trades, hash input,
idempotency, tamper detection, paging, read-only legacy audit, HTTP guards,
security headers, broken-data failure, and mounted routes. All fixtures are
synthetic and assert accounting/control behavior, not trading profitability.

## Remaining work before the requested paid launch

1. Confirm the actual website hosting/repository and rig deployment path. This
   repo is the operator API; the public marketing site's implementation is not
   included here. No DNS, hosting, or live server was changed.
2. Connect a properly timestamped market feed; review legacy-data provenance;
   run and inspect the historical evaluation. Freeze/validate the proposed
   consensus methodology rather than calling this baseline proven.
3. Start independently archived forward publication and implement forward
   outcome accounting. Personal account profit claims require complete exchange
   fills, fees, deposits/withdrawals, and position history. Old winning
   screenshots cannot establish Pro's performance or causation.
4. Build customer authentication, recovery, sessions, shared rate limiting,
   Stripe test-mode checkout/webhook idempotency, cancellation/expiry handling,
   and server-side Free/Pro entitlements. Current working price intent from the
   supplied chat: $2.99/month, $24.99/year; do not create live prices yet.
5. Add delayed Free research, protected current Pro data, watchlist, optional
   notifications, billing controls, and mobile installation tests. This public
   preview exposes its whole record and is not the future paywall.
6. Review the actual product/claims, then approve production rollout and charging.
   No live trading, billing, or paid advertising has been enabled by this PR.

## Validation limits in this recovery session

The engine and HTTP suite passed all 24 tests; Python compilation and JavaScript
syntax checks passed. A browser-render check was attempted, but this environment
had no browser binary and its browser download failed. Visual layout, actual
service-worker/offline behavior, and mobile installation therefore still need
staging/device verification. The legacy Bittensor runtime and rig database were
not available here, so the optional mount was tested using an isolated FastAPI
parent rather than starting the production operator stack.
