from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from ccproxy import __version__
from ccproxy.checks import next_provider_candidates, run_check
from ccproxy.config import (
    APP_CHOICES,
    current_provider,
    health_state_path,
    current_provider_id,
    init_config,
    load_config,
    log_path,
    normalize_app,
    proxy_runtime_status,
    save_config,
    set_current_provider,
    tail_file,
    upsert_provider,
)
from ccproxy.health_store import ensure_provider_entry, format_timestamp, load_health_state, provider_in_cooldown
from ccproxy.importers import import_from_cc_switch
from ccproxy.launch import (
    launch_claude,
    launch_codex,
    start_proxy_background,
    stop_proxy_background,
)
from ccproxy.proxy import run_proxy
from ccproxy.service import build_unit, current_username, install_service, resolve_ccproxy_executable, uninstall_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccproxy",
        description="Hot-switch local proxy and launcher for Codex and Claude CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create config skeleton if missing.")

    import_parser = sub.add_parser(
        "import-cc-switch", help="Import codex/claude providers from ~/.cc-switch/cc-switch.db."
    )
    import_parser.add_argument(
        "--db-path",
        default=str(Path.home() / ".cc-switch" / "cc-switch.db"),
        help="Path to cc-switch.db.",
    )

    add_parser = sub.add_parser("add", help="Add a provider manually.")
    add_parser.add_argument("app", choices=APP_CHOICES)
    add_parser.add_argument("id", help="Provider ID used inside ccproxy.")
    add_parser.add_argument("--name", help="Human-readable provider name.")
    add_parser.add_argument("--base-url", required=True, help="Upstream base URL.")
    add_parser.add_argument("--api-key", required=True, help="Upstream API key.")
    add_parser.add_argument("--model", help="Default model for Codex launcher.")
    add_parser.add_argument(
        "--auth-mode",
        choices=("bearer", "x-api-key", "both"),
        help="Auth mode for Claude upstreams. Defaults to bearer for codex and claude.",
    )
    add_parser.add_argument(
        "--set-current",
        action="store_true",
        help="Set this provider as current immediately.",
    )

    list_parser = sub.add_parser("list", help="List providers for an app.")
    list_parser.add_argument("app", nargs="?", default="codex", choices=APP_CHOICES)

    current_parser = sub.add_parser("current", help="Show current provider.")
    current_parser.add_argument("app", nargs="?", default="codex", choices=APP_CHOICES)

    check_parser = sub.add_parser("check", help="Run a real non-interactive health check against a provider.")
    check_parser.add_argument("app", choices=APP_CHOICES)
    check_parser.add_argument("provider", nargs="?", help="Provider ID first, then exact provider name. Defaults to current provider.")

    next_parser = sub.add_parser(
        "next",
        help="Rotate to the next healthy provider. Failed candidates are skipped automatically.",
    )
    next_parser.add_argument("app", choices=APP_CHOICES)

    health_parser = sub.add_parser(
        "health",
        help="Show runtime provider health and cooldown state recorded by the proxy.",
    )
    health_parser.add_argument("app", nargs="?", choices=APP_CHOICES)

    service_parser = sub.add_parser("service", help="Install or print a systemd service for boot-time startup.")
    service_sub = service_parser.add_subparsers(dest="service_command", required=True)

    service_install = service_sub.add_parser("install", help="Install a systemd unit.")
    service_install.add_argument("--scope", choices=("user", "system"), default="user")
    service_install.add_argument("--enable-now", action="store_true")
    service_install.add_argument(
        "--user",
        default=current_username(),
        help="Target user for system scope units. Defaults to current user.",
    )

    service_print = service_sub.add_parser("print", help="Print a systemd unit to stdout.")
    service_print.add_argument("--scope", choices=("user", "system"), default="user")
    service_print.add_argument(
        "--user",
        default=current_username(),
        help="Target user for system scope units. Defaults to current user.",
    )

    service_uninstall = service_sub.add_parser("uninstall", help="Remove an installed systemd unit.")
    service_uninstall.add_argument("--scope", choices=("user", "system"), default="user")
    service_uninstall.add_argument("--disable-now", action="store_true")

    use_parser = sub.add_parser("use", help="Switch current provider.")
    use_parser.add_argument("app", choices=APP_CHOICES)
    use_parser.add_argument("selector", help="Provider ID first, then exact provider name.")

    proxy_parser = sub.add_parser("proxy", help="Manage the local proxy.")
    proxy_sub = proxy_parser.add_subparsers(dest="proxy_command", required=True)

    proxy_up = proxy_sub.add_parser("up", help="Start proxy in background.")
    proxy_up.add_argument("--host", help="Override listen host.")
    proxy_up.add_argument("--port", type=int, help="Override listen port.")

    proxy_run = proxy_sub.add_parser("run", help="Run proxy in foreground.")
    proxy_run.add_argument("--host", help="Override listen host.")
    proxy_run.add_argument("--port", type=int, help="Override listen port.")

    proxy_sub.add_parser("down", help="Stop background proxy.")
    proxy_sub.add_parser("status", help="Show proxy status.")
    proxy_sub.add_parser("logs", help="Show recent proxy logs.")

    codex_parser = sub.add_parser(
        "codex",
        help="Launch Codex with a temporary CODEX_HOME pointed at localhost proxy.",
    )
    codex_parser.add_argument(
        "--provider",
        help="Optional provider selector to switch first.",
    )
    codex_parser.add_argument("args", nargs=argparse.REMAINDER, help="Arguments forwarded to codex.")

    claude_parser = sub.add_parser(
        "claude",
        help="Launch Claude with a temporary --settings file pointed at localhost proxy.",
    )
    claude_parser.add_argument(
        "--provider",
        help="Optional provider selector to switch first.",
    )
    claude_parser.add_argument("args", nargs=argparse.REMAINDER, help="Arguments forwarded to claude.")

    internal_proxy = sub.add_parser("_proxy-run")
    internal_proxy.add_argument("--host", required=True)
    internal_proxy.add_argument("--port", required=True, type=int)

    return parser


