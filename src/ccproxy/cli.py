from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TextIO

from ccproxy import __version__
from ccproxy.actions import (
    add_provider_action,
    build_test_snapshot as _build_test_snapshot,
    check_detail,
    delete_provider_action,
    iter_test_rows,
    next_provider_action,
    proxy_config_set_action,
    proxy_down_action,
    proxy_up_action,
    run_check_action,
    update_provider_action,
    update_test_summary,
    use_provider_action,
)
from ccproxy.checks import DEFAULT_CHECK_TIMEOUT_SEC
from ccproxy.command_registry import all_command_specs, command_spec, hidden_command_names, visible_command_names
from ccproxy.completion import completion_provider_entries, render_completion
from ccproxy.config import (
    APP_CHOICES,
    current_provider_id,
    init_config,
    load_config,
    log_path,
    normalize_app,
    ordered_provider_items,
    provider_priority,
    proxy_config,
    proxy_max_body_bytes,
    proxy_runtime_status,
    tail_file,
)
from ccproxy.importers import import_from_cc_switch
from ccproxy.launch import launch_claude, launch_codex
from ccproxy.proxy import run_proxy
from ccproxy.read_models import (
    build_current_provider_summary,
    build_dashboard_snapshot,
    build_health_snapshot as _build_health_snapshot,
    build_provider_rows,
    format_provider_label as _format_provider_label,
)
from ccproxy.service import build_unit, current_username, install_service, resolve_ccproxy_executable, uninstall_service
from ccproxy.tui import run_tui


CLI_LANG = "en"
BARE_NON_TTY_EXIT_CODE = 2

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


def _hide_subparser(subparsers, name: str) -> None:
    subparsers._choices_actions = [
        action for action in subparsers._choices_actions if getattr(action, "dest", None) != name
    ]


def _add_init_parser(sub) -> None:
    sub.add_parser(command_spec("init").name, help=t(command_spec("init").help_en, command_spec("init").help_zh))


def _add_import_parser(sub) -> None:
    spec = command_spec("import-cc-switch")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument(
        "--db-path",
        default=str(Path.home() / ".cc-switch" / "cc-switch.db"),
        help=t("Path to cc-switch.db.", "cc-switch.db 路径。"),
    )


def _add_add_parser(sub) -> None:
    spec = command_spec("add")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", choices=APP_CHOICES)
    parser.add_argument("id", help=t("Provider ID used inside ccproxy.", "ccproxy 内部使用的 provider ID。"))
    parser.add_argument("--name", help=t("Human-readable provider name.", "便于识别的 provider 名称。"))
    parser.add_argument("--base-url", required=True, help=t("Upstream base URL.", "上游 base URL。"))
    parser.add_argument("--api-key", required=True, help=t("Upstream API key.", "上游 API key。"))
    parser.add_argument("--model", help=t("Default model for Codex launcher.", "Codex 启动时默认模型。"))
    parser.add_argument(
        "--auth-mode",
        choices=("bearer", "x-api-key", "both"),
        help=t("Auth mode for Claude upstreams. Defaults to bearer for codex and claude.", "Claude 上游的鉴权模式；codex/claude 默认都是 bearer。"),
    )
    parser.add_argument("--set-current", action="store_true", help=t("Set this provider as current immediately.", "添加后立刻设为当前 provider。"))
    parser.add_argument("--priority", type=int, help=t("Lower number means higher failover priority.", "数字越小，自动故障转移优先级越高。"))


def _add_list_parser(sub) -> None:
    spec = command_spec("list")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", nargs="?", default="codex", choices=APP_CHOICES)


def _add_current_parser(sub) -> None:
    spec = command_spec("current")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", nargs="?", default="codex", choices=APP_CHOICES)


def _add_show_parser(sub) -> None:
    spec = command_spec("show")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", choices=APP_CHOICES)
    parser.add_argument("selector", help=t("Provider ID first, then exact provider name.", "优先传 provider ID，其次精确名称。"))


