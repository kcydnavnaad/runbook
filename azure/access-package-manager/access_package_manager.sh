#!/bin/bash

# Haal client-id, tenant-id en client-secret op uit omgevingsvariabelen
CLIENT_ID="$CLIENT_ID"
TENANT_ID="$TENANT_ID"
CLIENT_SECRET="$CLIENT_SECRET"

# Controleer of de omgevingsvariabelen zijn ingesteld
if [[ -z "$CLIENT_ID" || -z "$TENANT_ID" || -z "$CLIENT_SECRET" ]]; then
  echo "Error: CLIENT_ID, TENANT_ID, of CLIENT_SECRET is niet ingesteld. Zorg ervoor dat de omgevingsvariabelen zijn geconfigureerd."
  exit 1
fi

# Microsoft Graph API URL's
AUTH_URL="https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token"
GRAPH_API_URL="https://graph.microsoft.com/v1.0/"

# Functie voor het verkrijgen van een access token
get_access_token() {
  echo "Authenticating and obtaining access token..."
  ACCESS_TOKEN=$(curl -s -X POST $AUTH_URL \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "client_id=$CLIENT_ID" \
    -d "scope=https://graph.microsoft.com/.default" \
    -d "client_secret=$CLIENT_SECRET" \
    -d "grant_type=client_credentials" | jq -r '.access_token')

  # Check of het token succesvol is verkregen
  if [[ -z "$ACCESS_TOKEN" || "$ACCESS_TOKEN" == "null" ]]; then
    echo "Error: Failed to obtain access token"
    exit 1
  fi

  echo "Access token obtained successfully"
}

# Functie voor het ophalen van de lijst met access packages
get_access_packages() {
  echo "Fetching access packages..."
  
  RESPONSE=$(curl -s -X GET "${GRAPH_API_URL}identityGovernance/entitlementManagement/accessPackages" \
    -H "Authorization: Bearer $ACCESS_TOKEN")

  # Debug: Log de response van de API-aanroep
  echo "API Response:"
  echo "$RESPONSE" | jq .

  # Resultaat controleren
  if echo "$RESPONSE" | jq '.' | grep -q '"error"'; then
    echo "Error fetching access packages:"
    echo "$RESPONSE" | jq '.error'
    exit 1
  fi

  # Aantal access packages berekenen en weergeven
  ACCESS_PACKAGE_COUNT=$(echo "$RESPONSE" | jq '.value | length')
  echo "Total Access Packages Available: $ACCESS_PACKAGE_COUNT"

  # Access package IDs en namen ophalen
  echo "Available Access Packages:"
  echo "$RESPONSE" | jq -r '.value[] | "\(.id) - \(.displayName)"'
}

# Functie voor het bijwerken van de naam en beschrijving van de access package
update_access_package() {
  read -p "Enter the Access Package ID to update: " ACCESS_PACKAGE_ID
  read -p "Enter the customer name: " CUSTOMER_NAME
  read -p "Enter what the access package grants access to (e.g., password manager): " ACCESS_RIGHTS

  # Genereer de nieuwe naam en beschrijving
  NEW_NAME="AccessPackage-$CUSTOMER_NAME"
  NEW_DESCRIPTION="Access to $ACCESS_RIGHTS"

  echo "Updating access package..."
  echo "New Name: $NEW_NAME"
  echo "New Description: $NEW_DESCRIPTION"

  # Construeer de JSON payload voor de update
  PAYLOAD=$(cat <<EOF
  {
    "displayName": "$NEW_NAME",
    "description": "$NEW_DESCRIPTION"
  }
EOF
  )

  # Voer de request uit naar de Microsoft Graph API om de naam en beschrijving bij te werken
  RESPONSE=$(curl -s -X PATCH "${GRAPH_API_URL}identityGovernance/entitlementManagement/accessPackages/$ACCESS_PACKAGE_ID" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

  # Debug: Log de response van de API-aanroep
  echo "API Response:"
  echo "$RESPONSE" | jq .

  # Resultaat controleren
  if echo "$RESPONSE" | jq '.' | grep -q '"error"'; then
    echo "Error updating access package:"
    echo "$RESPONSE" | jq '.error'
  else
    echo "Access Package updated successfully:"
    echo "$RESPONSE" | jq .
  fi
}

# Start het script
get_access_token

while true; do
  get_access_packages

  read -p "Do you want to update an access package? (y/n): " UPDATE_CHOICE
  if [[ "$UPDATE_CHOICE" != "y" && "$UPDATE_CHOICE" != "Y" ]]; then
    break
  fi

  update_access_package
  
  # Na het bijwerken, automatisch opnieuw vragen om een Access Package ID
done

echo "Exiting the script."