def print_provider_list(app: str) -> None:
    data = load_config()
    app = normalize_app(app)
    providers = data["apps"][app]["providers"]
    current = data["apps"][app]["current"]
    if not providers:
        print(f"[{app}] no providers configured")
        return

    print(f"[{app}] providers")
    for provider_id, provider in providers.items():
        marker = "*" if provider_id == current else " "
        name = provider.get("name", provider_id)
        base_url = provider.get("base_url", "")
        print(f"{marker} {provider_id:24} {name:24} {base_url}")


def print_current(app: str) -> None:
    data = load_config()
    provider_id, provider = current_provider(data, app)
    print(f"{app}: {provider_id} ({provider.get('name', provider_id)}) -> {provider.get('base_url')}")


def cmd_import_cc_switch(db_path: Path) -> int:
    data = load_config()
    stats = import_from_cc_switch(data, db_path)
    save_config(data)
    print(
        "imported "
        f"codex={stats['codex_imported']} "
        f"claude={stats['claude_imported']} "
        f"skipped-codex={stats['codex_skipped']} "
        f"skipped-claude={stats['claude_skipped']}"
    )
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    data = load_config()
    provider = {
        "name": args.name or args.id,
        "base_url": args.base_url,
        "api_key": args.api_key,
        "model": args.model,
        "auth_mode": args.auth_mode or "bearer",
    }
    upsert_provider(
        data,
        args.app,
        args.id,
        provider,
        set_current=args.set_current,
    )
    save_config(data)
    print(f"saved {args.app} provider: {args.id}")
    if args.set_current:
        print(f"current {args.app}: {args.id}")
    return 0


def cmd_use(app: str, selector: str) -> int:
    data = load_config()
    provider_id, provider = set_current_provider(data, app, selector)
    save_config(data)
    status = proxy_runtime_status(data)
    print(f"current {app}: {provider_id} ({provider.get('name', provider_id)})")
    if status["running"]:
        print("proxy is running, next request will use the new provider without restarting the client")
    return 0


def cmd_check(app: str, provider: str | None) -> int:
    result = run_check(app, provider)
    status = "OK" if result.success else "FAIL"
    print(
        f"[{status}] {result.app} {result.provider_id} ({result.provider_name}) "
        f"{result.duration_sec:.1f}s"
    )
    if not result.success:
        if result.stderr.strip():
            print(result.stderr.strip())
        elif result.stdout.strip():
            print(result.stdout.strip())
        return 1
    return 0


def cmd_next(app: str) -> int:
    data = load_config()
    candidates = next_provider_candidates(data, app)
    if not candidates:
        raise ValueError(f"no providers configured for {app}")

    current = current_provider_id(data, app)
    for provider_id, provider in candidates:
        if provider_id == current and len(candidates) > 1:
            continue
        result = run_check(app, provider_id)
        status = "OK" if result.success else "FAIL"
        print(
            f"[{status}] {app} {provider_id} ({provider.get('name', provider_id)}) "
            f"{result.duration_sec:.1f}s"
        )
        if result.success:
            _, selected = set_current_provider(data, app, provider_id)
            save_config(data)
            print(f"current {app}: {provider_id} ({selected.get('name', provider_id)})")
            proxy_status = proxy_runtime_status(data)
            if proxy_status["running"]:
                print("proxy is running, next request will use the new provider without restarting the client")
            return 0

    print(f"no healthy provider found for {app}")
    return 1


def cmd_proxy_status() -> int:
    status = proxy_runtime_status(load_config())
    state = "running" if status["running"] else "stopped"
    print(f"proxy: {state}")
    print(f"listen: http://{status['host']}:{status['port']}")
    print(f"auto failover: {'enabled' if status['auto_failover'] else 'disabled'}")
    print(f"cooldown sec: {status['cooldown_sec']}")
    if status["pid"]:
        print(f"pid: {status['pid']}")
    print(f"log: {status['log_path']}")
    print(f"health: {status['health_path']}")
    return 0


