from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_CHOICES = ("codex", "claude")


def _xdg_dir(env_key: str, default_suffix: str) -> Path:
    raw = os.environ.get(env_key)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / default_suffix


def config_dir() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "ccproxy"


def state_dir() -> Path:
    return _xdg_dir("XDG_STATE_HOME", ".local/state") / "ccproxy"


def runtime_dir() -> Path:
    path = state_dir() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


def pid_path() -> Path:
    return state_dir() / "proxy.pid"


def log_path() -> Path:
    return state_dir() / "proxy.log"


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "proxy": {
            "host": "127.0.0.1",
            "port": 15721,
            "auto_failover": True,
        },
        "apps": {
            app: {
                "current": None,
                "providers": {},
            }
            for app in APP_CHOICES
        },
    }


def load_config() -> dict[str, Any]:
    ensure_dirs()
    path = config_path()
    if not path.exists():
        return default_config()

    data = json.loads(path.read_text())
    merged = default_config()
    merged["version"] = data.get("version", 1)
    merged["proxy"].update(data.get("proxy", {}))
    for app in APP_CHOICES:
        app_data = data.get("apps", {}).get(app, {})
        merged["apps"][app]["current"] = app_data.get("current")
        merged["apps"][app]["providers"] = app_data.get("providers", {})
    return merged


def save_config(data: dict[str, Any]) -> None:
    ensure_dirs()
    config_path().write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def init_config() -> Path:
    data = load_config()
    save_config(data)
    return config_path()


def normalize_app(app: str | None) -> str:
    if app is None:
        return "codex"
    if app not in APP_CHOICES:
        raise ValueError(f"unsupported app: {app}")
    return app


def providers_for(data: dict[str, Any], app: str) -> dict[str, dict[str, Any]]:
    app = normalize_app(app)
    return data["apps"][app]["providers"]


def resolve_provider_selector(
    providers: dict[str, dict[str, Any]], selector: str
) -> tuple[str, dict[str, Any]]:
    if selector in providers:
        return selector, providers[selector]

    matches = [
        (provider_id, provider)
        for provider_id, provider in providers.items()
        if provider.get("name") == selector
    ]
    if not matches:
        raise ValueError(f"provider not found: {selector}")
    if len(matches) > 1:
        ids = ", ".join(provider_id for provider_id, _ in matches)
        raise ValueError(f"provider selector is ambiguous: {selector} -> {ids}")
    return matches[0]


def current_provider_id(data: dict[str, Any], app: str) -> str | None:
    app = normalize_app(app)
    return data["apps"][app]["current"]


def current_provider(data: dict[str, Any], app: str) -> tuple[str, dict[str, Any]]:
    providers = providers_for(data, app)
    current_id = current_provider_id(data, app)
    if not current_id:
        raise ValueError(f"no current provider set for {app}")
    if current_id not in providers:
        raise ValueError(f"current provider missing from config: {current_id}")
    return current_id, providers[current_id]


def set_current_provider(data: dict[str, Any], app: str, selector: str) -> tuple[str, dict[str, Any]]:
    app = normalize_app(app)
    provider_id, provider = resolve_provider_selector(providers_for(data, app), selector)
    data["apps"][app]["current"] = provider_id
    return provider_id, provider


def upsert_provider(
    data: dict[str, Any],
    app: str,
    provider_id: str,
    provider: dict[str, Any],
    *,
    set_current: bool = False,
) -> None:
    app = normalize_app(app)
    providers_for(data, app)[provider_id] = provider
    if set_current or not data["apps"][app]["current"]:
        data["apps"][app]["current"] = provider_id


def pid_from_file() -> int | None:
    path = pid_path()
    if not path.exists():
        return None
    raw = path.read_text().strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def remove_stale_pid_file() -> None:
    pid = pid_from_file()
    if pid is not None and not is_pid_running(pid):
        pid_path().unlink(missing_ok=True)


def proxy_runtime_status(data: dict[str, Any]) -> dict[str, Any]:
    remove_stale_pid_file()
    pid = pid_from_file()
    return {
        "running": is_pid_running(pid),
        "pid": pid,
        "host": data["proxy"]["host"],
        "port": data["proxy"]["port"],
        "auto_failover": bool(data["proxy"].get("auto_failover", True)),
        "log_path": str(log_path()),
    }


def signal_pid(pid: int, signum: int) -> None:
    os.kill(pid, signum)


def tail_file(path: Path, limit: int = 80) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return lines[-limit:]
