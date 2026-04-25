# TaoScout
Bittensor operator intelligence agent. Tracks 129 subnets in real time.

## What it does
- Live subnet emission, burn, fill tracking
- GPU fit scoring for 24GB / 32GB / 48GB / 96GB hardware
- Daily operator dashboard with actionable alerts
- Natural language Bittensor questions
- 24h emission movers and trend tracking
- Deregistration risk scoring

## Hardware Requirements
- Minimum: RTX 3090 24GB
- Recommended: RTX 4090 24GB or RTX 5090 32GB

## Quick Start
cp config.example.json config.json
# Add your keys to config.json
pip install -r requirements.txt
python3 api.py

## Endpoints (require X-API-Key header)
GET  /health       system status
GET  /brief        operator brief
GET  /dashboard    daily dashboard with actions
GET  /scan         top 5 opportunities
GET  /rankings     GPU class rankings
GET  /movers       emission movers
POST /ask          natural language query
GET  /query-stats  usage analytics

## Environment Variables
TAOSTATS_API_KEY — from taostats.io
OLLAMA_HOST — default http://127.0.0.1:11434

## Not financial advice
TaoScout is automated operator tooling. Informational only.
