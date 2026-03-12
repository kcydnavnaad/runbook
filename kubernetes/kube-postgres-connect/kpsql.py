#!/usr/bin/env python3
import subprocess
import sys
from typing import List, Optional

from prompt_toolkit import prompt
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.validation import Validator


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: List[str], check: bool = True, capture: bool = True):
    return subprocess.run(
        cmd,
        text=True,
        capture_output=capture,
        check=check,
    )


def choose_with_autocomplete(message: str, items: List[str], default: Optional[str] = None) -> str:
    if not items:
        die(f"Geen opties beschikbaar voor: {message}")

    completer = WordCompleter(items, ignore_case=True, sentence=True)
    validator = Validator.from_callable(
        lambda x: x in items,
        error_message="Ongeldige keuze",
        move_cursor_to_end=True,
    )

    return prompt(
        message=message,
        completer=completer,
        validator=validator,
        default=default or "",
        complete_while_typing=True,
    ).strip()


def choose_manual_input(message: str, default: Optional[str] = None) -> str:
    return prompt(
        message=message,
        default=default or "",
        complete_while_typing=True,
    ).strip()


def get_current_context() -> str:
    result = run(["kubectl", "config", "current-context"])
    return result.stdout.strip()


def get_contexts() -> List[str]:
    result = run(["kubectl", "config", "get-contexts", "-o", "name"])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def use_context(ctx: str) -> None:
    run(["kubectl", "config", "use-context", ctx], capture=True)


def get_current_namespace_from_context() -> str:
    result = run(
        ["kubectl", "config", "view", "--minify", "--output", "jsonpath={..namespace}"],
        check=False,
    )
    return result.stdout.strip()


def get_namespaces() -> List[str]:
    result = run(
        ["kubectl", "get", "ns", "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}"],
        check=False,
    )
    if result.returncode != 0:
        return []
    return sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])


def get_pods(namespace: str) -> List[str]:
    result = run(
        ["kubectl", "-n", namespace, "get", "pods", "-o", "name"],
        check=False,
    )
    if result.returncode != 0:
        return []

    pods = [line.strip().replace("pod/", "") for line in result.stdout.splitlines() if line.strip()]
    preferred = [p for p in pods if ("postgres" in p.lower() or "db" in p.lower())]
    return preferred if preferred else pods


def get_databases(namespace: str, pod: str, pguser: str) -> List[str]:
    result = run(
        [
            "kubectl", "-n", namespace, "exec", "-i", pod, "--",
            "psql", "-U", pguser, "-t", "-A",
            "-c", "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1;"
        ],
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def main() -> None:
    print(f"🔎 Current context: {get_current_context()}")

    contexts = get_contexts()
    if not contexts:
        die("No contexts found in kubeconfig.")

    print("\n🌐 Available kubectl contexts:")
    for c in contexts:
        print(f"  - {c}")

    current_ctx = get_current_context()
    chosen_ctx = choose_with_autocomplete(
        "Selecteer context: ",
        contexts,
        default=current_ctx,
    )

    use_context(chosen_ctx)
    print(f"✅ Using context: {get_current_context()}")

    print("\n📦 Fetching namespaces...")
    namespaces = get_namespaces()

    if namespaces:
        print("Beschikbare namespaces:")
        for ns in namespaces:
            print(f"  - {ns}")

        default_ns = get_current_namespace_from_context()
        chosen_ns = choose_with_autocomplete(
            "Selecteer namespace: ",
            namespaces,
            default=default_ns if default_ns in namespaces else None,
        )
    else:
        default_ns = get_current_namespace_from_context()
        print("⚠️ Kon namespaces niet automatisch ophalen. Mogelijk RBAC/SSO issue.")
        if default_ns:
            chosen_ns = choose_manual_input(
                "Type namespace handmatig: ",
                default=default_ns,
            )
        else:
            chosen_ns = choose_manual_input("Type namespace handmatig: ")

        if not chosen_ns:
            die("No namespace provided.")

    print(f"✅ Namespace: {chosen_ns}")

    print(f"\n🐘 Searching for Postgres pods in namespace '{chosen_ns}'...")
    pods = get_pods(chosen_ns)
    if not pods:
        die(f"No pods found in namespace '{chosen_ns}', of geen toegang.")

    print("Beschikbare pods:")
    for p in pods:
        print(f"  - {p}")

    chosen_pod = choose_with_autocomplete("Selecteer pod: ", pods)
    print(f"✅ Pod: {chosen_pod}")

    pguser = prompt("👤 Postgres user [postgres]: ").strip() or "postgres"

    print("\n📚 Listing databases...")
    dbs = get_databases(chosen_ns, chosen_pod, pguser)

    if dbs:
        print("Beschikbare databases:")
        for db in dbs:
            print(f"  - {db}")

        dbname = choose_with_autocomplete("Selecteer database: ", dbs)
    else:
        print("⚠️ Could not list databases automatically.")
        dbname = choose_manual_input("Type database name manually: ")
        if not dbname:
            die("No database name provided.")

    print()
    print(
        f"🚀 Connecting: context={get_current_context()} "
        f"ns={chosen_ns} pod={chosen_pod} user={pguser} db={dbname}"
    )
    print("Tip: exit with \\q")

    subprocess.run(
        ["kubectl", "-n", chosen_ns, "exec", "-it", chosen_pod, "--", "psql", "-U", pguser, "-d", dbname]
    )


if __name__ == "__main__":
    main()