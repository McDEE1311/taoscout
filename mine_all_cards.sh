#!/bin/bash
cd /home/mcdeerig/taoscout
source /home/mcdeerig/bt-venv/bin/activate
echo "$(date) Starting Cathedral mining cycle"
python3 cathedral_miner.py --card eu-ai-act
sleep 10
python3 cathedral_miner.py --card us-ai-eo
sleep 10
python3 cathedral_miner.py --card uk-ai-whitepaper
sleep 10
python3 cathedral_miner.py --card singapore-pdpc
sleep 10
python3 cathedral_miner.py --card japan-meti-mic
echo "$(date) Mining cycle complete"
