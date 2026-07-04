# taorig1 Disaster Recovery Runbook

**Last incident:** 2026-07-03/04 power outage
**Repo location:** `ops/DISASTER_RECOVERY.md` (pair with `ops/disaster-recovery.sh`)

Run the script for an automated pass. Use this doc when you need to understand *why*, or when something doesn't match the script's assumptions and you're debugging by hand.

---

## Priority order (why the script checks in this sequence)

1. **GPU** — foundational, nothing works without it
2. **pm2 daemon** — everything revenue-generating runs under it
3. **DNS chain** — silent killer; breaks tunnel + any outbound chain calls without looking like a crash
4. **cloudflared tunnel** — public access to TaoScout
5. **TaoScout API** — the actual product
6. **LotiE** — ops/monitoring agent, NOT restarted automatically (see incident note below)
7. **Apex/Tron training** — lowest priority, speculative, not revenue. Not auto-restarted.

---

## Known failure modes (confirmed root causes, not guesses)

### 1. Tailscale DNS forwarder dies silently after reboot
**Symptom:** `cloudflared` fails with `Couldn't resolve SRV record ... server misbehaving`, pointing at a `fd7a:115c:a1e0::53` address (Tailscale's MagicDNS resolver). Site shows Cloudflare **Error 1033** even though the API is healthy locally.

**Root cause:** `tailscaled` health check reports `Tailscale failed to fetch the DNS configuration of your device` — its internal DNS forwarder has no upstream resolvers configured. This happens when `systemd-resolved` doesn't fully restore `/etc/resolv.conf` after a hard reboot, so Tailscale has nothing to forward external DNS queries to.

**Fix sequence:**
```bash
cat /etc/resolv.conf                          # check if empty/broken
sudo systemctl restart systemd-resolved
sleep 2
sudo systemctl restart tailscaled
sleep 3
tailscale status                              # health check line should be gone
nslookup api.cloudflare.com                   # confirm resolution works
pm2 restart taoscout-tunnel
pm2 logs taoscout-tunnel --lines 20 --nostream
```

**Fallback if resolved restart doesn't fix it:**
```bash
sudo tailscale set --accept-dns=false         # bypass MagicDNS, use system resolver directly
```

**Diagnostic trap to avoid:** the Cloudflare 1033 error page and the cloudflared logs both *look* like a Cloudflare/tunnel problem. They're not — check DNS on the host first before touching tunnel config or credentials. Wasted debugging time comes from assuming the layer where the error surfaces is the layer where it originates.

### 2. pm2 "online" doesn't mean "working"
`taoscout-tunnel` showed status `online` in `pm2 list` with a **302 restart count** — it was crash-looping the entire time, invisible unless you check restart count or read logs. **Always check restart count, not just status column, when triaging.**

### 3. LotiE config loss (prior incident, same failure class)
A previous outage wiped LotiE's OpenClaw config and left a bad model default in place, causing an API cost spike on restart. **Do not restart LotiE blind after any outage** — confirm config file exists and model default is correct first.

Current confirmed config path: `<UNKNOWN_REQUIRES_USER_INPUT — fill in once located>`

---

## Manual step-by-step (if not running the script)

```bash
# 1. GPU
nvidia-smi

# 2. pm2
pm2 list
pm2 resurrect      # only if daemon is down and you've previously run `pm2 save`

# 3. DNS
cat /etc/resolv.conf
resolvectl status
sudo systemctl restart systemd-resolved
sudo systemctl restart tailscaled
tailscale status
nslookup api.cloudflare.com

# 4. Tunnel
pm2 restart taoscout-tunnel
pm2 logs taoscout-tunnel --lines 20 --nostream
curl -sI https://taoscout.com | head -5

# 5. TaoScout API
curl -s localhost:<PORT>/health
pm2 logs taoscout-api --lines 30 --nostream

# 6. LotiE — DO NOT restart until config confirmed
cat <LOTIE_CONFIG_PATH>

# 7. Apex/Tron — check only, don't auto-resume
ls -la ~/apex-tron/snapshots/ 2>&1
tail -30 ~/apex-tron/training_log.txt 2>&1
```

---

## Open items to fill in (mark resolved once confirmed)

- [ ] Confirm exact LotiE config path → hardcode into script's `LOTIE_CONFIG_PATH`
- [ ] Confirm `taoscout-api` health-check port → hardcode into script's `TAOSCOUT_PORT`
- [ ] Decide fate of `cathedral-sat`, `hotfloat-watch`, `sn29-watch` (currently stopped in pm2 — intentional or dead weight?)
- [ ] Consider `pm2 save` cron/systemd hook so `pm2 resurrect` always has a fresh snapshot after outages
- [ ] Consider a `crontab @reboot` entry running this script automatically on boot, so recovery starts before you're even at the keyboard