def _add_update_parser(sub) -> None:
    spec = command_spec("update")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", choices=APP_CHOICES)
    parser.add_argument("selector", help=t("Provider ID first, then exact provider name.", "优先传 provider ID，其次精确名称。"))
    parser.add_argument("--name", help=t("Human-readable provider name.", "便于识别的 provider 名称。"))
    parser.add_argument("--base-url", help=t("Upstream base URL.", "上游 base URL。"))
    parser.add_argument("--api-key", help=t("Upstream API key.", "上游 API key。"))
    parser.add_argument("--model", help=t("Default model for Codex launcher.", "Codex 启动时默认模型。"))
    parser.add_argument("--auth-mode", choices=("bearer", "x-api-key", "both"), help=t("Auth mode for Claude upstreams.", "Claude 上游的鉴权模式。"))
    parser.add_argument("--priority", type=int, help=t("Lower number means higher failover priority.", "数字越小，自动故障转移优先级越高。"))
    parser.add_argument("--set-current", action="store_true", help=t("Set this provider as current immediately.", "更新后立刻设为当前 provider。"))


def _add_delete_parser(sub) -> None:
    spec = command_spec("delete")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", choices=APP_CHOICES)
    parser.add_argument("selector", help=t("Provider ID first, then exact provider name.", "优先传 provider ID，其次精确名称。"))


def _add_check_parser(sub) -> None:
    spec = command_spec("check")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", choices=APP_CHOICES)
    parser.add_argument("provider", nargs="?", help=t("Provider ID first, then exact provider name. Defaults to current provider.", "优先传 provider ID，其次精确名称；默认检查当前 provider。"))
    parser.add_argument("--timeout-sec", type=float, help=t(f"Per-provider timeout in seconds. Defaults to {DEFAULT_CHECK_TIMEOUT_SEC:g}.", f"单个 provider 检查超时秒数；默认 {DEFAULT_CHECK_TIMEOUT_SEC:g} 秒。"))


def _add_test_parser(sub) -> None:
    spec = command_spec("test")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", nargs="?", choices=APP_CHOICES)
    parser.add_argument("--json", action="store_true", help=t("Print test result as JSON.", "以 JSON 输出测试结果。"))
    parser.add_argument("--timeout-sec", type=float, help=t(f"Per-provider timeout in seconds. Defaults to {DEFAULT_CHECK_TIMEOUT_SEC:g}.", f"单个 provider 检查超时秒数；默认 {DEFAULT_CHECK_TIMEOUT_SEC:g} 秒。"))


def _add_next_parser(sub) -> None:
    spec = command_spec("next")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", choices=APP_CHOICES)


def _add_health_parser(sub) -> None:
    spec = command_spec("health")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", nargs="?", choices=APP_CHOICES)
    parser.add_argument("--json", action="store_true", help=t("Print health state as JSON.", "以 JSON 输出健康状态。"))


def _add_service_parser(sub) -> None:
    spec = command_spec("service")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    service_sub = parser.add_subparsers(dest="service_command", required=True)

    install = service_sub.add_parser("install", help=t("Install a systemd unit.", "安装 systemd unit。"))
    install.add_argument("--scope", choices=("user", "system"), default="user")
    install.add_argument("--enable-now", action="store_true")
    install.add_argument("--user", default=current_username(), help=t("Target user for system scope units. Defaults to current user.", "system 级 unit 的目标用户，默认当前用户。"))

    service_print = service_sub.add_parser("print", help=t("Print a systemd unit to stdout.", "把 systemd unit 打印到标准输出。"))
    service_print.add_argument("--scope", choices=("user", "system"), default="user")
    service_print.add_argument("--user", default=current_username(), help=t("Target user for system scope units. Defaults to current user.", "system 级 unit 的目标用户，默认当前用户。"))

    uninstall = service_sub.add_parser("uninstall", help=t("Remove an installed systemd unit.", "移除已安装的 systemd unit。"))
    uninstall.add_argument("--scope", choices=("user", "system"), default="user")
    uninstall.add_argument("--disable-now", action="store_true")


def _add_use_parser(sub) -> None:
    spec = command_spec("use")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("app", choices=APP_CHOICES)
    parser.add_argument("selector", help=t("Provider ID first, then exact provider name.", "优先传 provider ID，其次精确名称。"))


