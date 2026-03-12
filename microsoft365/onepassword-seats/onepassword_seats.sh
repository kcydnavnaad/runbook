#!/bin/bash
set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq ontbreekt. Installeer met: brew install jq"
  exit 1
fi

# Service account token uit Keychain
export OP_SERVICE_ACCOUNT_TOKEN="$(security find-generic-password -a "$USER" -s OP_SERVICE_ACCOUNT_TOKEN -w)"

json="$(op user list --format=json)"

# Billable states (seats in use)
# - ACTIVE
# - TRANSFER_STARTED
# - RECOVERY_STARTED
billable_total="$(echo "$json" | jq '[.[] | select(.state=="ACTIVE" or .state=="TRANSFER_STARTED" or .state=="RECOVERY_STARTED")] | length')"
suspended_total="$(echo "$json" | jq '[.[] | select(.state=="SUSPENDED")] | length')"
total_users="$(echo "$json" | jq 'length')"

echo "==================== 1Password Seats ===================="
echo "• Billable seats: $billable_total"
echo "• Suspended:      $suspended_total"
echo "• Total users:    $total_users"
echo ""

echo "================= Billable seats per domein ============="

# Per domein tellen (billable only)
# - als email leeg is of geen @ heeft -> 'unknown'
echo "$json" | jq -r '
  [ .[]
    | select(.state=="ACTIVE" or .state=="TRANSFER_STARTED" or .state=="RECOVERY_STARTED")
    | (.email // "" | ascii_downcase) as $e
    | if ($e | contains("@")) then ($e | split("@")[1]) else "unknown" end
  ]
  | group_by(.)
  | map({domain: .[0], count: length})
  | sort_by(.domain)
  | .[]
  | "\(.domain)\t\(.count)"
' | awk -F'\t' '{ printf "• %-28s %s\n", $1":", $2 }'

echo ""
echo "========================================================="