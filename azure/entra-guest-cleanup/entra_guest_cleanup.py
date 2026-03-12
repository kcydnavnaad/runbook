#!/usr/bin/env python3
"""
Dynapps Entra Guest Cleanup
Controleert guest accounts op inactiviteit en handelt automatisch:
  - >180 dagen inactief → account disablen
  - >365 dagen inactief → account verwijderen

Scope:
- Entra guest accounts (userType == Guest)
- Skips accounts zonder lastSignInDateTime (nooit ingelogd → apart gerapporteerd)
- Verwijderde accounts worden uit state verwijderd

CI/CD Variables:
  AZURE_TENANT_ID       - Tenant ID
  AZURE_CLIENT_ID       - Client ID van gitlab-automation-dynapps
  AZURE_CLIENT_SECRET   - Client Secret
  SLACK_WEBHOOK_URL     - Slack Incoming Webhook URL
"""

import os
import json
import datetime
import requests
import pytz

# =========================
# Config (CI/CD variables)
# =========================
TENANT_ID     = os.getenv("AZURE_TENANT_ID", "").strip()
CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "").strip()
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "").strip()

DISABLE_DAYS = 180
DELETE_DAYS  = 365
GRAPH        = "https://graph.microsoft.com/v1.0"
STATE_PATH   = "state/guest_cleanup_state.json"


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


