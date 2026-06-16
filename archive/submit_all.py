import json, base64, io, zipfile, requests, time
from datetime import datetime, timezone
from pathlib import Path
from substrateinterface import Keypair
try:
    import blake3 as bk
    blake3_hex = lambda b: bk.blake3(b).hexdigest()
except:
    import hashlib
    blake3_hex = lambda b: hashlib.sha256(b).hexdigest()

HOTKEY_SS58 = '5G9byaaFsMrDgutGqcsrRCDpDnqCmUEChZ3WFWSLQp9szbPH'
hotkey_data = json.load(open(Path.home()/'.bittensor/wallets/tao_wallet/hotkeys/sn39_miner'))
keypair = Keypair.create_from_mnemonic(hotkey_data['secretPhrase'])
soul = open(Path.home()/'cathedral-baseline-agent/soul.md').read()
agents = open(Path.home()/'cathedral-baseline-agent/AGENTS.md').read()
config = open(Path.home()/'cathedral-baseline-agent/config.yaml').read()
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
    zf.writestr('soul.md', soul)
    zf.writestr('AGENTS.md', agents)
    zf.writestr('config.yaml', config)
bundle_bytes = buf.getvalue()
bundle_hash = blake3_hex(bundle_bytes)

for card in ['eu-ai-act','us-ai-eo','uk-ai-whitepaper','singapore-pdpc','japan-meti-mic']:
    submitted_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    payload = json.dumps({'bundle_hash':bundle_hash,'card_id':card,'miner_hotkey':HOTKEY_SS58,'submitted_at':submitted_at},sort_keys=True,separators=(',',':'))
    sig_b64 = base64.b64encode(bytes(keypair.sign(payload.encode()))).decode()
    resp = requests.post('https://api.cathedral.computer/v1/agents/submit',
        headers={'X-Cathedral-Signature':sig_b64,'X-Cathedral-Hotkey':HOTKEY_SS58},
        data={'card_id':card,'display_name':'McDEE-v3','attestation_mode':'ssh-probe','ssh_host':'68.52.239.238','ssh_port':'22','ssh_user':'cathedral-probe','hermes_port':'8088','submitted_at':submitted_at},
        files={'bundle':('agent.zip',bundle_bytes,'application/zip')},
        timeout=180)
    print(f'{card}: {resp.status_code} {resp.text[:120]}')
    time.sleep(3)
