from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ccproxy.config import APP_CHOICES, health_state_path


def default_health_state() -> dict[str, Any]:
    return {
        "version": 1,
        "apps": {app: {} for app in APP_CHOICES},
    }


def load_health_state() -> dict[str, Any]:
    path = health_state_path()
    if not path.exists():
        return default_health_state()

    raw = json.loads(path.read_text())
    merged = default_health_state()
    for app in APP_CHOICES:
        merged["apps"][app].update(raw.get("apps", {}).get(app, {}))
    return merged


def save_health_state(data: dict[str, Any]) -> None:
    path = health_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def ensure_provider_entry(
    state: dict[str, Any],
    app: str,
    provider_id: str,
    provider_name: str | None = None,
) -> dict[str, Any]:
    entry = state["apps"][app].setdefault(
        provider_id,
        {
            "provider_name": provider_name or provider_id,
            "total_successes": 0,
            "total_failures": 0,
            "consecutive_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_error": None,
            "cooldown_until": None,
        },
    )
    if provider_name:
        entry["provider_name"] = provider_name
    return entry


def provider_in_cooldown(entry: dict[str, Any], now_ts: float | None = None) -> bool:
    now_ts = now_ts or time.time()
    cooldown_until = entry.get("cooldown_until")
    return bool(cooldown_until and cooldown_until > now_ts)


def reorder_providers_by_cooldown(
    ordered_items: list[tuple[str, dict[str, Any]]],
    state: dict[str, Any],
    app: str,
    now_ts: float | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    now_ts = now_ts or time.time()
    ready: list[tuple[str, dict[str, Any]]] = []
    cooling: list[tuple[str, dict[str, Any]]] = []
    for provider_id, provider in ordered_items:
        entry = state["apps"][app].get(provider_id)
        if entry and provider_in_cooldown(entry, now_ts):
            cooling.append((provider_id, provider))
        else:
            ready.append((provider_id, provider))
    return ready + cooling if ready else ordered_items


def record_failure(
    state: dict[str, Any],
    app: str,
    provider_id: str,
    provider_name: str,
    error: str,
    cooldown_sec: int,
    failure_threshold: int,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now_ts = now_ts or time.time()
    entry = ensure_provider_entry(state, app, provider_id, provider_name)
    entry["total_failures"] += 1
    entry["consecutive_failures"] += 1
    entry["last_failure_at"] = now_ts
    entry["last_error"] = error
    if entry["consecutive_failures"] >= max(failure_threshold, 1):
        entry["cooldown_until"] = now_ts + max(cooldown_sec, 0)
    else:
        entry["cooldown_until"] = None
    return entry


def record_success(
    state: dict[str, Any],
    app: str,
    provider_id: str,
    provider_name: str,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now_ts = now_ts or time.time()
    entry = ensure_provider_entry(state, app, provider_id, provider_name)
    entry["total_successes"] += 1
    entry["consecutive_failures"] = 0
    entry["last_success_at"] = now_ts
    entry["last_error"] = None
    entry["cooldown_until"] = None
    return entry


def format_timestamp(timestamp: float | None) -> str:
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
