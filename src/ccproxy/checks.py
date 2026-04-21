from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccproxy.config import (
    current_provider_id,
    load_config,
    normalize_app,
    ordered_provider_items,
    providers_for,
    resolve_provider_selector,
    runtime_dir,
)

CHECK_MARKER = "CCPROXY_CHECK_OK"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_CHECK_TIMEOUT_SEC = 45.0
CHECK_TIMEOUT_RETURN_CODE = 124


@dataclass
class CheckResult:
    app: str
    provider_id: str
    provider_name: str
    success: bool
    duration_sec: float
    summary: str
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


def _resolve_provider(
    data: dict[str, Any],
    app: str,
    selector: str | None,
) -> tuple[str, dict[str, Any]]:
    app = normalize_app(app)
    providers = providers_for(data, app)
    if not providers:
        raise ValueError(f"no providers configured for {app}")

    if selector:
        return resolve_provider_selector(providers, selector)

    current_id = current_provider_id(data, app)
    if not current_id:
        raise ValueError(f"no current provider set for {app}")
    return resolve_provider_selector(providers, current_id)


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


def _decode_subprocess_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _format_timeout_sec(timeout_sec: float) -> str:
    if float(timeout_sec).is_integer():
        return str(int(timeout_sec))
    return f"{timeout_sec:g}"


def resolve_check_timeout(timeout_sec: float | None = None) -> float:
    if timeout_sec is None:
        raw = os.environ.get("CCPROXY_CHECK_TIMEOUT_SEC")
        timeout_sec = float(raw) if raw else DEFAULT_CHECK_TIMEOUT_SEC

    timeout_value = float(timeout_sec)
    if timeout_value <= 0:
        raise ValueError(f"invalid check timeout: {timeout_value}")
    return timeout_value


def _run_subprocess(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout_sec: float,
) -> tuple[int, str, str, bool]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=timeout_sec,
        )
        return completed.returncode, completed.stdout, completed.stderr, False
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_subprocess_text(exc.stdout)
        stderr = _decode_subprocess_text(exc.stderr).rstrip()
        timeout_line = f"ERROR: check timed out after {_format_timeout_sec(timeout_sec)}s"
        if stderr:
            stderr = f"{stderr}\n{timeout_line}\n"
        else:
            stderr = f"{timeout_line}\n"
        return CHECK_TIMEOUT_RETURN_CODE, stdout, stderr, True


def _prepare_codex_temp_home(temp_home: Path) -> None:
    real_home = _codex_home()
    if not real_home.exists():
        return

    for child in real_home.iterdir():
        if child.name in {"config.toml", "auth.json"}:
            continue
        target = temp_home / child.name
        _safe_link_or_copy(child, target)


def _run_codex_check(provider: dict[str, Any], timeout_sec: float) -> tuple[int, str, str, bool]:
    model = provider.get("model") or "gpt-5.4"
    with tempfile.TemporaryDirectory(prefix="ccproxy-codex-check-", dir=runtime_dir()) as temp_dir:
        temp_home = Path(temp_dir)
        _prepare_codex_temp_home(temp_home)
        (temp_home / "config.toml").write_text(
            "\n".join(
                [
                    'model_provider = "ccproxy-check"',
                    f'model = "{model}"',
                    "",
                    "[model_providers.ccproxy-check]",
                    'name = "CCProxy Check"',
                    f'base_url = "{provider["base_url"]}"',
                    'wire_api = "responses"',
                    "requires_openai_auth = false",
                    "",
                ]
            )
        )
        (temp_home / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": provider["api_key"]}, indent=2) + "\n"
        )
        env = os.environ.copy()
        env["CODEX_HOME"] = str(temp_home)
        return _run_subprocess(
            [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--model",
                model,
                f"Reply with exactly {CHECK_MARKER}",
            ],
            env=env,
            timeout_sec=timeout_sec,
        )


def _run_claude_check(provider: dict[str, Any], timeout_sec: float) -> tuple[int, str, str, bool]:
    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="ccproxy-claude-check-",
        suffix=".json",
        dir=runtime_dir(),
        delete=False,
    ) as handle:
        settings_path = Path(handle.name)
        json.dump(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": provider["base_url"],
                    "ANTHROPIC_AUTH_TOKEN": provider["api_key"],
                    "ANTHROPIC_API_KEY": provider["api_key"],
                }
            },
            handle,
            indent=2,
        )
        handle.write("\n")

    try:
        return _run_subprocess(
            [
                "claude",
                "--settings",
                str(settings_path),
                "--bare",
                "--tools",
                "",
                "--model",
                DEFAULT_CLAUDE_MODEL,
                "--print",
                "--output-format",
                "json",
                "--max-budget-usd",
                "0.05",
                f"Reply with exactly {CHECK_MARKER}",
            ],
            timeout_sec=timeout_sec,
        )
    finally:
        settings_path.unlink(missing_ok=True)


def run_check(app: str, selector: str | None = None, timeout_sec: float | None = None) -> CheckResult:
    timeout_sec = resolve_check_timeout(timeout_sec)
    data = load_config()
    provider_id, provider = _resolve_provider(data, app, selector)
    name = provider.get("name", provider_id)

    started = time.monotonic()
    if app == "codex":
        returncode, stdout, stderr, timed_out = _run_codex_check(provider, timeout_sec)
        success = returncode == 0 and CHECK_MARKER in stdout
    elif app == "claude":
        returncode, stdout, stderr, timed_out = _run_claude_check(provider, timeout_sec)
        success = returncode == 0
    else:
        raise ValueError(f"unsupported app: {app}")

    duration = time.monotonic() - started
    if success:
        summary = "healthy"
    elif timed_out:
        summary = "timeout"
    else:
        summary = "failed"

    return CheckResult(
        app=app,
        provider_id=provider_id,
        provider_name=name,
        success=success,
        duration_sec=duration,
        summary=summary,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        timed_out=timed_out,
    )


def next_provider_candidates(data: dict[str, Any], app: str) -> list[tuple[str, dict[str, Any]]]:
    app = normalize_app(app)
    ordered = ordered_provider_items(data, app)
    if not ordered:
        return []

    current_id = current_provider_id(data, app)
    if current_id is None:
        return ordered

    return [
        item for item in ordered if item[0] != current_id
    ] + [
        item for item in ordered if item[0] == current_id
    ]
