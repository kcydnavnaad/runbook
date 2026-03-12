#!/usr/bin/env bash
set -euo pipefail

die() { echo "❌ $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Missing dependency: $1"; }

need kubectl
need grep
need sed
need sort
need base64

echo "🔎 Current context: $(kubectl config current-context)"

# -----------------------------
# Choose context (number only - kubectl "select" is fine here)
# -----------------------------
echo
echo "🌐 Available kubectl contexts:"
mapfile -t CTX < <(kubectl config get-contexts -o name)
((${#CTX[@]} > 0)) || die "No contexts found in kubeconfig."

select CHOSEN_CTX in "${CTX[@]}"; do
  [[ -n "${CHOSEN_CTX:-}" ]] || { echo "Choose a valid number."; continue; }
  kubectl config use-context "$CHOSEN_CTX" >/dev/null
  break
done
echo "✅ Using context: $(kubectl config current-context)"

# -----------------------------
# Choose namespace (number OR name)
# -----------------------------
echo
echo "📦 Fetching namespaces..."
mapfile -t NS < <(kubectl get ns -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)
((${#NS[@]} > 0)) || die "No namespaces found (or no access)."

echo "📦 Available namespaces:"
i=1
for ns in "${NS[@]}"; do
  echo "  $i) $ns"
  ((i++))
done

while true; do
  read -r -p "Choose namespace (number or name): " NSINPUT

  # Number -> index
  if [[ "$NSINPUT" =~ ^[0-9]+$ ]]; then
    idx=$((NSINPUT - 1))
    if [[ $idx -ge 0 && $idx -lt ${#NS[@]} ]]; then
      CHOSEN_NS="${NS[$idx]}"
      break
    fi
  else
    # Name -> exact match
    for ns in "${NS[@]}"; do
      if [[ "$ns" == "$NSINPUT" ]]; then
        CHOSEN_NS="$ns"
        break 2
      fi
    done
  fi

  echo "❌ Invalid choice. Try again."
done

echo "✅ Namespace: $CHOSEN_NS"

# -----------------------------
# Optional filter term
# -----------------------------
echo
read -r -p "🔎 Filter term (optional, e.g. zelektro-19 or production) [empty = show all odoo secrets]: " FILTER
FILTER="${FILTER:-}"

# -----------------------------
# Find Odoo secrets in namespace
# -----------------------------
echo
echo "🔐 Searching for Odoo secrets in '$CHOSEN_NS'..."
if [[ -n "$FILTER" ]]; then
  mapfile -t SECRETS < <(
    kubectl -n "$CHOSEN_NS" get secrets -o name \
      | sed 's|^secret/||' \
      | grep -i 'odoo-secret' \
      | grep -i "$FILTER" || true
  )
else
  mapfile -t SECRETS < <(
    kubectl -n "$CHOSEN_NS" get secrets -o name \
      | sed 's|^secret/||' \
      | grep -i 'odoo-secret' || true
  )
fi

((${#SECRETS[@]} > 0)) || die "No matching odoo secrets found in namespace '$CHOSEN_NS'."

echo "🧾 Found secrets:"
i=1
for s in "${SECRETS[@]}"; do
  echo "  $i) $s"
  ((i++))
done

# Choose secret (number OR name)
while true; do
  read -r -p "Choose secret (number or exact name): " SINPUT

  if [[ "$SINPUT" =~ ^[0-9]+$ ]]; then
    idx=$((SINPUT - 1))
    if [[ $idx -ge 0 && $idx -lt ${#SECRETS[@]} ]]; then
      SECRET_NAME="${SECRETS[$idx]}"
      break
    fi
  else
    for s in "${SECRETS[@]}"; do
      if [[ "$s" == "$SINPUT" ]]; then
        SECRET_NAME="$s"
        break 2
      fi
    done
  fi

  echo "❌ Invalid choice. Try again."
done

echo
echo "✅ Selected secret: $SECRET_NAME"

# -----------------------------
# List keys (IMPORTANT FIX)
# Your previous script used a jsonpath "range $k,$v := .data" form that kubectl jsonpath DOES NOT support reliably.
# We use Go-templates instead; they handle keys like "odoo-password" correctly.
# -----------------------------
echo
echo "🗝️ Keys in secret:"

# Quick check if the secret even has .data (and whether you have access)
DATA_COUNT="$(
  kubectl -n "$CHOSEN_NS" get secret "$SECRET_NAME" \
    -o go-template='{{if .data}}{{len .data}}{{else}}0{{end}}' 2>/dev/null || echo "0"
)"

if [[ "$DATA_COUNT" == "0" ]]; then
  echo "❌ No data keys found in secret."
  echo "   Possible causes:"
  echo "   - RBAC: you can see the Secret object name, but not read its data"
  echo "   - It's not populated (rare for Odoo secrets, but possible)"
  echo
  echo "🔎 Debug info:"
  kubectl -n "$CHOSEN_NS" get secret "$SECRET_NAME" -o yaml | sed -n '1,80p' || true
  exit 1
fi

mapfile -t KEYS < <(
  kubectl -n "$CHOSEN_NS" get secret "$SECRET_NAME" \
    -o go-template='{{range $k,$v := .data}}{{println $k}}{{end}}' \
    | sed '/^\s*$/d'
)

((${#KEYS[@]} > 0)) || die "No data keys found in secret (or no access)."

i=1
for k in "${KEYS[@]}"; do
  echo "  $i) $k"
  ((i++))
done

echo
read -r -p "Key to decode [odoo-password]: " KEYINPUT
KEYINPUT="${KEYINPUT:-odoo-password}"

# If user types a number here, map it as well
if [[ "$KEYINPUT" =~ ^[0-9]+$ ]]; then
  kidx=$((KEYINPUT - 1))
  if [[ $kidx -ge 0 && $kidx -lt ${#KEYS[@]} ]]; then
    KEY="${KEYS[$kidx]}"
  else
    die "Invalid key number."
  fi
else
  KEY="$KEYINPUT"
fi

# -----------------------------
# Get selected key's base64 value using Go-template index (handles hyphens in key names)
# -----------------------------
VAL_B64="$(
  kubectl -n "$CHOSEN_NS" get secret "$SECRET_NAME" \
    -o go-template="{{ index .data \"$KEY\" }}" 2>/dev/null || true
)"

if [[ -z "${VAL_B64:-}" ]]; then
  echo "❌ Key '$KEY' not found or empty. Available keys:"
  printf ' - %s\n' "${KEYS[@]}"
  exit 1
fi

echo
echo "🔓 Decoded value for key '$KEY':"
echo "$VAL_B64" | base64 -d
echo