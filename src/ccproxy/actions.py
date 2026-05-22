from __future__ import annotations

from typing import Any, Iterator

from ccproxy.checks import CheckResult, next_provider_candidates, run_check
from ccproxy.config import (
    APP_CHOICES,
    DEFAULT_PROVIDER_PRIORITY,
    current_provider_id,
    load_config,
    normalize_app,
    ordered_provider_items,
    provider_priority,
    proxy_runtime_status,
    remove_provider,
    resolve_provider_selector,
    save_config,
    set_current_provider,
    update_proxy_config,
    upsert_provider,
)
from ccproxy.launch import start_proxy_background, stop_proxy_background
from ccproxy.read_models import format_provider_label

CHECK_MARKER = "CCPROXY_CHECK_OK"
CHECK_DETAIL_NOISE_PREFIXES = (
    "Reading additional input from stdin...",
    "OpenAI Codex ",
    "workdir:",
    "model:",
    "provider:",
    "approval:",
    "sandbox:",
    "reasoning effort:",
    "reasoning summaries:",
    "session id:",
)
CHECK_DETAIL_NOISE_LINES = {"--------", "user", CHECK_MARKER}


def check_detail(result: CheckResult) -> str | None:
    import json

    error_lines: list[str] = []
    info_lines: list[str] = []

    for text in (result.stderr, result.stdout):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped in CHECK_DETAIL_NOISE_LINES:
                continue
            if stripped.startswith(CHECK_DETAIL_NOISE_PREFIXES):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                parsed = payload.get("result")
                if isinstance(parsed, str):
                    stripped = parsed.strip()
                else:
                    stripped = ""
                if not stripped or stripped in CHECK_DETAIL_NOISE_LINES:
                    continue
            if stripped.startswith("ERROR:"):
                error_lines.append(stripped)
                continue
            info_lines.append(stripped)
    if error_lines:
        return error_lines[-1]
    if result.success:
        return None
    if info_lines:
        return info_lines[0]
    return None


def run_check_action(
    app: str,
    provider: str | None = None,
    timeout_sec: float | None = None,
    *,
    transport: str = "http",
) -> CheckResult:
    return run_check(app, provider, timeout_sec=timeout_sec, transport=transport)


def build_test_row(
    provider_id: str,
    provider: dict[str, object],
    current_provider: str | None,
    result: CheckResult,
) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "provider_name": provider.get("name", provider_id),
        "priority": provider_priority(provider),
        "current": provider_id == current_provider,
        "success": result.success,
        "duration_sec": result.duration_sec,
        "summary": result.summary,
        "detail": check_detail(result),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "transport": result.transport,
    }


def iter_test_rows(
    app_name: str,
    ordered: list[tuple[str, dict[str, object]]],
    current: str | None,
    timeout_sec: float | None = None,
    transport: str = "http",
) -> Iterator[dict[str, object]]:
    for provider_id, provider in ordered:
        result = run_check_action(app_name, provider_id, timeout_sec=timeout_sec, transport=transport)
        yield build_test_row(provider_id, provider, current, result)


def update_test_summary(summary: dict[str, int], row: dict[str, object]) -> None:
    summary["total"] += 1
    if row["success"]:
        summary["ok"] += 1
    else:
        summary["fail"] += 1


def build_test_snapshot(
    app: str | None = None,
    timeout_sec: float | None = None,
    *,
    transport: str = "http",
) -> dict[str, object]:
    data = load_config()
    apps = [normalize_app(app)] if app else list(APP_CHOICES)
    snapshot: dict[str, object] = {"apps": {}, "summary": {"ok": 0, "fail": 0, "total": 0}}
    summary = snapshot["summary"]

    for app_name in apps:
        ordered = ordered_provider_items(data, app_name)
        current = current_provider_id(data, app_name)
        rows = list(iter_test_rows(app_name, ordered, current, timeout_sec=timeout_sec, transport=transport))
        snapshot["apps"][app_name] = rows
        for row in rows:
            update_test_summary(summary, row)

    return snapshot


def use_provider_action(app: str, selector: str) -> dict[str, object]:
    data = load_config()
    provider_id, provider = set_current_provider(data, app, selector)
    save_config(data)
    return {
        "app": app,
        "provider_id": provider_id,
        "provider": provider,
        "proxy_status": proxy_runtime_status(data),
        "provider_label": format_provider_label(provider_id, provider.get("name", provider_id)),
    }


