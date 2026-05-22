from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType

from ccproxy.adapters import build_upstream_url, build_upstream_websocket_url
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
CHECK_HTTP_ERROR_RETURN_CODE = 1
CHECK_WEBSOCKET_UNSUPPORTED_RETURN_CODE = 2
DEFAULT_CODEX_TUI_VERSION = "0.131.0"
CODEX_CHECK_USER_AGENT_ENV = "CCPROXY_CODEX_CHECK_USER_AGENT"
RESPONSES_WEBSOCKET_BETA = "responses_websockets=2026-02-06"


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
    transport: str = "http"


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


def _parse_codex_version(output: str) -> str | None:
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
    return match.group(1) if match else None


def _detect_codex_version() -> str:
    try:
        completed = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return DEFAULT_CODEX_TUI_VERSION
    return _parse_codex_version(f"{completed.stdout}\n{completed.stderr}") or DEFAULT_CODEX_TUI_VERSION


def _os_release_label() -> str:
    try:
        raw = Path("/etc/os-release").read_text()
    except OSError:
        return "Linux"

    values: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    if values.get("ID") == "arch":
        return "Arch Linux Rolling Release"
    return values.get("PRETTY_NAME") or values.get("NAME") or "Linux"


def _konsole_version_label(env: dict[str, str]) -> str | None:
    raw = env.get("KONSOLE_VERSION")
    if not raw or not raw.isdigit():
        return None
    value = int(raw)
    major = value // 10000
    minor = (value // 100) % 100
    patch = value % 100
    return f"Konsole/{major}.{minor:02d}.{patch}"


def codex_check_user_agent(env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    override = env.get(CODEX_CHECK_USER_AGENT_ENV)
    if override:
        return override

    version = _detect_codex_version()
    platform_label = f"{_os_release_label()}; {os.uname().machine}"
    terminal_label = _konsole_version_label(env) or "Terminal"
    return f"codex-tui/{version} ({platform_label}) {terminal_label} (codex-tui; {version})"


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


def _codex_check_headers(provider: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": codex_check_user_agent(),
    }


def _codex_websocket_headers(provider: dict[str, Any]) -> dict[str, str]:
    headers = _codex_check_headers(provider)
    headers["OpenAI-Beta"] = RESPONSES_WEBSOCKET_BETA
    return headers


def _codex_check_payload(provider: dict[str, Any]) -> bytes:
    model = provider.get("model") or "gpt-5.4"
    return json.dumps(
        {
            "model": model,
            "input": f"Reply with exactly {CHECK_MARKER}",
            "stream": False,
            "max_output_tokens": 16,
        }
    ).encode()


def _codex_websocket_check_payload(provider: dict[str, Any]) -> str:
    payload = json.loads(_codex_check_payload(provider).decode())
    payload["type"] = "response.create"
    payload["stream"] = True
    return json.dumps(payload)


def _decode_http_body(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace").strip()

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or error.get("type")
            if isinstance(message, str):
                return message.strip()
        if isinstance(error, str):
            return error.strip()
    return body.decode("utf-8", errors="replace").strip()


def _extract_responses_text(payload: object) -> str:
    if isinstance(payload, dict):
        output_text = payload.get("output_text")
        if isinstance(output_text, str):
            return output_text
        delta = payload.get("delta")
        if isinstance(delta, str):
            return delta
        text = payload.get("text")
        if isinstance(text, str):
            return text
        content = payload.get("content")
        if isinstance(content, list):
            content_parts: list[str] = []
            for content_item in content:
                content_text = _extract_responses_text(content_item)
                if content_text:
                    content_parts.append(content_text)
            if content_parts:
                return "\n".join(content_parts)
        item = payload.get("item")
        item_text = _extract_responses_text(item)
        if item_text:
            return item_text
        output = payload.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for content_item in content:
                    if not isinstance(content_item, dict):
                        continue
                    text = content_item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            if parts:
                return "\n".join(parts)
    return ""


def _run_codex_check(provider: dict[str, Any], timeout_sec: float) -> tuple[int, str, str, bool]:
    url = build_upstream_url(str(provider["base_url"]), "/v1/responses")
    request = urllib.request.Request(
        url,
        data=_codex_check_payload(provider),
        headers=_codex_check_headers(provider),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = _decode_http_body(exc.read())
        message = f"ERROR: upstream status {exc.code} {exc.reason}"
        if detail:
            message = f"{message}: {detail}"
        return CHECK_HTTP_ERROR_RETURN_CODE, "", f"{message}\n", False
    except TimeoutError:
        return CHECK_TIMEOUT_RETURN_CODE, "", f"ERROR: check timed out after {_format_timeout_sec(timeout_sec)}s\n", True
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return CHECK_TIMEOUT_RETURN_CODE, "", f"ERROR: check timed out after {_format_timeout_sec(timeout_sec)}s\n", True
        return CHECK_HTTP_ERROR_RETURN_CODE, "", f"ERROR: {reason}\n", False

    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        text = body.decode("utf-8", errors="replace")
    else:
        text = _extract_responses_text(payload)
    return 0, text, "", False


async def _run_codex_websocket_check_async(
    provider: dict[str, Any],
    timeout_sec: float,
) -> tuple[int, str, str, bool]:
    if not provider.get("supports_websockets", False):
        return (
            CHECK_WEBSOCKET_UNSUPPORTED_RETURN_CODE,
            "",
            "ERROR: provider websocket support is disabled; set supports_websockets=true first\n",
            False,
        )

    url = build_upstream_websocket_url(str(provider["base_url"]), "/v1/responses")
    timeout = ClientTimeout(total=timeout_sec, connect=min(timeout_sec, 10), sock_connect=min(timeout_sec, 10), sock_read=timeout_sec)
    text_parts: list[str] = []
    completed = False
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                url,
                headers=_codex_websocket_headers(provider),
                autoclose=True,
                autoping=True,
                max_msg_size=0,
            ) as ws:
                await ws.send_str(_codex_websocket_check_payload(provider))
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        try:
                            event = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        event_type = event.get("type") if isinstance(event, dict) else None
                        text = _extract_responses_text(event)
                        if text:
                            text_parts.append(text)
                        if event_type in {"response.completed", "response.done"}:
                            completed = True
                            break
                        if event_type == "error":
                            return CHECK_HTTP_ERROR_RETURN_CODE, "", f"ERROR: {msg.data}\n", False
                    elif msg.type == WSMsgType.ERROR:
                        raise ws.exception() or RuntimeError("websocket error")
                    elif msg.type in {WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.CLOSING}:
                        break
    except asyncio.TimeoutError:
        return CHECK_TIMEOUT_RETURN_CODE, "", f"ERROR: websocket check timed out after {_format_timeout_sec(timeout_sec)}s\n", True
    except (ClientError, OSError, ValueError) as exc:
        return CHECK_HTTP_ERROR_RETURN_CODE, "", f"ERROR: websocket {exc}\n", False

    text = "\n".join(text_parts)
    if completed or CHECK_MARKER in text:
        return 0, text, "", False
    return CHECK_HTTP_ERROR_RETURN_CODE, text, "ERROR: websocket stream ended before response.completed\n", False


def _run_codex_websocket_check(provider: dict[str, Any], timeout_sec: float) -> tuple[int, str, str, bool]:
    try:
        return asyncio.run(_run_codex_websocket_check_async(provider, timeout_sec))
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run_codex_websocket_check_async(provider, timeout_sec))
        finally:
            loop.close()

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


def run_check(
    app: str,
    selector: str | None = None,
    timeout_sec: float | None = None,
    *,
    transport: str = "http",
) -> CheckResult:
    timeout_sec = resolve_check_timeout(timeout_sec)
    data = load_config()
    provider_id, provider = _resolve_provider(data, app, selector)
    name = provider.get("name", provider_id)

    started = time.monotonic()
    if app == "codex":
        if transport == "websocket":
            returncode, stdout, stderr, timed_out = _run_codex_websocket_check(provider, timeout_sec)
        else:
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
        transport=transport,
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
