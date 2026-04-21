from __future__ import annotations

from typing import Any

from ccproxy.config import (
    APP_CHOICES,
    current_provider,
    current_provider_id,
    health_state_path,
    load_config,
    normalize_app,
    ordered_provider_items,
    provider_priority,
    proxy_runtime_status,
)
from ccproxy.health_store import ensure_provider_entry, format_timestamp, load_health_state, provider_in_cooldown


def format_provider_label(provider_id: str, provider_name: str | None) -> str:
    name = (provider_name or provider_id).strip() or provider_id
    if name == provider_id:
        return provider_id
    return f"{name} ({provider_id})"


def build_provider_rows(app: str) -> list[dict[str, object]]:
    config = load_config()
    app = normalize_app(app)
    current = config["apps"][app]["current"]
    ordered = ordered_provider_items(config, app)
    rows: list[dict[str, object]] = []
    for provider_id, provider in ordered:
        rows.append(
            {
                "provider_id": provider_id,
                "provider_name": provider.get("name", provider_id),
                "base_url": provider.get("base_url", ""),
                "priority": provider_priority(provider),
                "current": provider_id == current,
                "model": provider.get("model"),
                "auth_mode": provider.get("auth_mode", "bearer"),
            }
        )
    return rows


def build_current_provider_summary(app: str) -> dict[str, object]:
    data = load_config()
    provider_id, provider = current_provider(data, app)
    return {
        "app": app,
        "provider_id": provider_id,
        "provider_name": provider.get("name", provider_id),
        "priority": provider_priority(provider),
        "base_url": provider.get("base_url"),
        "label": format_provider_label(provider_id, provider.get("name", provider_id)),
    }


def build_health_snapshot(app: str | None = None) -> dict[str, object]:
    config = load_config()
    state = load_health_state()
    apps = [normalize_app(app)] if app else list(APP_CHOICES)
    result: dict[str, object] = {"apps": {}, "health_state_file": str(health_state_path())}

    for app_name in apps:
        current = config["apps"][app_name]["current"]
        rows: list[dict[str, object]] = []
        ordered = ordered_provider_items(config, app_name)
        for provider_id, provider in ordered:
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
                    "base_url": provider.get("base_url", ""),
                    "priority": provider_priority(provider),
                    "current": provider_id == current,
                    "status": "cooldown" if provider_in_cooldown(entry) else "ready",
                    "total_successes": entry["total_successes"],
                    "total_failures": entry["total_failures"],
                    "consecutive_failures": entry["consecutive_failures"],
                    "last_success_at": format_timestamp(entry.get("last_success_at")),
                    "last_failure_at": format_timestamp(entry.get("last_failure_at")),
                    "cooldown_until": format_timestamp(entry.get("cooldown_until")),
                    "last_error": entry.get("last_error"),
                    "model": provider.get("model"),
                    "auth_mode": provider.get("auth_mode", "bearer"),
                }
            )
        result["apps"][app_name] = rows
    return result


def build_dashboard_snapshot() -> dict[str, object]:
    config = load_config()
    proxy = proxy_runtime_status(config)
    health = build_health_snapshot()
    apps: dict[str, Any] = {}

    for app_name in APP_CHOICES:
        current_id = current_provider_id(config, app_name)
        provider_rows = health["apps"][app_name]
        current_row = next((row for row in provider_rows if row["provider_id"] == current_id), None)
        apps[app_name] = {
            "current_provider_id": current_id,
            "current_provider_name": None if current_row is None else current_row["provider_name"],
            "providers": provider_rows,
        }

    return {
        "proxy": proxy,
        "apps": apps,
        "health_state_file": health["health_state_file"],
    }
