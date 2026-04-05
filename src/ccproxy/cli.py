from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from ccproxy import __version__
from ccproxy.checks import next_provider_candidates, run_check
from ccproxy.completion import completion_provider_ids, render_completion
from ccproxy.config import (
    APP_CHOICES,
    current_provider,
    health_state_path,
    current_provider_id,
    init_config,
    load_config,
    log_path,
    normalize_app,
    proxy_config,
    proxy_max_body_bytes,
    proxy_runtime_status,
    save_config,
    set_current_provider,
    tail_file,
    update_proxy_config,
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


CLI_LANG = "en"

LANG_ALIASES = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh_cn": "zh",
    "cn": "zh",
    "en": "en",
    "en-us": "en",
}


def t(en: str, zh: str) -> str:
    return zh if CLI_LANG == "zh" else en


def canonical_lang(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None

    lower = value.lower()
    alias = LANG_ALIASES.get(lower) or LANG_ALIASES.get(value)
    if alias:
        return alias

    if lower.startswith("zh") or "hans" in lower or "hant" in lower or "chinese" in lower:
        return "zh"
    if lower.startswith("en"):
        return "en"
    return None


def detect_cli_lang(env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = env.get(key)
        normalized = canonical_lang(raw)
        if normalized:
            return normalized
        if raw:
            lower = raw.strip().lower()
            if lower in {"c", "posix"} or lower.startswith("c.") or lower.startswith("posix."):
                return "en"
            return "en"
    return "en"


def build_parser(lang: str = "en") -> argparse.ArgumentParser:
    global CLI_LANG
    CLI_LANG = lang
    parser = argparse.ArgumentParser(
        prog="ccproxy",
        description=t(
            "Hot-switch local proxy and launcher for Codex and Claude CLI.",
            "给 Codex 和 Claude CLI 用的本地热切代理与启动器。",
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help=t("Create config skeleton if missing.", "初始化配置骨架。"))

    import_parser = sub.add_parser(
        "import-cc-switch",
        help=t("Import codex/claude providers from ~/.cc-switch/cc-switch.db.", "从 ~/.cc-switch/cc-switch.db 导入 codex/claude provider。"),
    )
    import_parser.add_argument(
        "--db-path",
        default=str(Path.home() / ".cc-switch" / "cc-switch.db"),
        help=t("Path to cc-switch.db.", "cc-switch.db 路径。"),
    )

    add_parser = sub.add_parser("add", help=t("Add a provider manually.", "手动添加 provider。"))
    add_parser.add_argument("app", choices=APP_CHOICES)
    add_parser.add_argument("id", help=t("Provider ID used inside ccproxy.", "ccproxy 内部使用的 provider ID。"))
    add_parser.add_argument("--name", help=t("Human-readable provider name.", "便于识别的 provider 名称。"))
    add_parser.add_argument("--base-url", required=True, help=t("Upstream base URL.", "上游 base URL。"))
    add_parser.add_argument("--api-key", required=True, help=t("Upstream API key.", "上游 API key。"))
    add_parser.add_argument("--model", help=t("Default model for Codex launcher.", "Codex 启动时默认模型。"))
    add_parser.add_argument(
        "--auth-mode",
        choices=("bearer", "x-api-key", "both"),
        help=t("Auth mode for Claude upstreams. Defaults to bearer for codex and claude.", "Claude 上游的鉴权模式；codex/claude 默认都是 bearer。"),
    )
    add_parser.add_argument(
        "--set-current",
        action="store_true",
        help=t("Set this provider as current immediately.", "添加后立刻设为当前 provider。"),
    )

    list_parser = sub.add_parser("list", help=t("List providers for an app.", "列出某个 app 的 provider。"))
    list_parser.add_argument("app", nargs="?", default="codex", choices=APP_CHOICES)

    current_parser = sub.add_parser("current", help=t("Show current provider.", "显示当前 provider。"))
    current_parser.add_argument("app", nargs="?", default="codex", choices=APP_CHOICES)

    check_parser = sub.add_parser("check", help=t("Run a real non-interactive health check against a provider.", "对 provider 跑一次真实的非交互健康检查。"))
    check_parser.add_argument("app", choices=APP_CHOICES)
    check_parser.add_argument("provider", nargs="?", help=t("Provider ID first, then exact provider name. Defaults to current provider.", "优先传 provider ID，其次精确名称；默认检查当前 provider。"))

    next_parser = sub.add_parser(
        "next",
        help=t("Rotate to the next healthy provider. Failed candidates are skipped automatically.", "切到下一个健康 provider，失败候选会自动跳过。"),
    )
    next_parser.add_argument("app", choices=APP_CHOICES)

    health_parser = sub.add_parser(
        "health",
        help=t("Show runtime provider health and cooldown state recorded by the proxy.", "显示代理记录的运行期健康状态和冷却状态。"),
    )
    health_parser.add_argument("app", nargs="?", choices=APP_CHOICES)
    health_parser.add_argument("--json", action="store_true", help=t("Print health state as JSON.", "以 JSON 输出健康状态。"))

    service_parser = sub.add_parser("service", help=t("Install or print a systemd service for boot-time startup.", "安装或打印开机自启用的 systemd 服务。"))
    service_sub = service_parser.add_subparsers(dest="service_command", required=True)

    service_install = service_sub.add_parser("install", help=t("Install a systemd unit.", "安装 systemd unit。"))
    service_install.add_argument("--scope", choices=("user", "system"), default="user")
    service_install.add_argument("--enable-now", action="store_true")
    service_install.add_argument(
        "--user",
        default=current_username(),
        help=t("Target user for system scope units. Defaults to current user.", "system 级 unit 的目标用户，默认当前用户。"),
    )

    service_print = service_sub.add_parser("print", help=t("Print a systemd unit to stdout.", "把 systemd unit 打印到标准输出。"))
    service_print.add_argument("--scope", choices=("user", "system"), default="user")
    service_print.add_argument(
        "--user",
        default=current_username(),
        help=t("Target user for system scope units. Defaults to current user.", "system 级 unit 的目标用户，默认当前用户。"),
    )

    service_uninstall = service_sub.add_parser("uninstall", help=t("Remove an installed systemd unit.", "移除已安装的 systemd unit。"))
    service_uninstall.add_argument("--scope", choices=("user", "system"), default="user")
    service_uninstall.add_argument("--disable-now", action="store_true")

    use_parser = sub.add_parser("use", help=t("Switch current provider.", "切换当前 provider。"))
    use_parser.add_argument("app", choices=APP_CHOICES)
    use_parser.add_argument("selector", help=t("Provider ID first, then exact provider name.", "优先传 provider ID，其次精确名称。"))

    proxy_parser = sub.add_parser("proxy", help=t("Manage the local proxy.", "管理本地代理。"))
    proxy_sub = proxy_parser.add_subparsers(dest="proxy_command", required=True)

    proxy_up = proxy_sub.add_parser("up", help=t("Start proxy in background.", "后台启动代理。"))
    proxy_up.add_argument("--host", help=t("Override listen host.", "覆盖监听 host。"))
    proxy_up.add_argument("--port", type=int, help=t("Override listen port.", "覆盖监听端口。"))

    proxy_run = proxy_sub.add_parser("run", help=t("Run proxy in foreground.", "以前台方式运行代理。"))
    proxy_run.add_argument("--host", help=t("Override listen host.", "覆盖监听 host。"))
    proxy_run.add_argument("--port", type=int, help=t("Override listen port.", "覆盖监听端口。"))

    proxy_config_parser = proxy_sub.add_parser("config", help=t("Show or update persistent proxy settings.", "显示或更新持久化代理配置。"))
    proxy_config_sub = proxy_config_parser.add_subparsers(dest="proxy_config_command", required=True)
    proxy_config_sub.add_parser("show", help=t("Show persistent proxy settings.", "显示持久化代理配置。"))
    proxy_config_set = proxy_config_sub.add_parser("set", help=t("Update persistent proxy settings.", "更新持久化代理配置。"))
    proxy_config_set.add_argument("--host", help=t("Persist a new listen host.", "持久化新的监听 host。"))
    proxy_config_set.add_argument("--port", type=int, help=t("Persist a new listen port.", "持久化新的监听端口。"))
    proxy_config_set.add_argument(
        "--auto-failover",
        choices=("on", "off"),
        help=t("Enable or disable automatic failover.", "开启或关闭自动故障转移。"),
    )
    proxy_config_set.add_argument(
        "--cooldown-sec",
        type=int,
        help=t("Cooldown applied to a failed provider before it is tried again.", "失败 provider 在再次尝试前的冷却秒数。"),
    )
    proxy_config_set.add_argument(
        "--max-body-mb",
        type=int,
        help=t("Maximum accepted request body size in MiB.", "允许的最大请求体大小，单位 MiB。"),
    )

    proxy_sub.add_parser("down", help=t("Stop background proxy.", "停止后台代理。"))
    proxy_sub.add_parser("status", help=t("Show proxy status.", "显示代理状态。"))
    proxy_sub.add_parser("logs", help=t("Show recent proxy logs.", "显示最近的代理日志。"))

    codex_parser = sub.add_parser(
        "codex",
        help=t("Launch Codex with a temporary CODEX_HOME pointed at localhost proxy.", "用指向 localhost 代理的临时 CODEX_HOME 启动 Codex。"),
    )
    codex_parser.add_argument(
        "--provider",
        help=t("Optional provider selector to switch first.", "可选：先切 provider 再启动。"),
    )
    codex_parser.add_argument("args", nargs=argparse.REMAINDER, help=t("Arguments forwarded to codex.", "透传给 codex 的参数。"))

    claude_parser = sub.add_parser(
        "claude",
        help=t("Launch Claude with a temporary --settings file pointed at localhost proxy.", "用指向 localhost 代理的临时 --settings 启动 Claude。"),
    )
    claude_parser.add_argument(
        "--provider",
        help=t("Optional provider selector to switch first.", "可选：先切 provider 再启动。"),
    )
    claude_parser.add_argument("args", nargs=argparse.REMAINDER, help=t("Arguments forwarded to claude.", "透传给 claude 的参数。"))

    completion_parser = sub.add_parser(
        "completion",
        help=t("Print shell completion script.", "输出 shell 自动补全脚本。"),
    )
    completion_parser.add_argument("shell", choices=("bash", "zsh", "fish"))

    complete_providers = sub.add_parser("_complete-providers", help=argparse.SUPPRESS)
    complete_providers.add_argument("app", choices=APP_CHOICES)

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
        print(t(f"[{app}] no providers configured", f"[{app}] 没有配置 provider"))
        return

    print(t(f"[{app}] providers", f"[{app}] provider 列表"))
    for provider_id, provider in providers.items():
        marker = "*" if provider_id == current else " "
        name = provider.get("name", provider_id)
        base_url = provider.get("base_url", "")
        print(f"{marker} {provider_id:24} {name:24} {base_url}")


def print_current(app: str) -> None:
    data = load_config()
    provider_id, provider = current_provider(data, app)
    print(
        t(
            f"{app}: {provider_id} ({provider.get('name', provider_id)}) -> {provider.get('base_url')}",
            f"{app}: {provider_id} ({provider.get('name', provider_id)}) -> {provider.get('base_url')}",
        )
    )


def cmd_import_cc_switch(db_path: Path) -> int:
    data = load_config()
    stats = import_from_cc_switch(data, db_path)
    save_config(data)
    print(
        t(
            "imported "
            f"codex={stats['codex_imported']} "
            f"claude={stats['claude_imported']} "
            f"skipped-codex={stats['codex_skipped']} "
            f"skipped-claude={stats['claude_skipped']}",
            "已导入 "
            f"codex={stats['codex_imported']} "
            f"claude={stats['claude_imported']} "
            f"跳过-codex={stats['codex_skipped']} "
            f"跳过-claude={stats['claude_skipped']}",
        )
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
    print(t(f"saved {args.app} provider: {args.id}", f"已保存 {args.app} provider: {args.id}"))
    if args.set_current:
        print(t(f"current {args.app}: {args.id}", f"当前 {args.app}: {args.id}"))
    return 0


def cmd_completion(shell: str) -> int:
    print(render_completion(shell), end="")
    return 0


def cmd_complete_providers(app: str) -> int:
    for provider_id in completion_provider_ids(app):
        print(provider_id)
    return 0


def cmd_use(app: str, selector: str) -> int:
    data = load_config()
    provider_id, provider = set_current_provider(data, app, selector)
    save_config(data)
    status = proxy_runtime_status(data)
    print(t(f"current {app}: {provider_id} ({provider.get('name', provider_id)})", f"当前 {app}: {provider_id} ({provider.get('name', provider_id)})"))
    if status["running"]:
        print(
            t(
                "proxy is running, next request will use the new provider without restarting the client",
                "代理正在运行，下一次请求会直接使用新的 provider，不需要重启前台客户端",
            )
        )
    return 0


def cmd_check(app: str, provider: str | None) -> int:
    result = run_check(app, provider)
    status = t("OK", "成功") if result.success else t("FAIL", "失败")
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
        status = t("OK", "成功") if result.success else t("FAIL", "失败")
        print(
            f"[{status}] {app} {provider_id} ({provider.get('name', provider_id)}) "
            f"{result.duration_sec:.1f}s"
        )
        if result.success:
            _, selected = set_current_provider(data, app, provider_id)
            save_config(data)
            print(t(f"current {app}: {provider_id} ({selected.get('name', provider_id)})", f"当前 {app}: {provider_id} ({selected.get('name', provider_id)})"))
            proxy_status = proxy_runtime_status(data)
            if proxy_status["running"]:
                print(
                    t(
                        "proxy is running, next request will use the new provider without restarting the client",
                        "代理正在运行，下一次请求会直接使用新的 provider，不需要重启前台客户端",
                    )
                )
            return 0

    print(t(f"no healthy provider found for {app}", f"{app} 没有找到健康 provider"))
    return 1


def cmd_proxy_status() -> int:
    status = proxy_runtime_status(load_config())
    state = t("running", "运行中") if status["running"] else t("stopped", "已停止")
    print(t(f"proxy: {state}", f"代理: {state}"))
    print(t(f"listen: http://{status['host']}:{status['port']}", f"监听地址: http://{status['host']}:{status['port']}"))
    print(
        t(
            f"auto failover: {'enabled' if status['auto_failover'] else 'disabled'}",
            f"自动故障转移: {'开启' if status['auto_failover'] else '关闭'}",
        )
    )
    print(t(f"cooldown sec: {status['cooldown_sec']}", f"冷却秒数: {status['cooldown_sec']}"))
    print(t(f"max body mb: {status['max_body_mb']}", f"请求体上限 MiB: {status['max_body_mb']}"))
    if status["pid"]:
        print(f"pid: {status['pid']}")
    if status["manager"]:
        print(t(f"manager: {status['manager']}", f"托管方式: {status['manager']}"))
    print(t(f"healthy: {'yes' if status['healthy'] else 'no'}", f"健康探针: {'正常' if status['healthy'] else '异常'}"))
    print(t(f"log: {status['log_path']}", f"日志: {status['log_path']}"))
    print(t(f"health: {status['health_path']}", f"健康状态文件: {status['health_path']}"))
    return 0


def build_health_snapshot(app: str | None = None) -> dict[str, object]:
    config = load_config()
    state = load_health_state()
    apps = [app] if app else list(APP_CHOICES)
    result: dict[str, object] = {"apps": {}, "health_state_file": str(health_state_path())}

    for app_name in apps:
        providers = config["apps"][app_name]["providers"]
        current = config["apps"][app_name]["current"]
        rows: list[dict[str, object]] = []
        if not providers:
            result["apps"][app_name] = []
            continue
        for provider_id, provider in providers.items():
            entry = ensure_provider_entry(
                state,
                app_name,
                provider_id,
                provider.get("name", provider_id),
            )
            rows.append(
                {
                    "provider_id": provider_id,
                    "provider_name": provider.get("name", provider_id),
                    "current": provider_id == current,
                    "status": "cooldown" if provider_in_cooldown(entry) else "ready",
                    "total_successes": entry["total_successes"],
                    "total_failures": entry["total_failures"],
                    "consecutive_failures": entry["consecutive_failures"],
                    "last_success_at": format_timestamp(entry.get("last_success_at")),
                    "last_failure_at": format_timestamp(entry.get("last_failure_at")),
                    "cooldown_until": format_timestamp(entry.get("cooldown_until")),
                    "last_error": entry.get("last_error"),
                }
            )
        result["apps"][app_name] = rows
    return result


def cmd_health(app: str | None, json_mode: bool) -> int:
    snapshot = build_health_snapshot(app)
    if json_mode:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0

    for app_name, rows in snapshot["apps"].items():
        print(f"[{app_name}]")
        if not rows:
            print(t("  no providers configured", "  没有配置 provider"))
            continue
        for row in rows:
            marker = "*" if row["current"] else " "
            print(
                f"{marker} {row['provider_id']:24} {row['provider_name']:24} "
                f"{row['status']:8} "
                f"succ={row['total_successes']} fail={row['total_failures']} "
                f"cfail={row['consecutive_failures']}"
            )
            print(
                f"  last_ok={row['last_success_at']} "
                f"last_fail={row['last_failure_at']} "
                f"cooldown_until={row['cooldown_until']}"
            )
            if row["last_error"]:
                print(f"  last_error={row['last_error']}")
    print(t(f"health state file: {snapshot['health_state_file']}", f"健康状态文件: {snapshot['health_state_file']}"))
    return 0


def cmd_service_install(scope: str, enable_now: bool, username: str) -> int:
    path = install_service(scope, enable_now, username)
    print(t(f"installed {scope} service: {path}", f"已安装 {scope} 服务: {path}"))
    if scope == "user" and not enable_now:
        print(t("enable it with: systemctl --user enable --now ccproxy.service", "启用命令: systemctl --user enable --now ccproxy.service"))
    if scope == "system" and not enable_now:
        print(t("enable it with: sudo systemctl enable --now ccproxy.service", "启用命令: sudo systemctl enable --now ccproxy.service"))
    return 0


def cmd_service_print(scope: str, username: str) -> int:
    print(build_unit(scope, resolve_ccproxy_executable(), username), end="")
    return 0


def cmd_service_uninstall(scope: str, disable_now: bool) -> int:
    path = uninstall_service(scope, disable_now)
    print(t(f"removed {scope} service: {path}", f"已移除 {scope} 服务: {path}"))
    return 0


def cmd_proxy_logs() -> int:
    lines = tail_file(log_path())
    if not lines:
        print(t("no proxy logs yet", "还没有代理日志"))
        return 0
    print("\n".join(lines))
    return 0


def cmd_proxy_config_show() -> int:
    data = load_config()
    print(json.dumps(proxy_config(data), indent=2, sort_keys=True))
    return 0


def cmd_proxy_config_set(
    host: str | None,
    port: int | None,
    auto_failover: str | None,
    cooldown_sec: int | None,
    max_body_mb: int | None,
) -> int:
    data = load_config()
    changed = update_proxy_config(
        data,
        host=host,
        port=port,
        auto_failover=(auto_failover == "on") if auto_failover is not None else None,
        cooldown_sec=cooldown_sec,
        max_body_mb=max_body_mb,
    )
    save_config(data)
    if not changed:
        print(t("no proxy config changes", "代理配置没有变化"))
        return 0

    print(t("updated proxy config:", "已更新代理配置:"))
    for key, value in changed.items():
        print(f"  {key} = {value}")

    runtime = proxy_runtime_status(data)
    if runtime["running"] and any(key in changed for key in ("host", "port", "max_body_mb")):
        print(
            t(
                "proxy is running; host/port/max_body_mb changes apply after restart",
                "代理正在运行；host/port/max_body_mb 变更会在重启后生效",
            )
        )
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
    logging.info(
        "starting proxy with max_body_mb=%s (%s bytes)",
        data["proxy"].get("max_body_mb", 64),
        proxy_max_body_bytes(data),
    )
    asyncio.run(run_proxy(data["proxy"]["host"], data["proxy"]["port"]))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser(detect_cli_lang())
    args = parser.parse_args(argv)
    global CLI_LANG
    CLI_LANG = detect_cli_lang()

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
            raise SystemExit(cmd_health(args.app, args.json))

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
                pid_suffix = f" (pid={status['pid']})" if status["pid"] else ""
                print(
                    t(
                        f"proxy running on http://{status['host']}:{status['port']}{pid_suffix}",
                        f"代理已运行: http://{status['host']}:{status['port']}{pid_suffix}",
                    )
                )
                raise SystemExit(0)
            if args.proxy_command == "config":
                if args.proxy_config_command == "show":
                    raise SystemExit(cmd_proxy_config_show())
                if args.proxy_config_command == "set":
                    raise SystemExit(
                        cmd_proxy_config_set(
                            args.host,
                            args.port,
                            args.auto_failover,
                            args.cooldown_sec,
                            args.max_body_mb,
                        )
                    )
            if args.proxy_command == "run":
                raise SystemExit(cmd_proxy_run(args.host, args.port))
            if args.proxy_command == "down":
                stopped = stop_proxy_background()
                print(
                    t("proxy stopped", "代理已停止")
                    if stopped
                    else t("proxy was not running", "代理并未以前台后台模式运行，可能由 systemd 托管")
                )
                raise SystemExit(0)
            if args.proxy_command == "status":
                raise SystemExit(cmd_proxy_status())
            if args.proxy_command == "logs":
                raise SystemExit(cmd_proxy_logs())

        if args.command == "codex":
            raise SystemExit(launch_codex(args.provider, args.args))

        if args.command == "claude":
            raise SystemExit(launch_claude(args.provider, args.args))

        if args.command == "completion":
            raise SystemExit(cmd_completion(args.shell))

        if args.command == "_complete-providers":
            raise SystemExit(cmd_complete_providers(args.app))

        if args.command == "_proxy-run":
            raise SystemExit(cmd_proxy_run(args.host, args.port))

        parser.error(t(f"unknown command: {args.command}", f"未知命令: {args.command}"))
    except Exception as exc:
        print(t(f"error: {exc}", f"错误: {exc}"), file=sys.stderr)
        raise SystemExit(1) from exc
