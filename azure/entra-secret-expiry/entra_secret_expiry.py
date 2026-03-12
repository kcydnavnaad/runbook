#!/usr/bin/env python3
"""
Entra App Registration - Secret Expiry Check
Controleert vervaldatum van secrets in alle App Registrations
en stuurt een Slack melding met categorisering op vervaldatum.

CI/CD Variables:
  AZURE_TENANT_ID       - Tenant ID
  AZURE_CLIENT_ID       - Client ID van gitlab-automation-dynapps
  AZURE_CLIENT_SECRET   - Client Secret
  SLACK_WEBHOOK_URL     - Slack Incoming Webhook URL
"""

import requests
import datetime
import os
import sys
from dateutil import parser
import pytz

# =========================
# Config (CI/CD variables)
# =========================
CLIENT_ID     = os.getenv("AZURE_CLIENT_ID")
TENANT_ID     = os.getenv("AZURE_TENANT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")

if not all([CLIENT_ID, TENANT_ID, CLIENT_SECRET, SLACK_WEBHOOK]):
    print("Error: AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_CLIENT_SECRET of SLACK_WEBHOOK_URL ontbreekt.")
    sys.exit(1)

AUTH_URL      = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
GRAPH_API_URL = "https://graph.microsoft.com/v1.0/applications"

# =========================
# Drempelwaarden (dagen)
# =========================
KRITIEK    = 14
URGENT     = 30
WAARSCHUWING = 60

# =========================
# Helpers
# =========================
def get_access_token() -> str:
    print("Authenticating and obtaining access token...")
    payload = {
        "client_id":     CLIENT_ID,
        "scope":         "https://graph.microsoft.com/.default",
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
    }
    r = requests.post(AUTH_URL, data=payload, timeout=30)
    if r.status_code != 200:
        print(f"Error: {r.text}")
        sys.exit(1)
    token = r.json().get("access_token")
    if not token:
        print("Error: No access_token in response.")
        sys.exit(1)
    print("Access token obtained successfully.")
    return token


def format_date(dt: datetime.datetime) -> str:
    return dt.strftime("%d-%m-%Y")


def days_remaining(end: datetime.datetime, now: datetime.datetime) -> int:
    return (end - now).days


def send_to_slack(message: str) -> None:
    r = requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=30)
    if r.status_code == 200:
        print("Slack message sent successfully.")
    else:
        print(f"Failed to send Slack message: {r.text}")


# =========================
# Core Logic
# =========================
def check_secret_expiry(token: str) -> None:
    print("Fetching app registrations...")
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(GRAPH_API_URL, headers=headers, timeout=60)
    if r.status_code != 200:
        print(f"Error fetching app registrations: {r.text}")
        sys.exit(1)

    utc = pytz.utc
    now = datetime.datetime.now(utc)

    verlopen   = []   # < 0 dagen
    kritiek    = []   # 0–14 dagen
    urgent     = []   # 14–30 dagen
    waarschuwing = [] # 30–60 dagen

    for app in r.json().get("value", []):
        app_name = app.get("displayName")
        for secret in app.get("passwordCredentials", []):
            end  = parser.isoparse(secret["endDateTime"])
            days = days_remaining(end, now)

            if days < 0:
                verlopen.append((app_name, end, days))
            elif days <= KRITIEK:
                kritiek.append((app_name, end, days))
            elif days <= URGENT:
                urgent.append((app_name, end, days))
            elif days <= WAARSCHUWING:
                waarschuwing.append((app_name, end, days))

    # Samenvatting bovenaan
    totaal = len(verlopen) + len(kritiek) + len(urgent) + len(waarschuwing)

    if totaal == 0:
        msg = (
            ":tada: *Entra App Registrations — Secret Expiry Check*\n\n"
            "✅ Alle secrets zijn up-to-date. Geen actie vereist."
        )
        send_to_slack(msg)
        return

    lines = [
        ":key: *Entra App Registrations — Secret Expiry Check*\n",
        f"*Samenvatting:* {totaal} secret(s) vereisen aandacht",
        f"  • 🔴 Vervallen: {len(verlopen)}",
        f"  • 🚨 Kritiek (minder dan {KRITIEK} dagen): {len(kritiek)}",
        f"  • 🟠 Urgent (minder dan {URGENT} dagen): {len(urgent)}",
        f"  • 🟡 Waarschuwing (minder dan {WAARSCHUWING} dagen): {len(waarschuwing)}",
        "",
    ]

    if verlopen:
        lines.append(f"*🔴 Vervallen secrets ({len(verlopen)}):*")
        for name, end, days in sorted(verlopen, key=lambda x: x[1]):
            lines.append(f"  • *{name}* — Vervallen op {format_date(end)} ({abs(days)} dagen geleden)")
        lines.append("")

    if kritiek:
        lines.append(f"*🚨 Kritiek — Minder dan {KRITIEK} dagen geldig ({len(kritiek)}):*")
        for name, end, days in sorted(kritiek, key=lambda x: x[1]):
            lines.append(f"  • *{name}* — Vervalt op {format_date(end)} (nog {days} dagen)")
        lines.append("")

    if urgent:
        lines.append(f"*🟠 Urgent — Minder dan {URGENT} dagen geldig ({len(urgent)}):*")
        for name, end, days in sorted(urgent, key=lambda x: x[1]):
            lines.append(f"  • *{name}* — Vervalt op {format_date(end)} (nog {days} dagen)")
        lines.append("")

    if waarschuwing:
        lines.append(f"*🟡 Waarschuwing — Minder dan {WAARSCHUWING} dagen geldig ({len(waarschuwing)}):*")
        for name, end, days in sorted(waarschuwing, key=lambda x: x[1]):
            lines.append(f"  • *{name}* — Vervalt op {format_date(end)} (nog {days} dagen)")

    send_to_slack("\n".join(lines))


# =========================
# Main
# =========================
if __name__ == "__main__":
    token = get_access_token()
    check_secret_expiry(token)
    print("Done.")