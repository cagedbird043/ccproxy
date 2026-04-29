from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from ccproxy.adapters import ADAPTERS_BY_APP, build_upstream_url, route_request
from ccproxy.config import load_config, ordered_provider_items, proxy_max_body_bytes
from ccproxy.health_store import (
    load_health_state,
    record_failure,
    record_success,
    reorder_providers_by_cooldown,
    save_health_state,
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

FAILOVER_STATUS_CODES = {408, 429, 500, 502, 503, 504}
ERROR_BODY_PREVIEW_BYTES = 16 * 1024
ERROR_DETAIL_LIMIT = 400
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HTML_TAG_RE = re.compile(r"<[^>]+>")
QUOTA_EXHAUSTED_TERMS = (
    "quota exceeded",
    "quota has been exhausted",
    "exceeded your current quota",
    "insufficient_quota",
    "insufficient quota",
    "quota low",
    "billing hard limit",
    "credit balance",
    "insufficient balance",
    "balance is too low",
    "balance too low",
    "余额不足",
    "额度不足",
    "配额不足",
    "配额已用尽",
)
PAYLOAD_TOO_LARGE_TERMS = (
    "payload too large",
    "request entity too large",
    "content too large",
    "body too large",
    "client intended to send too large body",
    "请求体过大",
    "内容过大",
)


def should_failover_status(status_code: int) -> bool:
    return status_code in FAILOVER_STATUS_CODES


def is_quota_exhausted_detail(detail: str | None) -> bool:
    if not detail:
        return False
    normalized = _normalize_text(detail).casefold()
    return any(term in normalized for term in QUOTA_EXHAUSTED_TERMS)


def is_payload_too_large_detail(detail: str | None) -> bool:
    if not detail:
        return False
    normalized = _normalize_text(detail).casefold()
    return any(term in normalized for term in PAYLOAD_TOO_LARGE_TERMS)


def is_payload_too_large_response(status_code: int, detail: str | None = None) -> bool:
    return status_code == 413 or is_payload_too_large_detail(detail)


def should_failover_response(status_code: int, detail: str | None = None) -> bool:
    return (
        should_failover_status(status_code)
        or is_quota_exhausted_detail(detail)
        or is_payload_too_large_response(status_code, detail)
    )


def _truncate_detail(text: str, limit: int = ERROR_DETAIL_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _normalize_text(text: str) -> str:
    text = ANSI_ESCAPE_RE.sub("", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = "".join(ch if ch.isprintable() or ch in "\n\t " else " " for ch in text)
    return " ".join(text.split()).strip()


def _extract_message(value: object) -> str | None:
    if isinstance(value, str):
        cleaned = _normalize_text(value)
        return cleaned or None

    if isinstance(value, list):
        for item in value:
            message = _extract_message(item)
            if message:
                return message
        return None

    if isinstance(value, dict):
        for key in (
            "message",
            "detail",
            "error_description",
            "error",
            "result",
            "reason",
            "title",
        ):
            if key in value:
                message = _extract_message(value[key])
                if message:
                    return message
        for item in value.values():
            message = _extract_message(item)
            if message:
                return message
    return None


def _decode_error_body(body: bytes, content_type: str) -> str | None:
    if not body:
        return None

    decoded = body[:ERROR_BODY_PREVIEW_BYTES].decode("utf-8", errors="replace")
    cleaned = _normalize_text(decoded)
    if not cleaned:
        return None

    replacement_count = decoded.count("\ufffd")
    if replacement_count and replacement_count >= max(4, len(decoded) // 12):
        return None

    if "json" in content_type or cleaned[:1] in "{[":
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            payload = None
        message = _extract_message(payload) if payload is not None else None
        if message:
            return _truncate_detail(message)

    return _truncate_detail(cleaned)


def summarize_upstream_error(status: int, headers: Any, body: bytes) -> str:
    content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    detail = _decode_error_body(body, content_type)
    if detail:
        return f"upstream status {status}: {detail}"
    if content_type:
        return f"upstream status {status} with unreadable {content_type} error body"
    return f"upstream status {status} with unreadable error body"


def build_proxy_error_payload(
    app: str,
    *,
    detail: str,
    provider_id: str | None = None,
    upstream_status: int | None = None,
) -> dict[str, Any]:
    message = detail
    if provider_id:
        message = f"{provider_id}: {message}"

    if app == "claude":
        payload: dict[str, Any] = {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": message,
            },
        }
        if provider_id:
            payload["error"]["ccproxy_provider_id"] = provider_id
        if upstream_status is not None:
            payload["error"]["ccproxy_upstream_status"] = upstream_status
        return payload

    payload = {
        "error": {
            "message": message,
            "type": "api_error",
        }
    }
    if provider_id:
        payload["error"]["ccproxy_provider_id"] = provider_id
    if upstream_status is not None:
        payload["error"]["ccproxy_upstream_status"] = upstream_status
    return payload


def proxy_error_response(
    app: str,
    *,
    status: int,
    detail: str,
    provider_id: str | None = None,
    upstream_status: int | None = None,
) -> web.Response:
    return web.json_response(
        build_proxy_error_payload(
            app,
            detail=detail,
            provider_id=provider_id,
            upstream_status=upstream_status,
        ),
        status=status,
    )


def provider_attempt_order(
    config: dict[str, Any],
    health_state: dict[str, Any],
    app: str,
) -> list[tuple[str, dict[str, Any]]]:
    if not config["apps"][app]["providers"]:
        raise ValueError(f"no providers configured for {app}")

    ordered = ordered_provider_items(config, app)
    return reorder_providers_by_cooldown(ordered, health_state, app)


async def _update_health_state(
    request: web.Request,
    updater,
) -> None:
    lock: asyncio.Lock = request.app["health_lock"]
    async with lock:
        health_state = request.app["health_state"]
        updater(health_state)
        save_health_state(health_state)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def forward(request: web.Request) -> web.StreamResponse:
    routed = route_request(request.path)
    if routed is None:
        return web.json_response(
            {
                "error": "unsupported path",
                "path": request.path,
            },
            status=404,
        )

    app = routed.app
    upstream_path = routed.upstream_path
    adapter = ADAPTERS_BY_APP[app]
    config = load_config()
    health_state = request.app["health_state"]
    body = await request.read()
    session: ClientSession = request.app["session"]
    auto_failover = bool(config["proxy"].get("auto_failover", True))
    cooldown_sec = int(config["proxy"].get("cooldown_sec", 60))
    failure_threshold = int(config["proxy"].get("failure_threshold", 3))
    retry_attempts = int(config["proxy"].get("retry_attempts", 3))
    attempts = provider_attempt_order(config, health_state, app)
    last_error: str | None = None

    for attempt_index, (provider_id, provider) in enumerate(attempts):
        upstream_url = build_upstream_url(
            provider["base_url"], upstream_path, request.query_string
        )
        headers = adapter.build_headers(request.headers, provider)
        for retry_index in range(retry_attempts):
            logging.info(
                "proxy %s -> %s (%s) attempt=%s/%s",
                request.path_qs,
                upstream_url,
                provider_id,
                retry_index + 1,
                retry_attempts,
            )

            try:
                async with session.request(
                    request.method,
                    upstream_url,
                    headers=headers,
                    data=body,
                ) as upstream:
                    error_detail: str | None = None
                    quota_exhausted = False
                    if not (200 <= upstream.status < 300):
                        error_body = await upstream.read()
                        error_detail = summarize_upstream_error(
                            upstream.status,
                            upstream.headers,
                            error_body,
                        )
                        last_error = error_detail
                        quota_exhausted = is_quota_exhausted_detail(error_detail)
                        payload_too_large = is_payload_too_large_response(
                            upstream.status,
                            error_detail,
                        )
                    else:
                        payload_too_large = False

                    should_retry_same_provider = (
                        should_failover_status(upstream.status)
                        and not quota_exhausted
                        and not payload_too_large
                        and retry_index + 1 < retry_attempts
                    )
                    if should_retry_same_provider:
                        logging.warning(
                            "provider %s returned %s for %s, retrying same provider (%s/%s): %s",
                            provider_id,
                            upstream.status,
                            request.path_qs,
                            retry_index + 1,
                            retry_attempts,
                            error_detail,
                        )
                        continue

                    provider_failed = should_failover_response(upstream.status, error_detail)

                    if provider_failed:
                        effective_failure_threshold = 1 if (quota_exhausted or payload_too_large) else failure_threshold
                        await _update_health_state(
                            request,
                            lambda state: record_failure(
                                state,
                                app,
                                provider_id,
                                provider.get("name", provider_id),
                                error_detail or last_error or "",
                                cooldown_sec,
                                effective_failure_threshold,
                                now_ts=time.time(),
                            ),
                        )
                        can_failover = auto_failover and attempt_index + 1 < len(attempts)
                        if can_failover:
                            logging.warning(
                                "provider %s returned %s for %s after %s attempts, trying next provider: %s",
                                provider_id,
                                upstream.status,
                                request.path_qs,
                                retry_attempts,
                                error_detail or last_error or f"upstream status {upstream.status}",
                            )
                            break

                    if 200 <= upstream.status < 300:
                        await _update_health_state(
                            request,
                            lambda state: record_success(
                                state,
                                app,
                                provider_id,
                                provider.get("name", provider_id),
                                now_ts=time.time(),
                            ),
                        )
                        response_headers = {
                            key: value
                            for key, value in upstream.headers.items()
                            if key.lower() not in HOP_BY_HOP_HEADERS
                        }
                        response = web.StreamResponse(
                            status=upstream.status,
                            headers=response_headers,
                        )
                        await response.prepare(request)
                        async for chunk in upstream.content.iter_chunked(64 * 1024):
                            await response.write(chunk)
                        await response.write_eof()
                        return response

                    return proxy_error_response(
                        app,
                        status=upstream.status,
                        detail=error_detail or f"upstream status {upstream.status}",
                        provider_id=provider_id,
                        upstream_status=upstream.status,
                    )
            except (ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = _truncate_detail(_normalize_text(str(exc))) or str(exc)
                if retry_index + 1 < retry_attempts:
                    logging.warning(
                        "provider %s request failed for %s: %s; retrying same provider (%s/%s)",
                        provider_id,
                        request.path_qs,
                        exc,
                        retry_index + 1,
                        retry_attempts,
                    )
                    continue

                await _update_health_state(
                    request,
                    lambda state: record_failure(
                        state,
                        app,
                        provider_id,
                        provider.get("name", provider_id),
                        last_error or "",
                        cooldown_sec,
                        failure_threshold,
                        now_ts=time.time(),
                    ),
                )
                can_failover = auto_failover and attempt_index + 1 < len(attempts)
                if can_failover:
                    logging.warning(
                        "provider %s request failed for %s after %s attempts: %s; trying next provider",
                        provider_id,
                        request.path_qs,
                        retry_attempts,
                        exc,
                    )
                    break
                return web.json_response(
                    build_proxy_error_payload(
                        app,
                        detail=f"upstream request failed: {last_error}",
                        provider_id=provider_id,
                    ),
                    status=502,
                )
        else:
            continue

        continue

    return proxy_error_response(
        app,
        status=502,
        detail=last_error or "unknown upstream failure",
    )


def make_app(max_body_bytes: int) -> web.Application:
    app = web.Application(client_max_size=max_body_bytes)
    app.router.add_get("/__ccproxy/health", health)
    app.router.add_route("*", "/{tail:.*}", forward)
    return app


async def run_proxy(host: str, port: int) -> None:
    timeout = ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=180)
    config = load_config()
    max_body_bytes = proxy_max_body_bytes(config)
    app = make_app(max_body_bytes)
    app["session"] = ClientSession(timeout=timeout, auto_decompress=False)
    app["health_state"] = load_health_state()
    app["health_lock"] = asyncio.Lock()

    stop_event = asyncio.Event()

    async def _cleanup(_app: web.Application) -> None:
        await app["session"].close()

    app.on_cleanup.append(_cleanup)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logging.info(
        "ccproxy listening on http://%s:%s (max_body_mb=%s)",
        host,
        port,
        config["proxy"].get("max_body_mb", 64),
    )

    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, _request_shutdown)
        except NotImplementedError:
            signal.signal(signum, lambda *_args: _request_shutdown())

    await stop_event.wait()
    logging.info("ccproxy shutting down")
    await runner.cleanup()
