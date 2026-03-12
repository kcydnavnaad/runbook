#!/usr/bin/env python3
"""
Dynapps Offboarding - APPLY
Zet OOO in op disabled users in ALLOWED_DOMAINS.

Scope:
- Disabled users in ALLOWED_DOMAINS
- Skips admin accounts (.admin@)
- Skips users in OFFBOARDING_EXCLUSION_GROUP_ID
- Overschrijft OOO altijd met landspecifieke template (HTML)
- Externe auto-replies ingeschakeld (externalAudience=all)

Notifications:
- Slack alleen bij nieuwe updates of errors

CI/CD Variables:
  AZURE_TENANT_ID                 - Tenant ID
  AZURE_CLIENT_ID                 - Client ID van gitlab-automation-dynapps
  AZURE_CLIENT_SECRET             - Client Secret
  SLACK_WEBHOOK_URL               - Slack Incoming Webhook URL
  SLACK_CHANNEL                   - Slack channel (bv. #it-automation-alerts)
  ALLOWED_DOMAINS                 - Kommagescheiden lijst van domeinen
  CH_SUPPORT                      - Support email voor dynapps.ch
  FR_FALLBACK_SUPPORT             - Fallback support email voor dynapps.fr
  OFFBOARDING_EXCLUSION_GROUP_ID  - Groep ID van exclusie groep
"""

import os
import json
import time
import datetime
import requests

# =========================
# Config (CI/CD variables)
# =========================
TENANT_ID     = os.getenv("AZURE_TENANT_ID", "").strip()
CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "").strip()

ALLOWED_DOMAINS = set(
    d.strip().lower()
    for d in os.getenv("ALLOWED_DOMAINS", "").split(",")
    if d.strip()
)

CH_SUPPORT          = os.getenv("CH_SUPPORT", "support@dynapps.ch").strip()
FR_FALLBACK_SUPPORT = os.getenv("FR_FALLBACK_SUPPORT", "support-effiscience@dynapps.be").strip()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
SLACK_CHANNEL     = os.getenv("SLACK_CHANNEL", "#it-automation-alerts").strip()

OFFBOARDING_EXCLUSION_GROUP_ID = os.getenv("OFFBOARDING_EXCLUSION_GROUP_ID", "").strip()

STATE_PATH = "state/offboarding_state.json"
GRAPH      = "https://graph.microsoft.com/v1.0"


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
# State
# =========================
def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)

def get_last_notified_set(state: dict) -> set:
    raw = state.get("last_notified_updated_users", [])
    return set(str(x).lower() for x in raw) if isinstance(raw, list) else set()

