#!/usr/bin/env python3
"""
Dynapps MFA Compliance Check
Controleert welke gebruikers geen enkele MFA methode hebben ingesteld
en rapporteert dit wekelijks via Slack.

Scope:
- Alle actieve Member accounts (geen guests)
- Skips admin accounts (.admin@)
- Gebruikt Microsoft Graph authenticationMethods API

CI/CD Variables:
  AZURE_TENANT_ID       - Tenant ID
  AZURE_CLIENT_ID       - Client ID van gitlab-automation-dynapps
  AZURE_CLIENT_SECRET   - Client Secret
  SLACK_WEBHOOK_URL     - Slack Incoming Webhook URL
"""

import os
import datetime
import requests
import sys
import time

# =========================
# Config (CI/CD variables)
# =========================
TENANT_ID     = os.getenv("AZURE_TENANT_ID", "").strip()
CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "").strip()
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "").strip()

MFA_EXCLUSION_GROUP_ID = os.getenv("MFA_EXCLUSION_GROUP_ID", "").strip()

MFA_EXCLUSION_GROUP_ID = os.getenv("MFA_EXCLUSION_GROUP_ID", "").strip()

GRAPH_V1   = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"

# MFA methodes die tellen als "echte" MFA (niet password)
MFA_METHOD_TYPES = {
    "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod",
    "#microsoft.graph.phoneAuthenticationMethod",
    "#microsoft.graph.fido2AuthenticationMethod",
    "#microsoft.graph.windowsHelloForBusinessAuthenticationMethod",
    "#microsoft.graph.softwareOathAuthenticationMethod",
    "#microsoft.graph.temporaryAccessPassAuthenticationMethod",
}


# =========================
# Auth
# =========================
def get_token() -> str:
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        raise RuntimeError("AZURE_TENANT_ID, AZURE_CLIENT_ID of AZURE_CLIENT_SECRET ontbreekt.")
    payload = {
        "client_id":     CLIENT_ID,
        "scope":         "https://graph.microsoft.com/.default",
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
    }
    r = requests.post(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        data=payload, timeout=30,
    )
    if "access_token" not in r.json():
        raise RuntimeError(f"Token error: {r.text}")
    return r.json()["access_token"]


# =========================
# Graph helpers
# =========================
def graph_get(token: str, url: str, retries: int = 3):
    for attempt in range(retries):
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", 10))
            print(f"  THROTTLED: wacht {retry_after}s...")
            time.sleep(retry_after)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Max retries bereikt voor {url}")


def graph_post(token: str, url: str, payload: dict):
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def is_excluded(token: str, user_id: str) -> bool:
    if not MFA_EXCLUSION_GROUP_ID:
        return False
    try:
        data = graph_post(
            token,
            f"{GRAPH_V1}/users/{user_id}/checkMemberGroups",
            {"groupIds": [MFA_EXCLUSION_GROUP_ID]},
        )
        return bool(data and len(data.get("value", [])) > 0)
    except Exception:
        return False


def graph_post(token: str, url: str, payload: dict):
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def is_excluded(token: str, user_id: str) -> bool:
    if not MFA_EXCLUSION_GROUP_ID:
        return False
    try:
        data = graph_post(
            token,
            f"{GRAPH_V1}/users/{user_id}/checkMemberGroups",
            {"groupIds": [MFA_EXCLUSION_GROUP_ID]},
        )
        return bool(data and len(data.get("value", [])) > 0)
    except Exception as e:
        print(f"  EXCLUSION_CHECK_ERROR: {user_id} - {e}")
        return False


# =========================
# Slack
# =========================
def slack_post(text: str) -> bool:
    if not SLACK_WEBHOOK:
        return False
    try:
        r = requests.post(
            SLACK_WEBHOOK,
            json={"username": "Dynapps Automation", "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"SLACK_ERROR: {e}")
        return False


# =========================
# Core Logic
# =========================
# Mailbox types die geen MFA nodig hebben
EXCLUDED_PURPOSES = {"room", "equipment", "shared"}

def get_members(token: str) -> list:
    """Haal alle actieve Member accounts op via beta (voor userPurpose filter)."""
    url = (
        f"{GRAPH_BETA}/users"
        f"?$filter=userType eq 'Member' and accountEnabled eq true"
        f"&$select=id,displayName,userPrincipalName,userPurpose"
        f"&$top=999"
    )
    users = []
    while url:
        data = graph_get(token, url)
        if not data:
            break
        for user in data.get("value", []):
            purpose = (user.get("userPurpose") or "user").lower()
            if purpose not in EXCLUDED_PURPOSES:
                users.append(user)
        url = data.get("@odata.nextLink")
    return users


def has_mfa(token: str, user_id: str) -> bool:
    """Controleer of gebruiker minstens één MFA methode heeft."""
    url = f"{GRAPH_V1}/users/{user_id}/authentication/methods"
    data = graph_get(token, url)
    if not data:
        return False
    for method in data.get("value", []):
        odata_type = method.get("@odata.type", "")
        if odata_type in MFA_METHOD_TYPES:
            return True
    return False


def is_admin_account(upn: str) -> bool:
    return ".admin@" in upn.lower()


# =========================
# Main
# =========================
def main():
    start_local = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{start_local}] MFA compliance check gestart")

    token = get_token()

    members = get_members(token)
    print(f"  Actieve member accounts gevonden: {len(members)}")

    no_mfa    = []
    errors    = []
    skipped   = 0
    compliant = 0

    for user in members:
        upn     = user.get("userPrincipalName", "")
        user_id = user["id"]
        name    = user.get("displayName") or upn

        if is_admin_account(upn):
            skipped += 1
            continue

        time.sleep(0.2)
        if is_excluded(token, user_id):
            skipped += 1
            print(f"  EXCLUDED: {upn}")
            continue
        try:
            if has_mfa(token, user_id):
                compliant += 1
                print(f"  OK: {upn}")
            else:
                no_mfa.append((name, upn))
                print(f"  NO_MFA: {upn}")
        except Exception as e:
            errors.append((name, upn, str(e)))
            print(f"  ERROR: {upn} - {e}")

    total_checked = compliant + len(no_mfa)
    compliance_pct = round((compliant / total_checked * 100)) if total_checked > 0 else 0

    print(f"\nCompliant: {compliant} | Geen MFA: {len(no_mfa)} | Errors: {len(errors)} | Skipped (admin): {skipped}\n")

    # Slack bericht
    if not no_mfa and not errors:
        slack_post(
            f":white_check_mark: *Dynapps MFA Compliance* ({start_local})\n"
            f"Alle {compliant} gebruikers hebben MFA ingesteld. Compliance: 100% :tada:"
        )
        return

    emoji = ":white_check_mark:" if compliance_pct >= 90 else ":warning:" if compliance_pct >= 70 else ":red_circle:"

    lines = [
        f":lock: *Dynapps MFA Compliance Check* ({start_local})",
        f"*Compliance: {compliance_pct}%* ({compliant}/{total_checked} gebruikers) {emoji}",
        "",
    ]

    if no_mfa:
        lines.append(f"*:no_entry: Geen MFA ingesteld ({len(no_mfa)}):*")
        for name, upn in sorted(no_mfa, key=lambda x: x[1]):
            lines.append(f"  • *{name}* — {upn}")
        lines.append("")

    if errors:
        lines.append(f"*:warning: Errors ({len(errors)}):*")
        for name, upn, err in errors:
            lines.append(f"  • *{name}* ({upn}) — {err}")

    slack_post("\n".join(lines))


if __name__ == "__main__":
    main()