def cmd_health(app: str | None) -> int:
    config = load_config()
    state = load_health_state()
    apps = [app] if app else list(APP_CHOICES)
    for app_name in apps:
        print(f"[{app_name}]")
        providers = config["apps"][app_name]["providers"]
        current = config["apps"][app_name]["current"]
        if not providers:
            print("  no providers configured")
            continue
        for provider_id, provider in providers.items():
            entry = ensure_provider_entry(
                state,
                app_name,
                provider_id,
                provider.get("name", provider_id),
            )
            marker = "*" if provider_id == current else " "
            status = "cooldown" if provider_in_cooldown(entry) else "ready"
            print(
                f"{marker} {provider_id:24} {provider.get('name', provider_id):24} "
                f"{status:8} "
                f"succ={entry['total_successes']} fail={entry['total_failures']} "
                f"cfail={entry['consecutive_failures']}"
            )
            print(
                f"  last_ok={format_timestamp(entry.get('last_success_at'))} "
                f"last_fail={format_timestamp(entry.get('last_failure_at'))} "
                f"cooldown_until={format_timestamp(entry.get('cooldown_until'))}"
            )
            if entry.get("last_error"):
                print(f"  last_error={entry['last_error']}")
    if apps:
        print(f"health state file: {health_state_path()}")
    return 0


def cmd_service_install(scope: str, enable_now: bool, username: str) -> int:
    path = install_service(scope, enable_now, username)
    print(f"installed {scope} service: {path}")
    if scope == "user" and not enable_now:
        print("enable it with: systemctl --user enable --now ccproxy.service")
    if scope == "system" and not enable_now:
        print("enable it with: sudo systemctl enable --now ccproxy.service")
    return 0


def cmd_service_print(scope: str, username: str) -> int:
    print(build_unit(scope, resolve_ccproxy_executable(), username), end="")
    return 0


def cmd_service_uninstall(scope: str, disable_now: bool) -> int:
    path = uninstall_service(scope, disable_now)
    print(f"removed {scope} service: {path}")
    return 0


def cmd_proxy_logs() -> int:
    lines = tail_file(log_path())
    if not lines:
        print("no proxy logs yet")
        return 0
    print("\n".join(lines))
    return 0


def cmd_proxy_run(host: str | None, port: int | None) -> int:
    data = load_config()
    if host:
        data["proxy"]["host"] = host
    if port:
        data["proxy"]["port"] = port
    save_config(data)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(run_proxy(data["proxy"]["host"], data["proxy"]["port"]))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            print(init_config())
            raise SystemExit(0)

        if args.command == "import-cc-switch":
            raise SystemExit(cmd_import_cc_switch(Path(args.db_path).expanduser()))

        if args.command == "add":
            raise SystemExit(cmd_add(args))

        if args.command == "list":
            print_provider_list(args.app)
            raise SystemExit(0)

        if args.command == "current":
            print_current(args.app)
            raise SystemExit(0)

        if args.command == "check":
            raise SystemExit(cmd_check(args.app, args.provider))

        if args.command == "next":
            raise SystemExit(cmd_next(args.app))

        if args.command == "health":
            raise SystemExit(cmd_health(args.app))

        if args.command == "service":
            if args.service_command == "install":
                raise SystemExit(cmd_service_install(args.scope, args.enable_now, args.user))
            if args.service_command == "print":
                raise SystemExit(cmd_service_print(args.scope, args.user))
            if args.service_command == "uninstall":
                raise SystemExit(cmd_service_uninstall(args.scope, args.disable_now))

        if args.command == "use":
            raise SystemExit(cmd_use(args.app, args.selector))

        if args.command == "proxy":
            if args.proxy_command == "up":
                status = start_proxy_background(host=args.host, port=args.port)
                print(f"proxy running on http://{status['host']}:{status['port']} (pid={status['pid']})")
                raise SystemExit(0)
            if args.proxy_command == "run":
                raise SystemExit(cmd_proxy_run(args.host, args.port))
            if args.proxy_command == "down":
                stopped = stop_proxy_background()
                print("proxy stopped" if stopped else "proxy was not running")
                raise SystemExit(0)
            if args.proxy_command == "status":
                raise SystemExit(cmd_proxy_status())
            if args.proxy_command == "logs":
                raise SystemExit(cmd_proxy_logs())

        if args.command == "codex":
            raise SystemExit(launch_codex(args.provider, args.args))

        if args.command == "claude":
            raise SystemExit(launch_claude(args.provider, args.args))

        if args.command == "_proxy-run":
            raise SystemExit(cmd_proxy_run(args.host, args.port))

        parser.error(f"unknown command: {args.command}")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
