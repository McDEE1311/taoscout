#!/bin/bash
# Run this on the rig to install auth system
set -e

cd ~/taoscout

echo "=== Installing dependencies ==="
pip install passlib bcrypt --break-system-packages

echo "=== Copying files ==="
cp taoscout_auth.py ~/taoscout/taoscout_auth.py
cp taoscout_routes.py ~/taoscout/taoscout_routes.py

echo "=== Creating .env if missing ==="
if [ ! -f .env ]; then
  cat > .env << 'EOF'
TAOSCOUT_PAYMENT_ADDRESS=5GWF2n8PGg1hgcM7KEvJohHmEgtK8Mr1Qarm4MedXfGtwcTb
BASE_URL=https://app.taoscout.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=tao.forge.ai@gmail.com
SMTP_PASS=REPLACE_WITH_APP_PASSWORD
FROM_EMAIL=tao.forge.ai@gmail.com
EOF
  echo ".env created — add your Gmail app password"
else
  echo ".env already exists"
fi

echo "=== Backing up api.py ==="
cp api.py api.py.pre_auth_$(date +%Y%m%d_%H%M%S).bak

echo "=== Merging auth into api.py ==="
python3 << 'PY'
from pathlib import Path

api = Path("api.py").read_text()
auth = Path("taoscout_auth.py").read_text()
routes = Path("taoscout_routes.py").read_text()

# Add import at top after existing imports
import_line = "from taoscout_auth import (\n    PLANS, create_order, confirm_order, verify_pin, hash_pin,\n    create_session, verify_session, delete_session,\n    start_payment_watcher, start_expiration_checker,\n    check_pending_payments, get_conn, ENV, PAYMENT_ADDRESS\n)\n"

# Find where to insert import (after last existing import block)
insert_after = "sys.path.insert(0, str(SCRIPT_DIR))"
if insert_after in api:
    api = api.replace(insert_after, insert_after + "\n" + import_line)
    print("✓ Added imports")
else:
    print("✗ Could not find import insertion point")

# Add startup event to launch background workers
startup_code = """
@app.on_event("startup")
async def startup_workers():
    start_payment_watcher(interval_seconds=300)
    start_expiration_checker(interval_seconds=3600)

"""
# Insert before first route
insert_before = '@app.get("/health")'
if insert_before in api:
    api = api.replace(insert_before, startup_code + insert_before)
    print("✓ Added startup workers")
else:
    print("✗ Could not find health route")

# Append routes
api = api + "\n\n# ── AUTH ROUTES ──\n" + routes
Path("api.py").write_text(api)
print("✓ Routes merged")
PY

echo "=== Compiling ==="
python3 -m py_compile api.py && echo "✓ COMPILE OK" || echo "✗ COMPILE FAILED"

echo "=== Done. Add Gmail app password to .env then restart PM2 ==="
echo "pm2 restart taoscout-api"
