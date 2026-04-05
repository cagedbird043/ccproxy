from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ccproxy.service import systemd_service_scope

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


def health_state_path() -> Path:
    return state_dir() / "health.json"


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
            "cooldown_sec": 60,
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


def proxy_config(data: dict[str, Any]) -> dict[str, Any]:
    return data["proxy"]


def update_proxy_config(
    data: dict[str, Any],
    *,
    host: str | None = None,
    port: int | None = None,
    auto_failover: bool | None = None,
    cooldown_sec: int | None = None,
) -> dict[str, Any]:
    proxy = proxy_config(data)
    changed: dict[str, Any] = {}

    if host is not None and host != proxy["host"]:
        proxy["host"] = host
        changed["host"] = host

    if port is not None:
        if port <= 0 or port > 65535:
            raise ValueError(f"invalid port: {port}")
        if port != proxy["port"]:
            proxy["port"] = port
            changed["port"] = port

    if auto_failover is not None and auto_failover != bool(proxy.get("auto_failover", True)):
        proxy["auto_failover"] = auto_failover
        changed["auto_failover"] = auto_failover

    if cooldown_sec is not None:
        if cooldown_sec < 0:
            raise ValueError(f"invalid cooldown_sec: {cooldown_sec}")
        if cooldown_sec != int(proxy.get("cooldown_sec", 60)):
            proxy["cooldown_sec"] = cooldown_sec
            changed["cooldown_sec"] = cooldown_sec

    return changed


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


def proxy_health_ok(host: str, port: int, timeout_sec: float = 0.5) -> bool:
    url = f"http://{host}:{port}/__ccproxy/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def proxy_runtime_status(data: dict[str, Any]) -> dict[str, Any]:
    remove_stale_pid_file()
    pid = pid_from_file()
    host = data["proxy"]["host"]
    port = data["proxy"]["port"]
    pid_running = is_pid_running(pid)
    health_ok = proxy_health_ok(host, port)
    systemd_scope = systemd_service_scope()

    manager = None
    if pid_running:
        manager = "pidfile"
    elif systemd_scope:
        manager = f"systemd-{systemd_scope}"
    elif health_ok:
        manager = "external"

    return {
        "running": pid_running or health_ok,
        "pid": pid,
        "host": host,
        "port": port,
        "auto_failover": bool(data["proxy"].get("auto_failover", True)),
        "cooldown_sec": int(data["proxy"].get("cooldown_sec", 60)),
        "log_path": str(log_path()),
        "health_path": str(health_state_path()),
        "healthy": health_ok,
        "manager": manager,
        "systemd_scope": systemd_scope,
    }


def signal_pid(pid: int, signum: int) -> None:
    os.kill(pid, signum)


def tail_file(path: Path, limit: int = 80) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return lines[-limit:]