def _add_proxy_parser(sub) -> None:
    spec = command_spec("proxy")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    proxy_sub = parser.add_subparsers(dest="proxy_command", required=True)

    up = proxy_sub.add_parser("up", help=t("Start proxy in background.", "后台启动代理。"))
    up.add_argument("--host", help=t("Override listen host.", "覆盖监听 host。"))
    up.add_argument("--port", type=int, help=t("Override listen port.", "覆盖监听端口。"))

    run = proxy_sub.add_parser("run", help=t("Run proxy in foreground.", "以前台方式运行代理。"))
    run.add_argument("--host", help=t("Override listen host.", "覆盖监听 host。"))
    run.add_argument("--port", type=int, help=t("Override listen port.", "覆盖监听端口。"))

    config_parser = proxy_sub.add_parser("config", help=t("Show or update persistent proxy settings.", "显示或更新持久化代理配置。"))
    config_sub = config_parser.add_subparsers(dest="proxy_config_command", required=True)
    config_sub.add_parser("show", help=t("Show persistent proxy settings.", "显示持久化代理配置。"))
    config_set = config_sub.add_parser("set", help=t("Update persistent proxy settings.", "更新持久化代理配置。"))
    config_set.add_argument("--host", help=t("Persist a new listen host.", "持久化新的监听 host。"))
    config_set.add_argument("--port", type=int, help=t("Persist a new listen port.", "持久化新的监听端口。"))
    config_set.add_argument("--auto-failover", choices=("on", "off"), help=t("Enable or disable automatic failover.", "开启或关闭自动故障转移。"))
    config_set.add_argument("--cooldown-sec", type=int, help=t("Cooldown applied to a failed provider before it is tried again.", "失败 provider 在再次尝试前的冷却秒数。"))
    config_set.add_argument("--failure-threshold", type=int, help=t("How many failed requests before a provider enters cooldown.", "连续多少次请求失败后才进入冷却。"))
    config_set.add_argument("--retry-attempts", type=int, help=t("How many attempts to make on the same provider before failover.", "同一个 provider 在故障转移前最多重试多少次。"))
    config_set.add_argument("--max-body-mb", type=int, help=t("Maximum accepted request body size in MiB.", "允许的最大请求体大小，单位 MiB。"))

    proxy_sub.add_parser("down", help=t("Stop background proxy.", "停止后台代理。"))
    proxy_sub.add_parser("status", help=t("Show proxy status.", "显示代理状态。"))
    proxy_sub.add_parser("logs", help=t("Show recent proxy logs.", "显示最近的代理日志。"))


def _add_codex_parser(sub) -> None:
    spec = command_spec("codex")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("--provider", help=t("Optional provider selector to switch first.", "可选：先切 provider 再启动。"))
    parser.add_argument("args", nargs=argparse.REMAINDER, help=t("Arguments forwarded to codex.", "透传给 codex 的参数。"))


def _add_claude_parser(sub) -> None:
    spec = command_spec("claude")
    parser = sub.add_parser(spec.name, help=t(spec.help_en, spec.help_zh))
    parser.add_argument("--provider", help=t("Optional provider selector to switch first.", "可选：先切 provider 再启动。"))
    parser.add_argument("args", nargs=argparse.REMAINDER, help=t("Arguments forwarded to claude.", "透传给 claude 的参数。"))


def _add_completion_parser(sub) -> None:
    parser = sub.add_parser(command_spec("completion").name, help=argparse.SUPPRESS)
    parser.add_argument("shell", choices=("bash", "zsh", "fish"))


def _add_complete_providers_parser(sub) -> None:
    parser = sub.add_parser(command_spec("_complete-providers").name, help=argparse.SUPPRESS)
    parser.add_argument("app", choices=APP_CHOICES)


def _add_internal_proxy_parser(sub) -> None:
    parser = sub.add_parser(command_spec("_proxy-run").name)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)


COMMAND_BUILDERS = {
    "init": _add_init_parser,
    "import-cc-switch": _add_import_parser,
    "add": _add_add_parser,
    "list": _add_list_parser,
    "current": _add_current_parser,
    "show": _add_show_parser,
    "update": _add_update_parser,
    "delete": _add_delete_parser,
    "check": _add_check_parser,
    "test": _add_test_parser,
    "next": _add_next_parser,
    "health": _add_health_parser,
    "service": _add_service_parser,
    "use": _add_use_parser,
    "proxy": _add_proxy_parser,
    "codex": _add_codex_parser,
    "claude": _add_claude_parser,
    "completion": _add_completion_parser,
    "_complete-providers": _add_complete_providers_parser,
    "_proxy-run": _add_internal_proxy_parser,
}


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
    parser.add_argument(
        "--json",
        dest="dashboard_json",
        action="store_true",
        help=t("Print top-level dashboard snapshot as JSON.", "以 JSON 输出顶层 dashboard 摘要。"),
    )
    sub = parser.add_subparsers(
        dest="command",
        required=False,
        metavar="{" + ",".join(visible_command_names()) + "}",
    )
    for spec in all_command_specs():
        COMMAND_BUILDERS[spec.name](sub)
    for hidden_command in hidden_command_names():
        _hide_subparser(sub, hidden_command)
    return parser


