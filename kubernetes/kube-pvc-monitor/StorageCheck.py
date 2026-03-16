from kubernetes import client, config
from prompt_toolkit import prompt
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.validation import Validator
import subprocess

# ── config ────────────────────────────────────────────────────────────────────

WARN_PCT = 70
CRIT_PCT = 85

# De mounts die we willen tonen, met een leesbare label
TARGET_MOUNTS = {
    "/var/lib/postgresql/data": "DB PVC",
    "/opt/odoo/data":           "ODOO PVC",
}

# ── helpers ───────────────────────────────────────────────────────────────────

def bar(pct: int, width: int = 20) -> str:
    filled = int(width * pct / 100)
    color  = "\033[91m" if pct >= CRIT_PCT else "\033[93m" if pct >= WARN_PCT else "\033[92m"
    reset  = "\033[0m"
    return f"{color}{'█' * filled}{'░' * (width - filled)}{reset} {pct:>3}%"

def parse_df(raw: str) -> dict[str, dict]:
    """Geeft een dict terug: mountpad -> {size, used, avail, pct, fs}"""
    result = {}
    for line in raw.strip().splitlines()[1:]:   # skip header
        parts = line.split()
        if len(parts) < 6:
            continue
        fs, size, used, avail, pct_str, mount = (
            parts[0], parts[1], parts[2], parts[3], parts[4], parts[-1]
        )
        if mount not in TARGET_MOUNTS:
            continue
        try:
            pct = int(pct_str.replace("%", ""))
        except ValueError:
            continue
        result[mount] = {"fs": fs, "size": size, "used": used,
                         "avail": avail, "pct": pct}
    return result

def status_color(pct: int) -> str:
    if pct >= CRIT_PCT: return "\033[91m"   # rood
    if pct >= WARN_PCT: return "\033[93m"   # geel
    return "\033[92m"                        # groen

def print_pvc_overview(data: dict[str, dict], pod: str, ns: str) -> None:
    reset = "\033[0m"
    bold  = "\033[1m"
    print(f"\n{bold}PVC Storage overzicht{reset}")
    print(f"  Pod:       {pod}")
    print(f"  Namespace: {ns}\n")

    for mount, label in TARGET_MOUNTS.items():
        print(f"{bold}=== {label} ==={reset}")
        if mount not in data:
            print(f"  \033[90m(niet gevonden in df-output){reset}\n")
            continue
        r   = data[mount]
        col = status_color(r["pct"])
        print(f"  Mount:  {mount}")
        print(f"  Size:   {r['size']}")
        print(f"  Used:   {col}{r['used']}{reset}")
        print(f"  Avail:  {col}{r['avail']}{reset}")
        print(f"  Usage:  {bar(r['pct'])}")
        print()

    # samenvatting met waarschuwingen
    warnings = [(label, data[m]) for m, label in TARGET_MOUNTS.items()
                if m in data and data[m]["pct"] >= WARN_PCT]
    if warnings:
        print("─" * 45)
        for label, r in warnings:
            col = "\033[91m" if r["pct"] >= CRIT_PCT else "\033[93m"
            icon = "⛔" if r["pct"] >= CRIT_PCT else "⚠️ "
            print(f"{col}{icon}  {label}: {r['pct']}% vol – nog {r['avail']} beschikbaar{reset}")
        print()
    else:
        print("\033[92m✓ Alle PVCs OK\033[0m\n")

# ── context ───────────────────────────────────────────────────────────────────

contexts, active_context = config.list_kube_config_contexts()
if not contexts:
    print("Geen kube-config contexten gevonden.")
    exit(1)

ctx_names = [c["name"] for c in contexts]
completer = WordCompleter(ctx_names)
validator = Validator.from_callable(lambda c: c in ctx_names,
                                    error_message="Ongeldige context")

selected_context = prompt(
    message=f"Selecteer context ({', '.join(ctx_names)}): ",
    completer=completer, validator=validator,
    default=active_context["name"], complete_while_typing=True,
)
config.load_kube_config(context=selected_context)

# ── namespace ─────────────────────────────────────────────────────────────────

v1      = client.CoreV1Api()
ns_list = [ns.metadata.name for ns in v1.list_namespace().items]
completer = WordCompleter(ns_list)
validator = Validator.from_callable(lambda n: n in ns_list,
                                    error_message="Namespace niet gevonden")

print("\nBeschikbare namespaces:")
for ns in ns_list:
    print(f"  - {ns}")

selected_ns = prompt(
    message="Selecteer namespace: ",
    completer=completer, validator=validator, complete_while_typing=True,
)

# ── pods ──────────────────────────────────────────────────────────────────────

pods     = v1.list_namespaced_pod(namespace=selected_ns).items
pod_list = [p.metadata.name for p in pods]
if not pod_list:
    print(f"Geen pods in namespace '{selected_ns}'.")
    exit(1)

completer = WordCompleter(pod_list)
validator = Validator.from_callable(lambda p: p in pod_list,
                                    error_message="Pod niet gevonden")

print("\nBeschikbare pods:")
for p in pod_list:
    print(f"  - {p}")

selected_pod = prompt(
    message="Selecteer pod: ",
    completer=completer, validator=validator, complete_while_typing=True,
)

# ── controleer of pod nog bestaat (kan herstart zijn tijdens selectie) ────────

try:
    v1.read_namespaced_pod(name=selected_pod, namespace=selected_ns)
except client.exceptions.ApiException as e:
    if e.status == 404:
        print(f"\n\033[91m⛔ Pod '{selected_pod}' bestaat niet meer (herstart tijdens selectie?).\033[0m")
        print("Herstart het script om de nieuwe pod te selecteren.")
        exit(1)
    raise

# ── df uitvoeren ──────────────────────────────────────────────────────────────

try:
    result = subprocess.run(
        [
            "kubectl",
            "--context", selected_context,   # zelfde context als Python client
            "-n", selected_ns,
            "exec", selected_pod,
            "--", "df", "-h",
        ],
        capture_output=True, text=True,
    )
    data = parse_df(result.stdout)
    print_pvc_overview(data, selected_pod, selected_ns)

    # echte kubectl-fouten tonen (niet de "Defaulted container" melding)
    real_errors = [l for l in result.stderr.splitlines()
                   if not l.startswith("Defaulted container")]
    if real_errors:
        print("kubectl errors:\n" + "\n".join(real_errors))

except Exception as e:
    print(f"Fout bij uitvoeren commando: {e}")