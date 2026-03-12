#!/bin/bash

# Haal waarden op uit omgevingsvariabelen
tenant_id=${TENANT_ID}
client_id=${CLIENT_ID}
client_secret=${CLIENT_SECRET}

# Controleer of de variabelen zijn ingesteld
if [[ -z "$tenant_id" || -z "$client_id" || -z "$client_secret" ]]; then
  echo "Error: Omgevingsvariabelen TENANT_ID, CLIENT_ID of CLIENT_SECRET zijn niet ingesteld."
  echo "Stel deze variabelen in en probeer opnieuw."
  exit 1
fi

# Verkrijg een access token
echo "Fetching access token..."
access_token=$(curl -s -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=${client_id}&client_secret=${client_secret}&scope=https://graph.microsoft.com/.default" \
  "https://login.microsoftonline.com/${tenant_id}/oauth2/v2.0/token" | jq -r '.access_token')

# Check of het access token is verkregen
if [[ -z "$access_token" || "$access_token" == "null" ]]; then
  echo "Error: Failed to obtain access token."
  exit 1
fi

echo "Access token retrieved."

# Functie om de groep aan te maken
create_group() {
  local group_name="$1"
  local client_name="$2"
  local mail_nickname="$3"
  local is_dynamic_group="$4"
  local dynamic_query="$5"

  # Gebruik dezelfde beschrijving voor alle groepen
  group_description="This is a group to add individuals to the dynamic group of $client_name."

  echo "Creating group: $group_name"

  # Standaard dynamische query
  default_dynamic_query="user.memberof -any (group.objectId -in ['af13e296-a85e-4664-b010-a0a63ffab259', '9a50d1f7-3a0f-4c8b-9019-8096d5dc1e82', '9c8fd7ae-e9ab-4450-ad50-3b3c042b3b83', 'fcbe0039-abce-4d6a-a92b-c357da3f05e7'])"

  # Als de groep dynamisch is en er geen dynamische query is opgegeven, gebruik de default query
  if [[ "$is_dynamic_group" == "yes" && -z "$dynamic_query" ]]; then
    dynamic_query="$default_dynamic_query"
  fi

  # Maak de JSON-payload voor het aanmaken van de groep
  if [[ "$is_dynamic_group" == "yes" ]]; then
    response=$(curl -s -X POST https://graph.microsoft.com/v1.0/groups \
      -H "Authorization: Bearer $access_token" \
      -H "Content-Type: application/json" \
      -d '{
        "displayName": "'"$group_name"'",
        "mailEnabled": false,
        "mailNickname": "'"$mail_nickname"'",
        "securityEnabled": true,
        "description": "'"$group_description"'",
        "membershipRule": "'"$dynamic_query"'",
        "membershipRuleProcessingState": "On",
        "groupTypes": ["DynamicMembership"]
      }')
  else
    response=$(curl -s -X POST https://graph.microsoft.com/v1.0/groups \
      -H "Authorization: Bearer $access_token" \
      -H "Content-Type: application/json" \
      -d '{
        "displayName": "'"$group_name"'",
        "mailEnabled": false,
        "mailNickname": "'"$mail_nickname"'",
        "securityEnabled": true,
        "description": "'"$group_description"'"
      }')
  fi

  # Controleer of de groep succesvol is aangemaakt
  if echo "$response" | grep -q '"id"'; then
    echo "Group created successfully!"
    echo "Response: $response"
  else
    echo "Failed to create group."
    echo "Response: $response"
  fi
}

# Hoofdlogica

# Vraag of de gebruiker een lijst wil importeren
read -p "Do you want to import a list of group names from a file? (yes/no): " import_list

if [[ "$import_list" == "yes" ]]; then
  read -p "Enter the filename: " filename
  if [[ -f "$filename" ]]; then
    while IFS= read -r group_name; do
      read -p "Enter the client name for this group: " client_name
      mail_nickname=$(echo "$group_name" | tr '[:upper:]' '[:lower:]' | tr ' ' '_')
      read -p "Should '$group_name' be a dynamic group? (yes/no): " is_dynamic_group
      if [[ "$is_dynamic_group" == "yes" ]]; then
        dynamic_query=""
        read -p "Enter the dynamic query for membership (leave empty to use default): " dynamic_query
        if [[ -z "$dynamic_query" ]]; then
          dynamic_query="user.memberof -any (group.objectId -in ['af13e296-a85e-4664-b010-a0a63ffab259', '9a50d1f7-3a0f-4c8b-9019-8096d5dc1e82', '9c8fd7ae-e9ab-4450-ad50-3b3c042b3b83', 'fcbe0039-abce-4d6a-a92b-c357da3f05e7'])"
        fi
      else
        dynamic_query=""
      fi
      create_group "$group_name" "$client_name" "$mail_nickname" "$is_dynamic_group" "$dynamic_query"
    done < "$filename"
  else
    echo "File not found: $filename"
  fi
elif [[ "$import_list" == "no" ]]; then
  while true; do
    read -p "Enter the client name: " client_name
    read -p "Enter the name of the group you want to create: " group_name
    mail_nickname=$(echo "$group_name" | tr '[:upper:]' '[:lower:]' | tr ' ' '_')
    read -p "Should '$group_name' be a dynamic group? (yes/no): " is_dynamic_group
    if [[ "$is_dynamic_group" == "yes" ]]; then
      dynamic_query=""
      read -p "Enter the dynamic query for membership (leave empty to use default): " dynamic_query
      if [[ -z "$dynamic_query" ]]; then
        dynamic_query="user.memberof -any (group.objectId -in ['af13e296-a85e-4664-b010-a0a63ffab259', '9a50d1f7-3a0f-4c8b-9019-8096d5dc1e82', '9c8fd7ae-e9ab-4450-ad50-3b3c042b3b83', 'fcbe0039-abce-4d6a-a92b-c357da3f05e7'])"
      fi
    else
      dynamic_query=""
    fi
    create_group "$group_name" "$client_name" "$mail_nickname" "$is_dynamic_group" "$dynamic_query"
    
    # Vraag of de gebruiker nog meer groepen wil maken
    read -p "Do you want to add more groups? (yes/no): " add_more
    if [[ "$add_more" != "yes" ]]; then
      break
    fi
  done
else
  echo "Please enter 'yes' or 'no'."
fi

echo "All groups created successfully."