def format_provider_label(provider_id: str, provider_name: str | None) -> str:
    return _format_provider_label(provider_id, provider_name)


def _check_detail(result) -> str | None:
    return check_detail(result)


run_check = run_check_action


def _build_test_row(
    provider_id: str,
    provider: dict[str, object],
    current_provider: str | None,
    result,
) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "provider_name": provider.get("name", provider_id),
        "priority": provider_priority(provider),
        "current": provider_id == current_provider,
        "success": result.success,
        "duration_sec": result.duration_sec,
        "summary": result.summary,
        "detail": _check_detail(result),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
    }


def _iter_test_rows(
    app_name: str,
    ordered: list[tuple[str, dict[str, object]]],
    current: str | None,
    timeout_sec: float | None = None,
):
    for provider_id, provider in ordered:
        result = run_check(app_name, provider_id, timeout_sec=timeout_sec)
        yield _build_test_row(provider_id, provider, current, result)


def build_test_snapshot(app: str | None = None, timeout_sec: float | None = None) -> dict[str, object]:
    data = load_config()
    apps = [normalize_app(app)] if app else list(APP_CHOICES)
    snapshot: dict[str, object] = {"apps": {}, "summary": {"ok": 0, "fail": 0, "total": 0}}
    summary = snapshot["summary"]

    for app_name in apps:
        ordered = ordered_provider_items(data, app_name)
        current = current_provider_id(data, app_name)
        rows = list(_iter_test_rows(app_name, ordered, current, timeout_sec=timeout_sec))
        snapshot["apps"][app_name] = rows
        for row in rows:
            update_test_summary(summary, row)

    return snapshot


def build_health_snapshot(app: str | None = None) -> dict[str, object]:
    return _build_health_snapshot(app)


def print_provider_list(app: str) -> None:
    rows = build_provider_rows(app)
    if not rows:
        print(t(f"[{app}] no providers configured", f"[{app}] 没有配置 provider"))
        return
    print(t(f"[{app}] providers", f"[{app}] provider 列表"))
    for row in rows:
        marker = "*" if row["current"] else " "
        print(f"{marker} p={row['priority']:<4} {row['provider_id']:24} {row['provider_name']:24} {row['base_url']}")


def print_current(app: str) -> None:
    summary = build_current_provider_summary(app)
    print(
        t(
            f"{app}: {summary['selected_label']} p={summary['selected_priority']} -> {summary['selected_base_url']}",
            f"{app}: {summary['selected_label']} p={summary['selected_priority']} -> {summary['selected_base_url']}",
        )
    )
    if summary["effective_matches_selected"]:
        print(
            t(
                f"next request: {summary['effective_label']} (same as selected)",
                f"下一次请求: {summary['effective_label']}（和手动主 provider 一致）",
            )
        )
    else:
        print(
            t(
                f"next request: {summary['effective_label']} p={summary['effective_priority']} -> {summary['effective_base_url']}",
                f"下一次请求: {summary['effective_label']} p={summary['effective_priority']} -> {summary['effective_base_url']}",
            )
        )
        print(
            t(
                f"reason: {summary['effective_reason']}",
                f"原因: {summary['effective_reason']}",
            )
        )


def cmd_show(app: str, selector: str) -> int:
    data = load_config()
    providers = data["apps"][app]["providers"]
    from ccproxy.config import current_provider_id, resolve_provider_selector

    provider_id, provider = resolve_provider_selector(providers, selector)
    print(json.dumps({"app": app, "provider_id": provider_id, "current": provider_id == current_provider_id(data, app), "provider": provider}, indent=2, sort_keys=True))
    return 0


