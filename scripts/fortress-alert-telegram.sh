#!/usr/bin/env bash
# fortress-alert-telegram.sh — sample Telegram alert hook for fortress-verify
#
# Reads verify JSON on stdin, sends a Telegram message via the bot configured
# in ~/.hermes/.env (TELEGRAM_BOT_TOKEN) to either:
#   - $SKMEMORY_FORTRESS_ALERT_CHAT_ID (preferred)
#   - the first ID in TELEGRAM_ALLOWED_USERS as fallback
#
# Install:
#   ln -sf "$(pwd)/scripts/fortress-alert-telegram.sh" ~/.skenv/bin/skmemory-fortress-alert
# Or set in environment:
#   export SKMEMORY_FORTRESS_ALERT_CMD=/path/to/fortress-alert-telegram.sh

set -euo pipefail

JSON="$(cat)"
AGENT="$(echo "$JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("agent","unknown"))')"
TOTAL="$(echo "$JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("total",0))')"
PASSED="$(echo "$JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("passed",0))')"
TAMPERED="$(echo "$JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("tampered",[])))')"
UNSEALED="$(echo "$JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("unsealed",[])))')"
TS="$(echo "$JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ts",""))')"

# Source bot token
ENV_FILE="${SKMEMORY_FORTRESS_ENV_FILE:-${HOME}/.hermes/.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${SKMEMORY_FORTRESS_ALERT_CHAT_ID:-}"
if [[ -z "$CHAT_ID" && -n "${TELEGRAM_ALLOWED_USERS:-}" ]]; then
  CHAT_ID="${TELEGRAM_ALLOWED_USERS%%,*}"
fi

if [[ -z "$TOKEN" || -z "$CHAT_ID" ]]; then
  echo "[fortress-alert-telegram] missing TELEGRAM_BOT_TOKEN or chat id; skipping" >&2
  exit 0  # don't fail the verify pipeline on missing alert config
fi

if [[ "$TAMPERED" -gt 0 ]]; then
  HEADER="🚨 *FORTRESS TAMPER DETECTED* — agent: \`${AGENT}\`"
else
  HEADER="⚠️ *Fortress verify failed* — agent: \`${AGENT}\`"
fi

TEXT="$(cat <<EOF
${HEADER}
ts: \`${TS}\`
total: ${TOTAL}, passed: ${PASSED}
tampered: ${TAMPERED}, unsealed: ${UNSEALED}

Inspect: \`skmemory fortress verify\`
Log: \`~/.skcapstone/agents/${AGENT}/logs/fortress-verify.log\`
EOF
)"

curl -sS --max-time 10 \
  -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${TEXT}" \
  --data-urlencode "parse_mode=Markdown" \
  >/dev/null || {
    echo "[fortress-alert-telegram] curl failed" >&2
    exit 1
  }

echo "[fortress-alert-telegram] alert sent to chat ${CHAT_ID}"
