from __future__ import annotations

import asyncio
import logging
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


def should_failover_status(status_code: int) -> bool:
    return status_code in FAILOVER_STATUS_CODES


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
                    should_retry_same_provider = (
                        should_failover_status(upstream.status)
                        and retry_index + 1 < retry_attempts
                    )
                    if should_retry_same_provider:
                        logging.warning(
                            "provider %s returned %s for %s, retrying same provider (%s/%s)",
                            provider_id,
                            upstream.status,
                            request.path_qs,
                            retry_index + 1,
                            retry_attempts,
                        )
                        await upstream.read()
                        continue

                    provider_failed = should_failover_status(upstream.status)
                    if provider_failed:
                        last_error = f"upstream status {upstream.status}"
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
                                "provider %s returned %s for %s after %s attempts, trying next provider",
                                provider_id,
                                upstream.status,
                                request.path_qs,
                                retry_attempts,
                            )
                            await upstream.read()
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
            except (ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = str(exc)
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
                    {
                        "error": "upstream request failed",
                        "provider_id": provider_id,
                        "detail": str(exc),
                    },
                    status=502,
                )
        else:
            continue

        continue

    return web.json_response(
        {
            "error": "all providers failed",
            "detail": last_error or "unknown upstream failure",
        },
        status=502,
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