def cmd_import_cc_switch(db_path: Path) -> int:
    data = load_config()
    stats = import_from_cc_switch(data, db_path)
    from ccproxy.config import save_config

    save_config(data)
    print(t(
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
    ))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    result = add_provider_action(
        args.app,
        args.id,
        name=args.name,
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        auth_mode=args.auth_mode,
        set_current=args.set_current,
        priority=args.priority,
    )
    print(t(f"saved {args.app} provider: {result['provider_label']}", f"已保存 {args.app} provider: {result['provider_label']}"))
    if args.set_current:
        print(t(f"current {args.app}: {result['provider_label']}", f"当前 {args.app}: {result['provider_label']}"))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    result = update_provider_action(
        args.app,
        args.selector,
        name=args.name,
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        auth_mode=args.auth_mode,
        priority=args.priority,
        set_current=args.set_current,
    )
    if not result["changed"]:
        print(t("no provider changes", "provider 配置没有变化"))
        return 0
    print(t(f"updated {args.app} provider: {result['provider_label']}", f"已更新 {args.app} provider: {result['provider_label']}"))
    for key, value in result["changed"].items():
        print(f"  {key} = {value}")
    return 0


def cmd_delete(app: str, selector: str) -> int:
    result = delete_provider_action(app, selector)
    print(t(f"deleted {app} provider: {result['provider_label']}", f"已删除 {app} provider: {result['provider_label']}"))
    if result["previous_current"] != result["new_current"]:
        if result["new_current"] is None:
            print(t(f"current {app}: none", f"当前 {app}: 无"))
        else:
            current_id, current = result["new_current_provider"]
            current_label = format_provider_label(current_id, current.get("name", current_id))
            print(t(f"current {app}: {current_label}", f"当前 {app}: {current_label}"))
    return 0


def cmd_completion(shell: str) -> int:
    print(render_completion(shell), end="")
    return 0


def cmd_complete_providers(app: str) -> int:
    for provider_id, description in completion_provider_entries(app):
        print(f"{provider_id}\t{description}")
    return 0


def cmd_use(app: str, selector: str) -> int:
    result = use_provider_action(app, selector)
    print(t(f"current {app}: {result['provider_label']}", f"当前 {app}: {result['provider_label']}"))
    if result["proxy_status"]["running"]:
        print(t("proxy is running, next request will use the new provider without restarting the client", "代理正在运行，下一次请求会直接使用新的 provider，不需要重启前台客户端"))
    return 0


def cmd_check(app: str, provider: str | None, timeout_sec: float | None = None) -> int:
    result = run_check_action(app, provider, timeout_sec=timeout_sec)
    status = t("OK", "成功") if result.success else t("FAIL", "失败")
    provider_label = format_provider_label(result.provider_id, result.provider_name)
    print(f"[{status}] {result.app} {provider_label} {result.duration_sec:.1f}s")
    if not result.success:
        if result.stderr.strip():
            print(result.stderr.strip())
        elif result.stdout.strip():
            print(result.stdout.strip())
        return 1
    return 0


def _print_test_row(row: dict[str, object]) -> None:
    marker = "*" if row["current"] else " "
    status = t("OK", "成功") if row["success"] else t("FAIL", "失败")
    print(
        f"{marker} [{status}] p={row['priority']:<4} "
        f"{row['provider_id']:24} {row['provider_name']:24} "
        f"{row['duration_sec']:.1f}s",
        flush=True,
    )
    if row["detail"]:
        print(f"  {row['detail']}", flush=True)


def cmd_test(app: str | None, json_mode: bool, timeout_sec: float | None = None) -> int:
    if json_mode:
        snapshot = build_test_snapshot(app, timeout_sec=timeout_sec)
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0 if snapshot["summary"]["fail"] == 0 else 1

    data = load_config()
    apps = [normalize_app(app)] if app else list(APP_CHOICES)
    summary = {"ok": 0, "fail": 0, "total": 0}

    for app_name in apps:
        print(f"[{app_name}]", flush=True)
        ordered = ordered_provider_items(data, app_name)
        if not ordered:
            print(t("  no providers configured", "  没有配置 provider"), flush=True)
            continue
        current = current_provider_id(data, app_name)
        for row in _iter_test_rows(app_name, ordered, current, timeout_sec=timeout_sec):
            _print_test_row(row)
            update_test_summary(summary, row)

    print(t(f"summary: ok={summary['ok']} fail={summary['fail']} total={summary['total']}", f"汇总: 成功={summary['ok']} 失败={summary['fail']} 总计={summary['total']}"), flush=True)
    return 0 if summary["fail"] == 0 else 1


