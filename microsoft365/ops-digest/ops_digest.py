#!/usr/bin/env python3
"""
Dynapps Ops Digest
Wekelijkse rapportage van M365 licenties en 1Password seats naar Slack.

CI/CD Variables:
  AZURE_TENANT_ID           - Tenant ID
  AZURE_CLIENT_ID           - Client ID van gitlab-automation-dynapps
  AZURE_CLIENT_SECRET       - Client Secret
  SLACK_WEBHOOK_URL         - Slack Incoming Webhook URL
  SLACK_MENTIONS            - Slack user mentions bij wijzigingen
  OP_SERVICE_ACCOUNT_TOKEN  - 1Password Service Account Token
"""

import os
import json
import datetime
import requests
import subprocess
from collections import defaultdict
from typing import Optional, Dict, List, Any, Tuple

# =========================
# Config (CI/CD variables)
# =========================
CLIENT_ID     = os.getenv("AZURE_CLIENT_ID")
TENANT_ID     = os.getenv("AZURE_TENANT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_MENTIONS    = os.getenv("SLACK_MENTIONS", "")
INCLUDE_GUESTS    = False

STATE_DIR           = "state"  # Relatief aan de repo root
M365_SNAPSHOT_PATH  = os.path.join(STATE_DIR, "snapshot_m365.json")
OP_SNAPSHOT_PATH    = os.path.join(STATE_DIR, "snapshot_1password.json")

OP_BIN = "/usr/local/bin/op"

GRAPH = "https://graph.microsoft.com/v1.0"

EXCLUDE_SKUS = {
    "POWER_BI_STANDARD",
    "POWERAPPS_DEV",
    "FLOW_FREE",
}

FRIENDLY = {
    "SPB":                          "Microsoft 365 Business Premium",
    "O365_BUSINESS_ESSENTIALS":     "Microsoft 365 Business Basic",
    "O365_BUSINESS_PREMIUM":        "Office 365 Business Premium (legacy)",
    "AAD_PREMIUM_P2":               "Microsoft Entra ID P2",
    "EMS":                          "Enterprise Mobility + Security (EMS)",
    "MCOEV":                        "Microsoft Teams Phone Standard",
    "PHONESYSTEM_VIRTUALUSER":      "Teams Phone Resource Account",
    "Microsoft_365_Copilot":        "Microsoft 365 Copilot",
    "Microsoft_Teams_Premium":      "Microsoft Teams Premium",
    "Microsoft_Teams_Rooms_Pro":    "Microsoft Teams Rooms Pro",
    "POWER_BI_PRO":                 "Power BI Pro",
    "PBI_PREMIUM_PER_USER":         "Power BI Premium Per User",
    "POWERAUTOMATE_ATTENDED_RPA":   "Power Automate Attended RPA",
    "RIGHTSMANAGEMENT_ADHOC":       "Rights Management Adhoc",
}

OP_BILLABLE_STATES = {"ACTIVE", "TRANSFER_STARTED", "RECOVERY_STARTED"}

# Domeinen uitsluiten uit overzicht (wel zichtbaar in wijzigingen)
OVERVIEW_EXCLUDE_DOMAINS = {"dynapps.be"}

# Vlaggen per domein
DOMAIN_FLAGS = {
    "dynapps.be": "🇧🇪",
    "dynapps.ch": "🇨🇭",
    "dynapps.fr": "🇫🇷",
    "dynapps.es": "🇪🇸",
    "dynapps.nl": "🇳🇱",
    "dynapps.eu": "🇪🇺",
}

def get_flag(domain: str) -> str:
    return DOMAIN_FLAGS.get(domain.lower(), "🏳️")


# =========================
# Helpers
# =========================
def die(msg: str) -> None:
    print(f"Error: {msg}")
    raise SystemExit(1)

def to_friendly(sku: str) -> str:
    return FRIENDLY.get(sku, sku)

def normalize_domain(email_or_upn: str) -> str:
    if not email_or_upn or "@" not in email_or_upn:
        return "unknown"
    return email_or_upn.split("@", 1)[1].lower()

def load_snapshot(path: str) -> Optional[Dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_snapshot(path: str, snapshot: Dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)

def diff_counts(prev: Optional[Dict], cur: Dict, friendly_map: bool) -> List[str]:
    prev = prev or {}
    lines: List[str] = []
    for domain in sorted(set(prev.keys()) | set(cur.keys())):
        p_items = prev.get(domain, {}) or {}
        c_items = cur.get(domain, {}) or {}
        for key in sorted(set(p_items.keys()) | set(c_items.keys())):
            p = int(p_items.get(key, 0))
            c = int(c_items.get(key, 0))
            if p == c:
                continue
            name = to_friendly(key) if friendly_map else key
            if p == 0 and c > 0:
                lines.append(f"🟢 {domain} — {name}: +{c}  (NIEUW)")
            elif p > 0 and c == 0:
                lines.append(f"🔻 {domain} — {name}: -{p}  (VERWIJDERD)")
            elif c > p:
                lines.append(f"🔼 {domain} — {name}: +{c-p}  ({p} → {c})")
            else:
                lines.append(f"🔽 {domain} — {name}: -{p-c}  ({p} → {c})")
    return lines

def format_overview(section_title: str, counts: Dict, friendly_map: bool, exclude_domains: set = None) -> str:
    exclude_domains = exclude_domains or set()
    lines: List[str] = [f"*{section_title}*"]
    filtered = {d: v for d, v in counts.items() if d not in exclude_domains}
    for domain in sorted(filtered.keys(), key=lambda d: -sum(filtered[d].values())):
        items = list(filtered[domain].items())
        if friendly_map:
            items = sorted(items, key=lambda x: to_friendly(x[0]).lower())
        else:
            items = sorted(items, key=lambda x: x[0].lower())
        total = sum(v for _, v in items)
        flag  = get_flag(domain)
        lines.append(f"{flag} *{domain}*  (totaal: {total})")
        for k, v in items:
            label = to_friendly(k) if friendly_map else k
            lines.append(f"  • {label}: {v}")
        lines.append("")
    return "\n".join(lines).strip()


def send_to_slack(message: str) -> None:
    if not SLACK_WEBHOOK_URL:
        die("SLACK_WEBHOOK_URL ontbreekt.")
    r = requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=30)
    if r.status_code != 200:
        die(f"Failed to send Slack message: {r.status_code} {r.text}")


# =========================
# M365 (Graph)
# =========================
def get_access_token() -> str:
    if not CLIENT_ID or not TENANT_ID or not CLIENT_SECRET:
        die("AZURE_CLIENT_ID, AZURE_TENANT_ID of AZURE_CLIENT_SECRET ontbreekt.")
    payload = {
        "client_id":     CLIENT_ID,
        "scope":         "https://graph.microsoft.com/.default",
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
    }
    print("Authenticating (Graph)...")
    r = requests.post(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        data=payload, timeout=30,
    )
    if r.status_code != 200:
        die(f"Failed to obtain access token: {r.text}")
    token = r.json().get("access_token")
    if not token:
        die("No access_token in response.")
    return token

def graph_get_all(url: str, headers: Dict) -> List[Dict]:
    items: List[Dict] = []
    next_url = url
    while next_url:
        r = requests.get(next_url, headers=headers, timeout=60)
        if r.status_code != 200:
            die(f"Graph GET failed: {r.status_code} {r.text}")
        data = r.json()
        items.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")
    return items

def get_sku_map(headers: Dict) -> Dict[str, str]:
    skus = graph_get_all(GRAPH + "/subscribedSkus?$select=skuId,skuPartNumber", headers)
    return {str(s["skuId"]): s["skuPartNumber"] for s in skus if s.get("skuId") and s.get("skuPartNumber")}

def build_m365_counts(headers: Dict) -> Dict:
    print("Fetching M365 license assignments per domain...")
    sku_map = get_sku_map(headers)
    users   = graph_get_all(
        GRAPH + "/users?$select=userPrincipalName,userType,assignedLicenses&$top=999",
        headers,
    )
    counts: Dict = defaultdict(lambda: defaultdict(int))
    for u in users:
        upn = u.get("userPrincipalName")
        if not upn:
            continue
        if (u.get("userType") or "").lower() == "guest":
            continue
        domain = normalize_domain(upn)
        for lic in (u.get("assignedLicenses") or []):
            sku_id   = lic.get("skuId")
            sku_part = sku_map.get(str(sku_id), str(sku_id))
            if sku_part in EXCLUDE_SKUS:
                continue
            counts[domain][sku_part] += 1
    return {d: dict(v) for d, v in counts.items()}


# =========================
# 1Password (CLI)
# =========================
def build_1password_counts() -> Tuple[Dict, Dict]:
    print("Fetching 1Password seats per domain...")
    if not os.path.exists(OP_BIN):
        die(f"1Password CLI not found at {OP_BIN}")
    op_token = os.getenv("OP_SERVICE_ACCOUNT_TOKEN")
    if not op_token:
        die("OP_SERVICE_ACCOUNT_TOKEN ontbreekt.")
    env = os.environ.copy()
    env["OP_SERVICE_ACCOUNT_TOKEN"] = op_token
    try:
        raw = subprocess.check_output(
            [OP_BIN, "user", "list", "--format=json"],
            env=env, stderr=subprocess.STDOUT, text=True,
        )
    except subprocess.CalledProcessError as e:
        die(f"`op user list` failed: {e.output.strip()}")
    users     = json.loads(raw)
    billable  = 0
    suspended = 0
    domain_counts: Dict = defaultdict(int)
    for u in users:
        state = (u.get("state") or "").upper()
        email = (u.get("email") or "").strip()
        if state == "SUSPENDED":
            suspended += 1
            continue
        if state in OP_BILLABLE_STATES:
            billable += 1
            domain_counts[normalize_domain(email)] += 1
    counts_by_domain = {d: {"Billable seats": c} for d, c in domain_counts.items()}
    summary = {"billable": billable, "suspended": suspended, "total": len(users)}
    return counts_by_domain, summary


# =========================
# Git state commit
# =========================
def commit_state() -> None:
    gitlab_token = os.getenv("GITLAB_STATE_TOKEN")
    ci_repo_url  = os.getenv("CI_REPOSITORY_URL", "")
    ci_user      = os.getenv("GITLAB_USER_LOGIN", "pipeline")

    if not gitlab_token:
        print("WARN: GITLAB_STATE_TOKEN niet ingesteld, state wordt niet gecommit.")
        return

    # Vervang credentials in remote URL
    if "@" in ci_repo_url:
        repo_url = ci_repo_url.split("@", 1)[1]
    else:
        repo_url = ci_repo_url.replace("https://", "")

    remote = f"https://pipeline:{gitlab_token}@{repo_url}"

    cmds = [
        ["git", "config", "user.email", "pipeline@dynapps.be"],
        ["git", "config", "user.name", "GitLab Pipeline"],
        ["git", "add", "state/"],
        ["git", "diff", "--cached", "--quiet"],  # Check if there are changes
    ]

    import subprocess as sp
    for cmd in cmds:
        sp.run(cmd, check=False)

    # Check of er wijzigingen zijn
    result = sp.run(["git", "diff", "--cached", "--quiet"])
    if result.returncode == 0:
        print("State ongewijzigd, geen commit nodig.")
        return

    sp.run(["git", "commit", "-m", "chore: update ops digest state [skip ci]"], check=True)
    sp.run(["git", "push", remote, "HEAD:master"], check=True)
    print("State succesvol gecommit naar repo.")


# =========================
# Main
# =========================
if __name__ == "__main__":
    now   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f":bar_chart: *Ops Digest* — {now}"

    # M365
    token   = get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    cur_m365  = build_m365_counts(headers)
    prev_m365 = load_snapshot(M365_SNAPSHOT_PATH)
    m365_changes = diff_counts(prev_m365, cur_m365, friendly_map=True)

    # 1Password
    cur_op, op_summary = build_1password_counts()
    prev_op    = load_snapshot(OP_SNAPSHOT_PATH)
    op_changes = diff_counts(prev_op, cur_op, friendly_map=False)

    # Wijzigingen
    any_changes = (
        (prev_m365 is not None and bool(m365_changes)) or
        (prev_op   is not None and bool(op_changes))
    )

    m365_overview = format_overview("M365 licenties per domein (betalend)", cur_m365, friendly_map=True, exclude_domains=OVERVIEW_EXCLUDE_DOMAINS)
    op_overview   = format_overview(
        f"1Password seats — billable: {op_summary['billable']} | suspended: {op_summary['suspended']} | totaal: {op_summary['total']}",
        cur_op, friendly_map=False, exclude_domains=OVERVIEW_EXCLUDE_DOMAINS,
    )

    change_lines: List[str] = []
    if prev_m365 is None:
        change_lines.append("_M365: eerste run (geen vergelijking)._")
    elif m365_changes:
        change_lines.append("*M365 wijzigingen:*")
        change_lines.extend(m365_changes)
    else:
        change_lines.append("_M365: geen wijzigingen._")

    change_lines.append("")

    if prev_op is None:
        change_lines.append("_1Password: eerste run (geen vergelijking)._")
    elif op_changes:
        change_lines.append("*1Password wijzigingen:*")
        change_lines.extend(op_changes)
    else:
        change_lines.append("_1Password: geen wijzigingen._")

    prefix = f"{SLACK_MENTIONS}\n" if (any_changes and SLACK_MENTIONS) else ""
    msg    = f"{prefix}{title}\n\n*Wijzigingen t.o.v. vorige run:*\n{chr(10).join(change_lines)}\n\n{m365_overview}\n\n{op_overview}"

    send_to_slack(msg)

    save_snapshot(M365_SNAPSHOT_PATH, cur_m365)
    save_snapshot(OP_SNAPSHOT_PATH, cur_op)

    # State committen naar Git repo
    commit_state()

    print("Done.")