def set_last_notified_set(state: dict, upns: set) -> None:
    state["last_notified_updated_users"] = sorted(u.lower() for u in upns)
    state["last_run_utc"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =========================
# Graph helpers
# =========================
def graph_get(token: str, url: str):
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def graph_post(token: str, url: str, payload: dict):
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def graph_patch(token: str, url: str, payload: dict):
    r = requests.patch(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return True


# =========================
# Slack
# =========================
def slack_post(text: str) -> bool:
    if not SLACK_WEBHOOK_URL:
        return False
    try:
        r = requests.post(
            SLACK_WEBHOOK_URL,
            json={"channel": SLACK_CHANNEL, "username": "Dynapps Automation", "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"SLACK_ERROR: {e}")
        return False


# =========================
# Business logic
# =========================
def in_scope(upn: str) -> bool:
    if not upn or "@" not in upn:
        return False
    if ".admin@" in upn.lower():
        return False
    domain = upn.split("@", 1)[1].lower()
    return domain in ALLOWED_DOMAINS

def is_excluded(token: str, user_id: str) -> bool:
    if not OFFBOARDING_EXCLUSION_GROUP_ID:
        return False
    def _check_once() -> bool:
        data = graph_post(
            token,
            f"{GRAPH}/users/{user_id}/checkMemberGroups",
            {"groupIds": [OFFBOARDING_EXCLUSION_GROUP_ID]},
        )
        return bool(data and len(data.get("value", [])) > 0)
    try:
        if _check_once():
            return True
        time.sleep(8)
        return _check_once()
    except Exception as e:
        print(f"ERROR: exclusion check failed for user_id={user_id}: {e}")
        return False

def get_manager_email(token: str, user_id: str):
    try:
        mgr = graph_get(token, f"{GRAPH}/users/{user_id}/manager?$select=mail,userPrincipalName")
        if mgr is None:
            return None
        return mgr.get("mail") or mgr.get("userPrincipalName")
    except Exception:
        return None

def mailbox_exists(token: str, user_id: str) -> bool:
    data = graph_get(token, f"{GRAPH}/users/{user_id}/mailboxSettings?$select=automaticRepliesSetting")
    return data is not None

def build_template(domain: str, user_display_name: str, manager_email) -> str:
    if domain == "dynapps.ch":
        contact = CH_SUPPORT
    elif domain == "dynapps.fr":
        contact = manager_email if manager_email else FR_FALLBACK_SUPPORT
    else:
        contact = FR_FALLBACK_SUPPORT

    return (
        "Bonjour,<br><br>"
        "Je ne travaille plus au sein de l'entreprise et n'ai donc plus accès à cette messagerie.<br>"
        "Pour toute question ou demande, merci de contacter directement le support à l'adresse suivante : "
        f"{contact}.<br><br>"
        "Je vous remercie de votre compréhension et vous souhaite une agréable journée.<br><br>"
        "Meilleures salutations,<br><br>"
        f"{user_display_name}"
    )

def set_ooo(token: str, user_id: str, message: str) -> bool:
    payload = {
        "automaticRepliesSetting": {
            "status":               "alwaysEnabled",
            "externalAudience":     "all",
            "internalReplyMessage": message,
            "externalReplyMessage": message,
        }
    }
    return graph_patch(token, f"{GRAPH}/users/{user_id}/mailboxSettings", payload) is not None


# =========================
# Main
# =========================
def main():
    start_local = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    token       = get_token()
    state       = load_state()
    last_notified = get_last_notified_set(state)

    url = (
        f"{GRAPH}/users?"
        f"$filter=accountEnabled eq false&"
        f"$select=id,displayName,userPrincipalName,userType"
    )

    updated          = 0
    skipped_excluded = 0
    skipped_no_mailbox = 0
    errors           = 0
    changed_users: list = []
    error_users: list   = []

    while url:
        data = graph_get(token, url)
        if data is None:
            break

        for user in data.get("value", []):
            if user.get("userType") != "Member":
                continue

            upn = user.get("userPrincipalName", "")
            if not in_scope(upn):
                continue

            user_id   = user["id"]
            user_name = user.get("displayName") or upn
            domain    = upn.split("@", 1)[1].lower()

            if is_excluded(token, user_id):
                print(f"SKIP (EXCLUDED): {upn}")
                skipped_excluded += 1
                continue

            if not mailbox_exists(token, user_id):
                print(f"SKIP (NO_MAILBOX): {upn}")
                skipped_no_mailbox += 1
                continue

            manager_email = get_manager_email(token, user_id)
            message       = build_template(domain, user_name, manager_email)

            try:
                if set_ooo(token, user_id, message):
                    print(f"OK: OOO updated for {upn}")
                    updated += 1
                    changed_users.append(upn)
                else:
                    print(f"SKIP (NO_MAILBOX): {upn}")
                    skipped_no_mailbox += 1
            except Exception as e:
                print(f"ERROR: {upn} - {e}")
                errors += 1
                error_users.append(upn)

        url = data.get("@odata.nextLink")

    print(f"\nOOO Updated: {updated} | Excluded: {skipped_excluded} | No mailbox: {skipped_no_mailbox} | Errors: {errors}\n")

    current_changed_set = set(u.lower() for u in changed_users)
    newly_changed       = sorted(current_changed_set - last_notified)
    should_notify       = bool(newly_changed) or bool(errors)

    if should_notify:
        lines = [
            f":robot_face: *Dynapps Offboarding Automation* ({start_local})",
            f"*Result:* Updated={updated} | Excluded={skipped_excluded} | NoMailbox={skipped_no_mailbox} | Errors={errors}",
        ]
        if newly_changed:
            lines.append(f"*Nieuw bijgewerkt ({len(newly_changed)}):*")
            lines.extend([f"• {u}" for u in newly_changed])
        if error_users:
            lines.append(f"*Errors ({len(error_users)}):*")
            lines.extend([f"• {u}" for u in error_users])
        slack_post("\n".join(lines))
        set_last_notified_set(state, current_changed_set)
    else:
        set_last_notified_set(state, last_notified)

    save_state(state)


if __name__ == "__main__":
    main()