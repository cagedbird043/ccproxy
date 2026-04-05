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
    providers_for,
    resolve_provider_selector,
    runtime_dir,
)

CHECK_MARKER = "CCPROXY_CHECK_OK"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"


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


def _prepare_codex_temp_home(temp_home: Path) -> None:
    real_home = _codex_home()
    if not real_home.exists():
        return

    for child in real_home.iterdir():
        if child.name in {"config.toml", "auth.json"}:
            continue
        target = temp_home / child.name
        _safe_link_or_copy(child, target)


def _run_codex_check(provider: dict[str, Any]) -> tuple[int, str, str]:
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
        completed = subprocess.run(
            [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--model",
                model,
                f"Reply with exactly {CHECK_MARKER}",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        return completed.returncode, completed.stdout, completed.stderr


def _run_claude_check(provider: dict[str, Any]) -> tuple[int, str, str]:
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
        completed = subprocess.run(
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
            capture_output=True,
            text=True,
        )
        return completed.returncode, completed.stdout, completed.stderr
    finally:
        settings_path.unlink(missing_ok=True)


def run_check(app: str, selector: str | None = None) -> CheckResult:
    data = load_config()
    provider_id, provider = _resolve_provider(data, app, selector)
    name = provider.get("name", provider_id)

    started = time.monotonic()
    if app == "codex":
        returncode, stdout, stderr = _run_codex_check(provider)
        success = returncode == 0 and CHECK_MARKER in stdout
    elif app == "claude":
        returncode, stdout, stderr = _run_claude_check(provider)
        success = returncode == 0
    else:
        raise ValueError(f"unsupported app: {app}")

    duration = time.monotonic() - started
    if success:
        summary = "healthy"
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
    )


def next_provider_candidates(data: dict[str, Any], app: str) -> list[tuple[str, dict[str, Any]]]:
    app = normalize_app(app)
    providers = providers_for(data, app)
    if not providers:
        return []

    items = list(providers.items())
    current_id = current_provider_id(data, app)
    if current_id is None:
        return items

    index = next((idx for idx, (provider_id, _provider) in enumerate(items) if provider_id == current_id), None)
    if index is None:
        return items

    return items[index + 1 :] + items[: index + 1]