def cmd_next(app: str, timeout_sec: float | None = None) -> int:
    result = next_provider_action(app, timeout_sec=timeout_sec)
    for attempt in result["attempts"]:
        status = t("OK", "成功") if attempt["result"].success else t("FAIL", "失败")
        print(f"[{status}] {app} {attempt['provider_label']} {attempt['result'].duration_sec:.1f}s")
    if result["selected_provider_id"] is None:
        print(t(f"no healthy provider found for {app}", f"{app} 没有找到健康 provider"))
        return 1
    print(t(f"current {app}: {result['selected_label']}", f"当前 {app}: {result['selected_label']}"))
    if result["proxy_status"]["running"]:
        print(t("proxy is running, next request will use the new provider without restarting the client", "代理正在运行，下一次请求会直接使用新的 provider，不需要重启前台客户端"))
    return 0


def cmd_proxy_status() -> int:
    status = proxy_runtime_status(load_config())
    state = t("running", "运行中") if status["running"] else t("stopped", "已停止")
    print(t(f"proxy: {state}", f"代理: {state}"))
    print(t(f"listen: http://{status['host']}:{status['port']}", f"监听地址: http://{status['host']}:{status['port']}"))
    print(t(f"auto failover: {'enabled' if status['auto_failover'] else 'disabled'}", f"自动故障转移: {'开启' if status['auto_failover'] else '关闭'}"))
    print(t(f"cooldown sec: {status['cooldown_sec']}", f"冷却秒数: {status['cooldown_sec']}"))
    print(t(f"failure threshold: {status['failure_threshold']}", f"失败阈值: {status['failure_threshold']}"))
    print(t(f"retry attempts: {status['retry_attempts']}", f"同 provider 重试次数: {status['retry_attempts']}"))
    print(t(f"max body mb: {status['max_body_mb']}", f"请求体上限 MiB: {status['max_body_mb']}"))
    if status["pid"]:
        print(f"pid: {status['pid']}")
    if status["manager"]:
        print(t(f"manager: {status['manager']}", f"托管方式: {status['manager']}"))
    print(t(f"healthy: {'yes' if status['healthy'] else 'no'}", f"健康探针: {'正常' if status['healthy'] else '异常'}"))
    print(t(f"log: {status['log_path']}", f"日志: {status['log_path']}"))
    print(t(f"health: {status['health_path']}", f"健康状态文件: {status['health_path']}"))
    return 0


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
        print(t("  legend: * selected   ! next request", "  图例: * 手动主 provider   ! 下一次请求"))
        for row in rows:
            selected_marker = "*" if row["current"] else " "
            effective_marker = "!" if row.get("effective") else " "
            print(f"{selected_marker}{effective_marker} p={row['priority']:<4} {row['provider_id']:24} {row['provider_name']:24} {row['status']:8} succ={row['total_successes']} fail={row['total_failures']} cfail={row['consecutive_failures']}")
            print(f"  last_ok={row['last_success_at']} last_fail={row['last_failure_at']} cooldown_until={row['cooldown_until']}")
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
    print(json.dumps(proxy_config(load_config()), indent=2, sort_keys=True))
    return 0


def cmd_proxy_config_set(host: str | None, port: int | None, auto_failover: str | None, cooldown_sec: int | None, failure_threshold: int | None, retry_attempts: int | None, max_body_mb: int | None) -> int:
    result = proxy_config_set_action(
        host=host,
        port=port,
        auto_failover=(auto_failover == "on") if auto_failover is not None else None,
        cooldown_sec=cooldown_sec,
        failure_threshold=failure_threshold,
        retry_attempts=retry_attempts,
        max_body_mb=max_body_mb,
    )
    changed = result["changed"]
    if not changed:
        print(t("no proxy config changes", "代理配置没有变化"))
        return 0
    print(t("updated proxy config:", "已更新代理配置:"))
    for key, value in changed.items():
        print(f"  {key} = {value}")
    runtime = result["runtime"]
    if runtime["running"] and any(key in changed for key in ("host", "port", "max_body_mb")):
        print(t("proxy is running; host/port/max_body_mb changes apply after restart", "代理正在运行；host/port/max_body_mb 变更会在重启后生效"))
    return 0


