# TaoScout

Bittensor operator intelligence agent for subnet analytics, GPU-fit ranking, movers, risk scoring, and operator actions.

## What it does

- Tracks live Bittensor subnet emissions, burn, fill, and trends
- Scores opportunities by GPU class: 24GB / 32GB / 48GB / 96GB
- Flags movers, volatility, deregistration risk, and operator actions
- Provides FastAPI endpoints and simple dashboard access
- Supports natural-language questions through local inference

## Hardware

Minimum:
- RTX 3090 24GB

Preferred:
- RTX 4090 / RTX 5090

Helper GPUs:
- RTX 3060s can be used for background/routing lanes, not minimum runtime

## Setup

cp config.example.json config.json
pip install -r requirements.txt
python3 api.py

## Endpoints

- GET /health
- GET /dashboard
- GET /brief
- GET /scan
- GET /rankings
- GET /movers
- POST /ask
- GET /query-stats

Protected endpoints require X-API-Key.

## Notes

TaoScout is operator tooling. Informational only. Not financial advice.