def add_provider_action(
    app: str,
    provider_id: str,
    *,
    name: str | None,
    base_url: str,
    api_key: str,
    model: str | None = None,
    auth_mode: str | None = None,
    set_current: bool = False,
    priority: int | None = None,
    supports_websockets: bool | None = None,
) -> dict[str, object]:
    data = load_config()
    provider = {
        "name": name or provider_id,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "auth_mode": auth_mode or "bearer",
        "priority": priority if priority is not None else DEFAULT_PROVIDER_PRIORITY,
    }
    if supports_websockets is not None:
        provider["supports_websockets"] = supports_websockets
    upsert_provider(data, app, provider_id, provider, set_current=set_current)
    save_config(data)
    return {
        "app": app,
        "provider_id": provider_id,
        "provider": provider,
        "provider_label": format_provider_label(provider_id, provider.get("name", provider_id)),
        "set_current": set_current,
    }


def update_provider_action(
    app: str,
    selector: str,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    auth_mode: str | None = None,
    priority: int | None = None,
    supports_websockets: bool | None = None,
    set_current: bool = False,
) -> dict[str, object]:
    data = load_config()
    provider_id, provider = resolve_provider_selector(data["apps"][app]["providers"], selector)
    changed: dict[str, object] = {}
    for key, value in (
        ("name", name),
        ("base_url", base_url),
        ("api_key", api_key),
        ("model", model),
        ("auth_mode", auth_mode),
        ("priority", priority),
        ("supports_websockets", supports_websockets),
    ):
        if value is None:
            continue
        if provider.get(key) != value:
            provider[key] = value
            changed[key] = value
    if set_current and current_provider_id(data, app) != provider_id:
        set_current_provider(data, app, provider_id)
        changed["current"] = provider_id
    if changed:
        save_config(data)
    return {
        "app": app,
        "provider_id": provider_id,
        "provider": provider,
        "provider_label": format_provider_label(provider_id, provider.get("name", provider_id)),
        "changed": changed,
    }


def delete_provider_action(app: str, selector: str) -> dict[str, object]:
    data = load_config()
    previous_current = current_provider_id(data, app)
    provider_id, provider = remove_provider(data, app, selector)
    new_current = current_provider_id(data, app)
    save_config(data)
    return {
        "app": app,
        "provider_id": provider_id,
        "provider": provider,
        "provider_label": format_provider_label(provider_id, provider.get("name", provider_id)),
        "previous_current": previous_current,
        "new_current": new_current,
        "new_current_provider": None if new_current is None else resolve_provider_selector(data["apps"][app]["providers"], new_current),
    }


def next_provider_action(app: str, timeout_sec: float | None = None) -> dict[str, object]:
    data = load_config()
    candidates = next_provider_candidates(data, app)
    if not candidates:
        raise ValueError(f"no providers configured for {app}")

    current = current_provider_id(data, app)
    attempts: list[dict[str, object]] = []
    for provider_id, provider in candidates:
        if provider_id == current and len(candidates) > 1:
            continue
        result = run_check_action(app, provider_id, timeout_sec=timeout_sec)
        attempts.append(
            {
                "provider_id": provider_id,
                "provider": provider,
                "result": result,
                "provider_label": format_provider_label(provider_id, provider.get("name", provider_id)),
            }
        )
        if result.success:
            _, selected = set_current_provider(data, app, provider_id)
            save_config(data)
            return {
                "app": app,
                "selected_provider_id": provider_id,
                "selected_provider": selected,
                "selected_label": format_provider_label(provider_id, selected.get("name", provider_id)),
                "attempts": attempts,
                "proxy_status": proxy_runtime_status(data),
            }
    return {"app": app, "attempts": attempts, "selected_provider_id": None}


def proxy_up_action(host: str | None = None, port: int | None = None) -> dict[str, object]:
    return start_proxy_background(host=host, port=port)


def proxy_down_action() -> bool:
    return stop_proxy_background()


def proxy_config_set_action(
    *,
    host: str | None = None,
    port: int | None = None,
    auto_failover: bool | None = None,
    cooldown_sec: int | None = None,
    failure_threshold: int | None = None,
    retry_attempts: int | None = None,
    max_body_mb: int | None = None,
) -> dict[str, object]:
    data = load_config()
    changed = update_proxy_config(
        data,
        host=host,
        port=port,
        auto_failover=auto_failover,
        cooldown_sec=cooldown_sec,
        failure_threshold=failure_threshold,
        retry_attempts=retry_attempts,
        max_body_mb=max_body_mb,
    )
    save_config(data)
    return {"changed": changed, "runtime": proxy_runtime_status(data)}
