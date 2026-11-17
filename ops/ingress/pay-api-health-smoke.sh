#!/usr/bin/env bash
# Read-only external acceptance test AFTER an approved Stage-4C cutover.
# Never changes DNS, Nginx, containers or Telegram bots.
set -Eeuo pipefail
[[ "$#" == 0 ]] || { echo "No arguments accepted" >&2; exit 2; }
base="https://pay-api.usevimi.ru"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

fetch_status() {
  # Enforce trusted TLS and HTTPS. No --insecure/-k fallback.
  curl --silent --show-error --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time 10 --output "$tmp" \
    --write-out '%{http_code}' "$base$1"
}

code="$(fetch_status /health)"
[[ "$code" == "200" ]] || { echo "FAIL: /health expected 200, got $code" >&2; exit 1; }
python3 - "$tmp" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as file:
    assert json.load(file) == {"status": "ok"}, "Unexpected public health payload"
PY

for route in / /payments /subscriptions /webhooks/telegram /docs /openapi.json /health/; do
  code="$(fetch_status "$route")"
  [[ "$code" == "404" ]] || {
    echo "FAIL: $route expected closed 404, got $code" >&2
    exit 1
  }
done
echo "STAGE4C HEALTH-ONLY INGRESS: PASS (GET /health=200, all checked other GET routes=404)"
