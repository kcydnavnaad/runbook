#!/bin/bash

# Haal client-id, tenant-id en client-secret op uit omgevingsvariabelen
CLIENT_ID="$CLIENT_ID"
TENANT_ID="$TENANT_ID"
CLIENT_SECRET="$CLIENT_SECRET"

# Graph API instellingen
ACCESS_PACKAGE_ID="JOUW_ACCESS_PACKAGE_ID"
GRAPH_API_URL="https://graph.microsoft.com/v1.0"

# Haal een OAuth 2.0 token op
TOKEN_RESPONSE=$(curl -s -X POST -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=$CLIENT_ID" \
  -d "scope=https://graph.microsoft.com/.default" \
  -d "client_secret=$CLIENT_SECRET" \
  -d "grant_type=client_credentials" \
  "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token")

# Extraheer het access token
ACCESS_TOKEN=$(echo $TOKEN_RESPONSE | jq -r '.access_token')

# Controleer of het token is opgehaald
if [ "$ACCESS_TOKEN" == "null" ] || [ -z "$ACCESS_TOKEN" ]; then
  echo "❌ Fout: Kan geen access token ophalen!"
  exit 1
fi

echo "✅ Access token opgehaald!"

# Lifecycle-instellingen aanpassen (365 dagen, geen specifieke termijn door gebruikers)
JSON_PAYLOAD=$(cat <<EOF
{
  "assignmentPolicies": [
    {
      "expiration": {
        "type": "afterDuration",
        "duration": "P365D"
      },
      "requestorSettings": {
        "allowCustomAssignmentSchedule": false
      },
      "accessReviewSettings": {
        "isEnabled": false
      }
    }
  ]
}
EOF
)

# Stuur de PATCH-request naar Graph API
RESPONSE=$(curl -s -X PATCH "$GRAPH_API_URL/identityGovernance/entitlementManagement/accessPackages/$ACCESS_PACKAGE_ID" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$JSON_PAYLOAD")

# Controleer of de update is gelukt
if echo "$RESPONSE" | grep -q "error"; then
  echo "❌ Fout bij bijwerken van het Access Package:"
  echo "$RESPONSE"
  exit 1
else
  echo "✅ Access Package succesvol bijgewerkt!"
fi
