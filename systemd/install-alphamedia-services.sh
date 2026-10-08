#!/usr/bin/env bash
# Idempotent installer / watchdog for the AlphaMedia backend services
# (API + api.alphamedia.bond named tunnel + Cloudflare API forwarder).
# Safe to run every few minutes: installs unit files if missing
# (VM replacements wipe /etc/systemd/system but keep ~/workspace),
# reloads systemd if they changed, and (re)starts services that are down.
# Does NOT touch the AlphaRDP bridge services (separate system, hands off).
set -u
SRC_DIR="/home/hatch/workspace/tgbot/systemd"
DST_DIR="/etc/systemd/system"
SERVICES="alphamedia-api.service alphamedia-tunnel.service alphamedia-cf-api-fwd.service alphamedia-egress-fwd.service"
TIMERS="alphamedia-watchdog.timer alphamedia-health-watchdog.timer"
# also copy the watchdog .service (oneshot, run by the timer)
UNITS="alphamedia-watchdog.service alphamedia-boot-recovery.service alphamedia-health-watchdog.service"

changed=0
for svc in $SERVICES $UNITS $TIMERS; do
  if ! cmp -s "$SRC_DIR/$svc" "$DST_DIR/$svc" 2>/dev/null; then
    cp "$SRC_DIR/$svc" "$DST_DIR/$svc"
    chmod 644 "$DST_DIR/$svc"
    changed=1
  fi
done
if [ "$changed" = "1" ]; then
  systemctl daemon-reload
fi
for svc in $SERVICES; do
  # alphamedia-cf-api-fwd needs a local route for the DNS-sinkhole IP so socat
  # can bind 443. systemd-spawned processes cannot add routes on this box, so
  # do it here (this script runs from a root shell in the watchdog cron too).
  if [ "$svc" = "alphamedia-cf-api-fwd.service" ]; then
    SINK=$(getent hosts api.trycloudflare.com 2>/dev/null | awk '{print $1}' | head -1)
    [ -n "$SINK" ] || SINK=198.18.230.156
    ip route replace local "$SINK/32" dev lo 2>/dev/null || true
  fi
  for attempt in 1 2 3; do
    if systemctl is-active --quiet "$svc"; then
      break
    fi
    systemctl enable --quiet "$svc" 2>/dev/null || true
    systemctl start "$svc" 2>/dev/null || systemctl restart "$svc" 2>/dev/null || true
    sleep 5
  done
  if ! systemctl is-active --quiet "$svc"; then
    echo "WARNING: $svc failed to start after 3 attempts" >&2
  fi
done
for tmr in $TIMERS; do
  if ! systemctl is-active --quiet "$tmr"; then
    systemctl enable --quiet "$tmr" 2>/dev/null || true
    systemctl start "$tmr" 2>/dev/null || true
  fi
done

# report status for the watchdog log
for svc in $SERVICES; do
  echo "$svc: $(systemctl is-active "$svc" 2>/dev/null)"
done