def cmd_proxy_run(host: str | None, port: int | None) -> int:
    data = load_config()
    if host:
        data["proxy"]["host"] = host
    if port:
        data["proxy"]["port"] = port
    from ccproxy.config import save_config

    save_config(data)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.info("starting proxy with max_body_mb=%s (%s bytes)", data["proxy"].get("max_body_mb", 64), proxy_max_body_bytes(data))
    asyncio.run(run_proxy(data["proxy"]["host"], data["proxy"]["port"]))
    return 0


def cmd_dashboard_json() -> int:
    print(json.dumps(build_dashboard_snapshot(), indent=2, sort_keys=True))
    return 0


def can_launch_default_tui(stdin, stdout, env: dict[str, str] | None = None) -> bool:
    env = env or os.environ
    term = (env.get("TERM") or "").strip().lower()
    if term in {"", "dumb"}:
        return False
    return bool(getattr(stdin, "isatty", lambda: False)() and getattr(stdout, "isatty", lambda: False)())


def classify_invocation(args: argparse.Namespace, stdin, stdout, env: dict[str, str] | None = None) -> str:
    if getattr(args, "command", None):
        return "command"
    if getattr(args, "dashboard_json", False):
        return "dashboard-json"
    if can_launch_default_tui(stdin, stdout, env=env):
        return "tui"
    return "fallback"


def cmd_bare_non_tty_fallback(stderr: TextIO | None = None) -> int:
    stream = stderr or sys.stderr
    print(
        t(
            "ccproxy: interactive TUI requires a real terminal. Use --help, --json, or an explicit subcommand.",
            "ccproxy：交互式 TUI 需要真实终端。请使用 --help、--json 或明确的子命令。",
        ),
        file=stream,
    )
    return BARE_NON_TTY_EXIT_CODE


def main(argv: list[str] | None = None) -> None:
    parser = build_parser(detect_cli_lang())
    args = parser.parse_args(argv)
    global CLI_LANG
    CLI_LANG = detect_cli_lang()

    if args.command and getattr(args, "dashboard_json", False):
        parser.error(t("top-level --json cannot be combined with subcommands", "顶层 --json 不能和子命令混用"))

    try:
        route = classify_invocation(args, sys.stdin, sys.stdout)
        if route == "dashboard-json":
            raise SystemExit(cmd_dashboard_json())
        if route == "tui":
            raise SystemExit(run_tui(lang=CLI_LANG))
        if route == "fallback":
            raise SystemExit(cmd_bare_non_tty_fallback())

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
        if args.command == "show":
            raise SystemExit(cmd_show(args.app, args.selector))
        if args.command == "update":
            raise SystemExit(cmd_update(args))
        if args.command == "delete":
            raise SystemExit(cmd_delete(args.app, args.selector))
        if args.command == "check":
            raise SystemExit(cmd_check(args.app, args.provider, args.timeout_sec))
        if args.command == "test":
            raise SystemExit(cmd_test(args.app, args.json, args.timeout_sec))
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
                status = proxy_up_action(host=args.host, port=args.port)
                pid_suffix = f" (pid={status['pid']})" if status["pid"] else ""
                print(t(f"proxy running on http://{status['host']}:{status['port']}{pid_suffix}", f"代理已运行: http://{status['host']}:{status['port']}{pid_suffix}"))
                raise SystemExit(0)
            if args.proxy_command == "config":
                if args.proxy_config_command == "show":
                    raise SystemExit(cmd_proxy_config_show())
                if args.proxy_config_command == "set":
                    raise SystemExit(cmd_proxy_config_set(args.host, args.port, args.auto_failover, args.cooldown_sec, args.failure_threshold, args.retry_attempts, args.max_body_mb))
            if args.proxy_command == "run":
                raise SystemExit(cmd_proxy_run(args.host, args.port))
            if args.proxy_command == "down":
                stopped = proxy_down_action()
                print(t("proxy stopped", "代理已停止") if stopped else t("proxy was not running", "代理并未以前台后台模式运行，可能由 systemd 托管"))
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
