#!/usr/bin/env bash
# Drive the bot's WhatsApp conversation locally.
#
#   ./scripts/whatsapp.sh "hi"
#   ./scripts/whatsapp.sh "1" "2" "1"
#
# Each argument is one inbound customer message, sent in order. The bot's
# replies land in scripts/evolution-inbox.log (via the fake Evolution API).
#
# Requires: the bot on :9000, the fake Evolution API on :9999, and a tenant
# whose evolution_api_url points at the fake one.
set -euo pipefail

BOT="${BOT_URL:-http://localhost:9000}"
CUSTOMER="${CUSTOMER_NUMBER:-27711111111@s.whatsapp.net}"
TENANT="${TENANT_NUMBER:-27810000000}"

for text in "$@"; do
  # A unique id per message — the bot uses it for idempotency, and a repeated
  # id would be silently dropped as a duplicate.
  MSG_ID="local-$(date +%s%N)-$RANDOM"
  curl -s -m 10 -X POST "$BOT/webhook" \
    -H 'Content-Type: application/json' \
    -d "{
      \"event\": \"messages.upsert\",
      \"sender\": \"$TENANT\",
      \"data\": {
        \"key\": {\"remoteJid\": \"$CUSTOMER\", \"id\": \"$MSG_ID\", \"fromMe\": false},
        \"pushName\": \"Test Customer\",
        \"message\": {\"conversation\": \"$text\"}
      }
    }"
  echo ""
  sleep 1   # let the outbox drain so replies land in order
done