# =========================
# Graph helpers
# =========================
def graph_get(token: str, url: str):
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def graph_patch(token: str, url: str, payload: dict):
    r = requests.patch(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return True

def graph_delete(token: str, url: str) -> bool:
    r = requests.delete(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


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
def get_all_guests(token: str) -> list:
    """Haal alle guest accounts op — actief én disabled — inclusief signInActivity."""
    url = (
        f"{GRAPH}/users"
        f"?$filter=userType eq 'Guest'"
        f"&$select=id,displayName,userPrincipalName,mail,accountEnabled,signInActivity,createdDateTime"
        f"&$top=999"
    )
    users = []
    while url:
        data = graph_get(token, url)
        if not data:
            break
        users.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return users


def get_last_signin_from_user(user: dict):
    """Haal laatste sign-in op uit signInActivity property op het user object."""
    activity = user.get("signInActivity")
    if not activity:
        return None
    last = activity.get("lastSignInDateTime") or activity.get("lastNonInteractiveSignInDateTime")
    if not last:
        return None
    try:
        return datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
    except Exception:
        return None


def get_created_date(user: dict):
    """Haal aanmaakdatum op uit createdDateTime property."""
    created = user.get("createdDateTime")
    if not created:
        return None
    try:
        return datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except Exception:
        return None


def disable_user(token: str, user_id: str) -> bool:
    return graph_patch(token, f"{GRAPH}/users/{user_id}", {"accountEnabled": False}) is not None

def delete_user(token: str, user_id: str) -> bool:
    return graph_delete(token, f"{GRAPH}/users/{user_id}")


# =========================
# Main
# =========================
def main():
    start_local = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{start_local}] Guest cleanup gestart")

    token = get_token()
    state = load_state()
    already_disabled = set(state.get("disabled_guests", []))
    now = datetime.datetime.now(pytz.utc)

    guests = get_all_guests(token)
    print(f"  Guest accounts gevonden: {len(guests)}")

    deleted_now     = []
    disabled_now    = []
    never_signed_in = []
    errors          = []
    skipped         = 0

    for user in guests:
        upn        = user.get("userPrincipalName", "")
        user_id    = user["id"]
        name       = user.get("displayName") or upn
        mail       = user.get("mail") or upn
        is_enabled = user.get("accountEnabled", True)

        last_signin = get_last_signin_from_user(user)

        if last_signin is None:
            # Nooit ingelogd — gebruik aanmaakdatum als fallback
            created = get_created_date(user)
            if created is None:
                never_signed_in.append((name, mail, "aanmaakdatum onbekend"))
                print(f"  NEVER_SIGNED_IN (no date): {upn}")
                continue
            days_since_created = (now - created).days
            if days_since_created >= DELETE_DAYS:
                try:
                    if delete_user(token, user_id):
                        deleted_now.append((name, mail, days_since_created))
                        already_disabled.discard(upn.lower())
                        print(f"  DELETED (nooit ingelogd, {days_since_created}d oud): {upn}")
                    else:
                        errors.append((name, mail, "delete mislukt"))
                except Exception as e:
                    errors.append((name, mail, str(e)))
                    print(f"  ERROR: {upn} - {e}")
            elif days_since_created >= DISABLE_DAYS and is_enabled:
                if upn.lower() not in already_disabled:
                    try:
                        if disable_user(token, user_id):
                            disabled_now.append((name, mail, days_since_created))
                            already_disabled.add(upn.lower())
                            print(f"  DISABLED (nooit ingelogd, {days_since_created}d oud): {upn}")
                        else:
                            errors.append((name, mail, "disable mislukt"))
                    except Exception as e:
                        errors.append((name, mail, str(e)))
                        print(f"  ERROR: {upn} - {e}")
                else:
                    skipped += 1
            else:
                never_signed_in.append((name, mail, f"aangemaakt {days_since_created} dagen geleden"))
                print(f"  NEVER_SIGNED_IN ({days_since_created}d oud): {upn}")
            continue

        days_inactive = (now - last_signin).days

        # >365 dagen → verwijderen (ook als al disabled)
        if days_inactive >= DELETE_DAYS:
            try:
                if delete_user(token, user_id):
                    deleted_now.append((name, mail, days_inactive))
                    already_disabled.discard(upn.lower())
                    print(f"  DELETED: {upn} ({days_inactive} dagen inactief)")
                else:
                    errors.append((name, mail, "delete mislukt"))
            except Exception as e:
                errors.append((name, mail, str(e)))
                print(f"  ERROR: {upn} - {e}")

        # >180 dagen maar <365 → disablen (enkel als nog actief)
        elif days_inactive >= DISABLE_DAYS and is_enabled:
            if upn.lower() in already_disabled:
                skipped += 1
                continue
            try:
                if disable_user(token, user_id):
                    disabled_now.append((name, mail, days_inactive))
                    already_disabled.add(upn.lower())
                    print(f"  DISABLED: {upn} ({days_inactive} dagen inactief)")
                else:
                    errors.append((name, mail, "disable mislukt"))
            except Exception as e:
                errors.append((name, mail, str(e)))
                print(f"  ERROR: {upn} - {e}")

        else:
            skipped += 1

    # State opslaan
    state["disabled_guests"] = sorted(already_disabled)
    state["last_run_utc"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    save_state(state)

    print(f"\nDeleted: {len(deleted_now)} | Disabled: {len(disabled_now)} | Nooit ingelogd: {len(never_signed_in)} | Errors: {len(errors)} | Skipped: {skipped}\n")

    # Slack bericht
    if not deleted_now and not disabled_now and not never_signed_in and not errors:
        slack_post(
            f":white_check_mark: *Dynapps Guest Cleanup* ({start_local})\n"
            f"Geen inactieve guests gevonden. Alles in orde."
        )
        return

    lines = [
        f":bust_in_silhouette: *Dynapps Guest Cleanup* ({start_local})",
        f"*Samenvatting:* Deleted={len(deleted_now)} | Disabled={len(disabled_now)} | Nooit ingelogd={len(never_signed_in)} | Errors={len(errors)}",
        "",
    ]

    if deleted_now:
        lines.append(f"*:wastebasket: Verwijderd ({len(deleted_now)}) — inactief >{DELETE_DAYS} dagen:*")
        for name, mail, days in sorted(deleted_now, key=lambda x: -x[2]):
            lines.append(f"  • *{name}* ({mail}) — {days} dagen inactief")
        lines.append("")

    if disabled_now:
        lines.append(f"*:no_entry: Disabled ({len(disabled_now)}) — inactief >{DISABLE_DAYS} dagen:*")
        for name, mail, days in sorted(disabled_now, key=lambda x: -x[2]):
            lines.append(f"  • *{name}* ({mail}) — {days} dagen inactief")
        lines.append("")

    if never_signed_in:
        lines.append(f"*:question: Nooit ingelogd ({len(never_signed_in)}) — recent aangemaakt, handmatige review aanbevolen:*")
        for name, mail, info in sorted(never_signed_in, key=lambda x: x[0]):
            lines.append(f"  • *{name}* ({mail}) — {info}")
        lines.append("")

    if errors:
        lines.append(f"*:warning: Errors ({len(errors)}):*")
        for name, mail, err in errors:
            lines.append(f"  • *{name}* ({mail}) — {err}")

    slack_post("\n".join(lines))


if __name__ == "__main__":
    main()