#!/usr/bin/env bash
#
# taorig1 disaster recovery — post power-outage bring-up
# Repo: github.com/McDEE1311/taoscout (dev branch) — ops/disaster-recovery.sh
#
# Usage:
#   chmod +x disaster-recovery.sh
#   ./disaster-recovery.sh          # run full check + fix sequence
#   ./disaster-recovery.sh --check  # dry-run, report only, no restarts
#
# Order matters: GPU -> pm2 daemon -> DNS chain -> tunnel -> app health -> LotiE
# Each stage prints PASS/FAIL/ACTION so you can see exactly what broke.

set -uo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--check" ]]; then
  DRY_RUN=true
fi

pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1"; }
info() { echo "[INFO] $1"; }
act()  { echo "[ACTION] $1"; }

run_or_echo() {
  if $DRY_RUN; then
    info "would run: $*"
  else
    "$@"
  fi
}

echo "=================================================="
echo " taorig1 disaster recovery — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo " mode: $([ "$DRY_RUN" = true ] && echo 'CHECK ONLY' || echo 'FIX')"
echo "=================================================="

# ---------------------------------------------------------------------------
# STAGE 1: GPU health
# ---------------------------------------------------------------------------
echo
echo "--- STAGE 1: GPU health ---"
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l)
  if [[ "$GPU_COUNT" -eq 3 ]]; then
    pass "all 3 GPUs detected"
    nvidia-smi --query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used --format=csv,noheader
  else
    fail "expected 3 GPUs, found $GPU_COUNT — check physical connections / driver"
    nvidia-smi 2>&1
  fi
else
  fail "nvidia-smi not found — driver may not have loaded after reboot"
fi

# ---------------------------------------------------------------------------
# STAGE 2: pm2 daemon + process list
# ---------------------------------------------------------------------------
echo
echo "--- STAGE 2: pm2 ---"
if pm2 ping >/dev/null 2>&1; then
  pass "pm2 daemon responding"
else
  fail "pm2 daemon not responding"
  act "attempting pm2 resurrect (requires prior 'pm2 save')"
  run_or_echo pm2 resurrect
fi
pm2 jlist > /tmp/pm2_state.json 2>/dev/null || true
if [[ -s /tmp/pm2_state.json ]]; then
  STOPPED=$(python3 -c "
import json
try:
    procs = json.load(open('/tmp/pm2_state.json'))
    stopped = [p['name'] for p in procs if p.get('pm2_env',{}).get('status') != 'online']
    print(','.join(stopped) if stopped else 'none')
except Exception as e:
    print('parse-error')
" 2>/dev/null)
  if [[ "$STOPPED" == "none" ]]; then
    pass "all pm2 processes online"
  else
    info "stopped/non-online processes: $STOPPED"
    info "confirm manually whether these should be running before restarting"
  fi
fi

# ---------------------------------------------------------------------------
# STAGE 3: DNS chain — resolv.conf -> systemd-resolved -> tailscaled
# ---------------------------------------------------------------------------
echo
echo "--- STAGE 3: DNS chain ---"
if nslookup api.cloudflare.com >/dev/null 2>&1; then
  pass "external DNS resolution working"
else
  fail "external DNS resolution broken"
  info "current /etc/resolv.conf:"
  cat /etc/resolv.conf 2>&1

  act "restarting systemd-resolved"
  run_or_echo sudo systemctl restart systemd-resolved
  sleep 2

  act "restarting tailscaled"
  run_or_echo sudo systemctl restart tailscaled
  sleep 3

  if ! $DRY_RUN; then
    if nslookup api.cloudflare.com >/dev/null 2>&1; then
      pass "DNS resolution restored after resolved+tailscaled restart"
    else
      fail "still broken — falling back to tailscale --accept-dns=false"
      run_or_echo sudo tailscale set --accept-dns=false
      sleep 2
      if nslookup api.cloudflare.com >/dev/null 2>&1; then
        pass "DNS resolution restored via accept-dns=false fallback"
      else
        fail "DNS still broken — manual intervention required. Check upstream resolvers, /etc/systemd/resolved.conf, and network interface config."
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
# STAGE 4: cloudflared tunnel
# ---------------------------------------------------------------------------
echo
echo "--- STAGE 4: cloudflared tunnel ---"
if pm2 describe taoscout-tunnel >/dev/null 2>&1; then
  RESTART_COUNT=$(pm2 jlist 2>/dev/null | python3 -c "
import json,sys
procs = json.load(sys.stdin)
for p in procs:
    if p['name'] == 'taoscout-tunnel':
        print(p.get('pm2_env',{}).get('restart_time', '?'))
" 2>/dev/null)
  info "taoscout-tunnel restart count: $RESTART_COUNT"
  act "restarting tunnel to pick up fixed DNS"
  run_or_echo pm2 restart taoscout-tunnel
  sleep 5
  if ! $DRY_RUN; then
    pm2 logs taoscout-tunnel --lines 10 --nostream | tail -10
  fi
else
  fail "taoscout-tunnel not found in pm2 — check process name"
fi

echo
echo "--- Verify externally: curl -sI https://taoscout.com | head -5 ---"
if ! $DRY_RUN; then
  curl -sI --max-time 8 https://taoscout.com 2>&1 | head -5
fi

# ---------------------------------------------------------------------------
# STAGE 5: TaoScout API health
# ---------------------------------------------------------------------------
echo
echo "--- STAGE 5: TaoScout API ---"
# <UNKNOWN_REQUIRES_USER_INPUT>: confirm actual local port taoscout-api binds to
TAOSCOUT_PORT="${TAOSCOUT_PORT:-8000}"
if curl -sf --max-time 5 "http://localhost:${TAOSCOUT_PORT}/health" >/dev/null 2>&1; then
  pass "taoscout-api /health responding on port ${TAOSCOUT_PORT}"
else
  fail "taoscout-api /health not responding on port ${TAOSCOUT_PORT} — confirm correct port and check: pm2 logs taoscout-api --lines 30 --nostream"
fi

# ---------------------------------------------------------------------------
# STAGE 6: LotiE config check (no auto-restart — confirm config first)
# ---------------------------------------------------------------------------
echo
echo "--- STAGE 6: LotiE ---"
# <UNKNOWN_REQUIRES_USER_INPUT>: fill in actual config path once confirmed
LOTIE_CONFIG_PATH="${LOTIE_CONFIG_PATH:-}"
if [[ -z "$LOTIE_CONFIG_PATH" ]]; then
  info "LOTIE_CONFIG_PATH not set — skipping automated check. Set this env var once path is confirmed, e.g.:"
  info "  LOTIE_CONFIG_PATH=/path/to/config ./disaster-recovery.sh"
elif [[ -f "$LOTIE_CONFIG_PATH" ]]; then
  pass "LotiE config found at $LOTIE_CONFIG_PATH"
  info "manually verify model default before restarting LotiE (past incident: bad default caused API cost spike)"
else
  fail "LotiE config NOT found at $LOTIE_CONFIG_PATH — do not restart LotiE until config is rebuilt/restored"
fi

echo
echo "=================================================="
echo " Recovery pass complete."
echo " Revenue-critical (TaoScout, alpha trader, MMM) checked first by design."
echo " Apex/Tron training intentionally NOT auto-restarted — low priority, resume manually."
echo "=================================================="
