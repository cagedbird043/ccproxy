from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from ccproxy.config import (
    current_provider_id,
    is_pid_running,
    load_config,
    log_path,
    pid_from_file,
    pid_path,
    proxy_runtime_status,
    save_config,
    set_current_provider,
    signal_pid,
    state_dir,
)


def _strip_remainder(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def _wait_for_health(host: str, port: int, timeout_sec: float = 5.0) -> None:
    deadline = time.time() + timeout_sec
    url = f"http://{host}:{port}/__ccproxy/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    raise RuntimeError(f"proxy did not become healthy: {url}")


def ensure_proxy_running(host: str, port: int) -> None:
    status = proxy_runtime_status(load_config())
    if status["running"]:
        return
    start_proxy_background(host=host, port=port)


def start_proxy_background(host: str | None = None, port: int | None = None) -> dict[str, object]:
    data = load_config()
    if host:
        data["proxy"]["host"] = host
    if port:
        data["proxy"]["port"] = port
    save_config(data)

    status = proxy_runtime_status(data)
    if status["running"]:
        return status

    state_dir().mkdir(parents=True, exist_ok=True)
    log_file = log_path().open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "ccproxy",
        "_proxy-run",
        "--host",
        data["proxy"]["host"],
        "--port",
        str(data["proxy"]["port"]),
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_file.close()
    pid_path().write_text(f"{proc.pid}\n")
    try:
        _wait_for_health(data["proxy"]["host"], data["proxy"]["port"])
    except Exception:
        if is_pid_running(proc.pid):
            proc.terminate()
        raise
    return proxy_runtime_status(data)


def stop_proxy_background() -> bool:
    pid = pid_from_file()
    if not pid or not is_pid_running(pid):
        pid_path().unlink(missing_ok=True)
        return False

    signal_pid(pid, signal.SIGINT)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not is_pid_running(pid):
            pid_path().unlink(missing_ok=True)
            return True
        time.sleep(0.1)

    signal_pid(pid, signal.SIGTERM)
    deadline = time.time() + 2
    while time.time() < deadline:
        if not is_pid_running(pid):
            pid_path().unlink(missing_ok=True)
            return True
        time.sleep(0.1)
    return False


def _run_with_exit_code(cmd: list[str], env: dict[str, str] | None = None) -> int:
    completed = subprocess.run(cmd, env=env)
    return int(completed.returncode)


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _safe_link_or_copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        try:
            dst.symlink_to(src, target_is_directory=True)
        except OSError:
            shutil.copytree(src, dst, dirs_exist_ok=True)
        return

    try:
        dst.symlink_to(src)
    except OSError:
        shutil.copy2(src, dst)


def _prepare_codex_temp_home(temp_home: Path) -> None:
    real_home = _codex_home()
    if not real_home.exists():
        return

    for child in real_home.iterdir():
        if child.name in {"config.toml", "auth.json"}:
            continue
        target = temp_home / child.name
        _safe_link_or_copy(child, target)


def _codex_proxy_config(model: str | None) -> str:
    chosen_model = model or "gpt-5.4"
    return "\n".join(
        [
            'model_provider = "ccproxy"',
            f'model = "{chosen_model}"',
            f'review_model = "{chosen_model}"',
            "",
            "[model_providers.ccproxy]",
            'name = "CCProxy"',
            'base_url = "http://127.0.0.1:15721/v1"',
            'wire_api = "responses"',
            "requires_openai_auth = false",
            "",
        ]
    )


def launch_codex(selector: str | None, extra_args: list[str]) -> int:
    data = load_config()
    if selector:
        set_current_provider(data, "codex", selector)
        save_config(data)

    host = data["proxy"]["host"]
    port = data["proxy"]["port"]
    ensure_proxy_running(host, port)

    if not current_provider_id(data, "codex"):
        raise ValueError("no current codex provider configured")
    _, provider = set_current_provider(data, "codex", current_provider_id(data, "codex"))
    save_config(data)

    with tempfile.TemporaryDirectory(prefix="ccproxy-codex-") as temp_dir:
        temp_home = Path(temp_dir)
        _prepare_codex_temp_home(temp_home)
        (temp_home / "config.toml").write_text(_codex_proxy_config(provider.get("model")))
        (temp_home / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "ccproxy-placeholder"}, indent=2) + "\n"
        )
        env = os.environ.copy()
        env["CODEX_HOME"] = str(temp_home)
        cmd = ["codex", *_strip_remainder(extra_args)]
        return _run_with_exit_code(cmd, env=env)


def launch_claude(selector: str | None, extra_args: list[str]) -> int:
    data = load_config()
    if selector:
        set_current_provider(data, "claude", selector)
        save_config(data)

    host = data["proxy"]["host"]
    port = data["proxy"]["port"]
    ensure_proxy_running(host, port)

    if not current_provider_id(data, "claude"):
        raise ValueError("no current claude provider configured")

    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="ccproxy-claude-",
        suffix=".json",
        delete=False,
    ) as handle:
        settings_path = Path(handle.name)
        json.dump(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": f"http://{host}:{port}",
                    "ANTHROPIC_AUTH_TOKEN": "ccproxy-placeholder",
                    "ANTHROPIC_API_KEY": "ccproxy-placeholder",
                }
            },
            handle,
            indent=2,
        )
        handle.write("\n")

    try:
        cmd = ["claude", "--settings", str(settings_path), *_strip_remainder(extra_args)]
        return _run_with_exit_code(cmd)
    finally:
        settings_path.unlink(missing_ok=True)
