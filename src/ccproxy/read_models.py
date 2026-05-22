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
from ccproxy.health_store import reorder_providers_by_cooldown


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
                "supports_websockets": bool(provider.get("supports_websockets", False)),
            }
        )
    return rows


def build_current_provider_summary(app: str) -> dict[str, object]:
    data = load_config()
    state = load_health_state()
    app = normalize_app(app)
    provider_id, provider = current_provider(data, app)
    ordered = ordered_provider_items(data, app)
    effective_id, effective_provider = reorder_providers_by_cooldown(ordered, state, app)[0]
    current_entry = ensure_provider_entry(
        state,
        app,
        provider_id,
        provider.get("name", provider_id),
    )
    current_in_cooldown = provider_in_cooldown(current_entry)
    effective_matches_current = effective_id == provider_id
    if effective_matches_current:
        effective_reason = "selected provider will be tried first"
    elif current_in_cooldown:
        effective_reason = f"selected provider cooling down until {format_timestamp(current_entry.get('cooldown_until'))}"
    else:
        effective_reason = "runtime provider order currently prefers another provider"
    return {
        "app": app,
        "provider_id": provider_id,
        "provider_name": provider.get("name", provider_id),
        "priority": provider_priority(provider),
        "base_url": provider.get("base_url"),
        "label": format_provider_label(provider_id, provider.get("name", provider_id)),
        "selected_provider_id": provider_id,
        "selected_provider_name": provider.get("name", provider_id),
        "selected_priority": provider_priority(provider),
        "selected_base_url": provider.get("base_url"),
        "selected_label": format_provider_label(provider_id, provider.get("name", provider_id)),
        "selected_status": "cooldown" if current_in_cooldown else "ready",
        "selected_cooldown_until": format_timestamp(current_entry.get("cooldown_until")),
        "selected_supports_websockets": bool(provider.get("supports_websockets", False)),
        "effective_provider_id": effective_id,
        "effective_provider_name": effective_provider.get("name", effective_id),
        "effective_priority": provider_priority(effective_provider),
        "effective_base_url": effective_provider.get("base_url"),
        "effective_label": format_provider_label(effective_id, effective_provider.get("name", effective_id)),
        "effective_supports_websockets": bool(effective_provider.get("supports_websockets", False)),
        "effective_matches_selected": effective_matches_current,
        "effective_reason": effective_reason,
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
        effective_id = reorder_providers_by_cooldown(ordered, state, app_name)[0][0] if ordered else None
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
                    "effective": provider_id == effective_id,
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
                    "supports_websockets": bool(provider.get("supports_websockets", False)),
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
        effective_row = next((row for row in provider_rows if row.get("effective")), None)
        apps[app_name] = {
            "current_provider_id": current_id,
            "current_provider_name": None if current_row is None else current_row["provider_name"],
            "effective_provider_id": None if effective_row is None else effective_row["provider_id"],
            "effective_provider_name": None if effective_row is None else effective_row["provider_name"],
            "providers": provider_rows,
        }

    return {
        "proxy": proxy,
        "apps": apps,
        "health_state_file": health["health_state_file"],
    }
