#!/usr/bin/env bash
# Smoke test end-to-end pour cloe-care.
#
# Usage :
#   ./scripts/smoke_test.sh                       # local via http://127.0.0.1:8900
#   ./scripts/smoke_test.sh https://care.hellocloe.fr  # prod
#
# Le JWT est généré côté cloe-api avec un client_id de test.

set -euo pipefail

CARE_URL="${1:-http://127.0.0.1:8900}"
CLIENT_ID="${SMOKE_CLIENT_ID:-c_smoke_test}"

if ! command -v jq >/dev/null 2>&1; then
  echo "✗ jq requis (apt install -y jq)" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "✗ curl requis" >&2
  exit 1
fi

echo "=== Génération du JWT via cloe-api ==="
JWT="$(docker exec cloe-api python -c "from jwt_auth import _create_access_token; print(_create_access_token('${CLIENT_ID}', 'pro'))")"
if [ -z "$JWT" ]; then
  echo "✗ JWT non généré" >&2
  exit 1
fi
echo "✓ JWT obtenu (${#JWT} chars)"

echo
echo "=== 1. Health check ==="
HEALTH="$(curl -fsSL "$CARE_URL/health")"
echo "$HEALTH" | jq .
echo "$HEALTH" | jq -e '.status == "ok"' >/dev/null
echo "✓ health OK"

echo
echo "=== 2. Création d'un ticket ==="
TICKET_ID="$(curl -fsSL -X POST "$CARE_URL/tickets" \
    -H "Authorization: Bearer $JWT" | jq -r .ticket_id)"
echo "✓ ticket_id=$TICKET_ID"

echo
echo "=== 3. Envoi d'un message (SSE) ==="
curl -fsSL -X POST "$CARE_URL/tickets/$TICKET_ID/messages" \
    -H "Authorization: Bearer $JWT" \
    -H "Content-Type: application/json" \
    -d '{"content":"Quand je clique sur Generer le rapport, rien ne se passe et je vois une roue qui tourne sans fin."}' \
    --max-time 30 | head -c 600
echo
echo "✓ chat ok"

echo
echo "=== 4. Soumission du ticket ==="
SUBMIT="$(curl -fsSL -X POST "$CARE_URL/tickets/$TICKET_ID/submit" \
    -H "Authorization: Bearer $JWT")"
echo "$SUBMIT" | jq .
SUBMIT_STATUS="$(echo "$SUBMIT" | jq -r .status)"
if [ "$SUBMIT_STATUS" = "received" ] || [ "$SUBMIT_STATUS" = "rejected_review" ]; then
  echo "✓ submit ok (status=$SUBMIT_STATUS)"
else
  echo "✗ statut inattendu: $SUBMIT_STATUS" >&2
  exit 1
fi

echo
echo "=== 5. Polling du statut (jusqu'à 120s) ==="
for i in $(seq 1 24); do
  sleep 5
  STATUS="$(curl -fsSL "$CARE_URL/tickets/$TICKET_ID/status" \
      -H "Authorization: Bearer $JWT" | jq -r .status)"
  echo "  [$i/24 t=$((i*5))s] status=$STATUS"
  case "$STATUS" in
    resolved|escalated|fix_rolled_back|rejected_review|no_action|refused_by_admin|awaiting_admin_review)
      echo "✓ état terminal/attente atteint: $STATUS"
      exit 0
      ;;
  esac
done

echo "✗ Timeout : aucun état terminal après 120s" >&2
exit 1
