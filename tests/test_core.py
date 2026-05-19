import asyncio
import json
import subprocess
import time
import urllib.error
from pathlib import Path

from ccproxy.adapters import build_upstream_url, route_request
from ccproxy.checks import (
    CODEX_CHECK_USER_AGENT_ENV,
    CHECK_HTTP_ERROR_RETURN_CODE,
    CHECK_TIMEOUT_RETURN_CODE,
    CheckResult,
    codex_check_user_agent,
    _run_codex_check,
    _run_subprocess,
    next_provider_candidates,
)
from ccproxy.command_registry import visible_command_names
from ccproxy.completion import completion_provider_entries, completion_provider_ids, render_completion
from ccproxy.cli import (
    _check_detail,
    build_health_snapshot,
    build_parser,
    build_test_snapshot,
    classify_invocation,
    cmd_bare_non_tty_fallback,
    cmd_test,
    detect_cli_lang,
    format_provider_label,
)
from ccproxy.config import (
    default_config,
    ordered_provider_items,
    proxy_max_body_bytes,
    proxy_runtime_status,
    remove_provider,
    resolve_provider_selector,
    update_proxy_config,
)
from ccproxy.health_store import (
    default_health_state,
    record_failure,
    record_success,
    reorder_providers_by_cooldown,
)
from ccproxy.proxy import (
    build_proxy_error_payload,
    forward,
    has_ready_failover_candidate,
    is_quota_exhausted_detail,
    is_payload_too_large_detail,
    make_app,
    provider_attempt_order,
    should_failover_response,
    should_failover_status,
    summarize_upstream_error,
)
from ccproxy.read_models import build_current_provider_summary
from ccproxy.service import build_unit
from ccproxy.tui import CCProxyTUI


def test_resolve_provider_selector_prefers_id() -> None:
    providers = {
        "demo": {"name": "Shared"},
        "other": {"name": "demo"},
    }
    provider_id, provider = resolve_provider_selector(providers, "demo")
    assert provider_id == "demo"
    assert provider["name"] == "Shared"


def test_build_upstream_url_dedupes_v1() -> None:
    url = build_upstream_url("https://example.com/v1", "/v1/responses")
    assert url == "https://example.com/v1/responses"


def test_build_upstream_url_appends_v1_path() -> None:
    url = build_upstream_url("https://example.com", "/v1/messages")
    assert url == "https://example.com/v1/messages"


def test_route_request_maps_codex_aliases() -> None:
    assert route_request("/responses") == route_request("/responses").__class__(
        app="codex",
        upstream_path="/v1/responses",
    )
    assert route_request("/chat/completions") == route_request("/chat/completions").__class__(
        app="codex",
        upstream_path="/v1/chat/completions",
    )


def test_route_request_maps_claude_aliases() -> None:
    assert route_request("/v1/messages") == route_request("/v1/messages").__class__(
        app="claude",
        upstream_path="/v1/messages",
    )
    assert route_request("/claude/v1/messages") == route_request("/claude/v1/messages").__class__(
        app="claude",
        upstream_path="/v1/messages",
    )


def test_next_provider_candidates_rotate_after_current() -> None:
    data = {
        "apps": {
            "codex": {
                "current": "b",
                "providers": {
                    "a": {"name": "A", "priority": 20},
                    "b": {"name": "B", "priority": 30},
                    "c": {"name": "C", "priority": 10},
                },
            },
            "claude": {"current": None, "providers": {}},
        }
    }
    rotated = next_provider_candidates(data, "codex")
    assert [provider_id for provider_id, _provider in rotated] == ["c", "a", "b"]


def test_provider_attempt_order_keeps_current_first() -> None:
    data = {
        "proxy": {"cooldown_sec": 60},
        "apps": {
            "codex": {
                "current": "b",
                "providers": {
                    "a": {"name": "A", "priority": 10},
                    "b": {"name": "B", "priority": 30},
                    "c": {"name": "C", "priority": 20},
                },
            },
            "claude": {"current": None, "providers": {}},
        }
    }
    ordered = provider_attempt_order(data, default_health_state(), "codex")
    assert [provider_id for provider_id, _provider in ordered] == ["b", "a", "c"]


def test_ordered_provider_items_uses_priority_after_current() -> None:
    data = {
        "apps": {
            "codex": {
                "current": "b",
                "providers": {
                    "a": {"name": "A", "priority": 30},
                    "b": {"name": "B", "priority": 999},
                    "c": {"name": "C", "priority": 10},
                },
            },
            "claude": {"current": None, "providers": {}},
        }
    }
    ordered = ordered_provider_items(data, "codex")
    assert [provider_id for provider_id, _provider in ordered] == ["b", "c", "a"]


def test_should_failover_status_is_conservative() -> None:
    assert should_failover_status(429) is True
    assert should_failover_status(503) is True
    assert should_failover_status(401) is False
    assert should_failover_status(400) is False


def test_quota_exhausted_detail_detects_hard_quota_messages() -> None:
    assert is_quota_exhausted_detail("upstream status 403: quota exceeded for this key") is True
    assert is_quota_exhausted_detail("upstream status 403: 用户额度不足") is True
    assert is_quota_exhausted_detail("upstream status 403: invalid api key") is False


def test_should_failover_response_allows_quota_403_without_broadening_all_403() -> None:
    assert should_failover_response(403, "upstream status 403: quota exceeded") is True
    assert should_failover_response(403, "upstream status 403: invalid api key") is False
    assert should_failover_response(429, "upstream status 429: rate limit") is True


def test_payload_too_large_detail_detects_nginx_style_413_messages() -> None:
    assert is_payload_too_large_detail(
        "upstream status 413: 413 Request Entity Too Large nginx/1.18.0 (Ubuntu)"
    ) is True
    assert is_payload_too_large_detail(
        "upstream status 413: Payload Too Large"
    ) is True
    assert is_payload_too_large_detail("upstream status 500: sensitive_words_detected") is False


def test_should_failover_response_treats_413_as_provider_specific_body_limit() -> None:
    assert should_failover_response(
        413,
        "upstream status 413: 413 Request Entity Too Large nginx/1.18.0 (Ubuntu)",
    ) is True
    assert should_failover_response(
        413,
        "upstream status 413 with unreadable text/html error body",
    ) is True


def test_has_ready_failover_candidate_ignores_cooling_providers() -> None:
    state = default_health_state()
    record_failure(
        state,
        "codex",
        "cooling",
        "Cooling",
        "timeout",
        cooldown_sec=60,
        failure_threshold=1,
        now_ts=100.0,
    )
    candidates = [
        ("cooling", {"name": "Cooling"}),
        ("fresh", {"name": "Fresh"}),
    ]
    assert has_ready_failover_candidate(state, "codex", candidates, now_ts=110.0) is True
    assert has_ready_failover_candidate(state, "codex", candidates[:1], now_ts=110.0) is False


def test_reorder_providers_moves_cooling_provider_to_end() -> None:
    state = default_health_state()
    record_failure(
        state,
        "claude",
        "a",
        "A",
        "boom",
        cooldown_sec=60,
        failure_threshold=1,
        now_ts=100.0,
    )
    items = [("a", {"name": "A"}), ("b", {"name": "B"})]
    reordered = reorder_providers_by_cooldown(items, state, "claude", now_ts=101.0)
    assert [provider_id for provider_id, _provider in reordered] == ["b", "a"]


def test_record_success_clears_cooldown() -> None:
    state = default_health_state()
    record_failure(
        state,
        "codex",
        "a",
        "A",
        "boom",
        cooldown_sec=60,
        failure_threshold=1,
        now_ts=100.0,
    )
    record_success(state, "codex", "a", "A", now_ts=120.0)
    entry = state["apps"]["codex"]["a"]
    assert entry["cooldown_until"] is None
    assert entry["consecutive_failures"] == 0


def test_record_failure_waits_for_threshold_before_cooldown() -> None:
    state = default_health_state()
    record_failure(
        state,
        "codex",
        "a",
        "A",
        "boom1",
        cooldown_sec=60,
        failure_threshold=3,
        now_ts=100.0,
    )
    record_failure(
        state,
        "codex",
        "a",
        "A",
        "boom2",
        cooldown_sec=60,
        failure_threshold=3,
        now_ts=110.0,
    )
    assert state["apps"]["codex"]["a"]["cooldown_until"] is None

    record_failure(
        state,
        "codex",
        "a",
        "A",
        "boom3",
        cooldown_sec=60,
        failure_threshold=3,
        now_ts=120.0,
    )
    assert state["apps"]["codex"]["a"]["cooldown_until"] == 180.0


def test_update_proxy_config_changes_cooldown_and_failover() -> None:
    data = default_config()
    changed = update_proxy_config(
        data,
        cooldown_sec=120,
        auto_failover=False,
        failure_threshold=4,
        retry_attempts=5,
        max_body_mb=96,
    )
    assert changed == {
        "auto_failover": False,
        "cooldown_sec": 120,
        "failure_threshold": 4,
        "retry_attempts": 5,
        "max_body_mb": 96,
    }
    assert data["proxy"]["cooldown_sec"] == 120
    assert data["proxy"]["auto_failover"] is False
    assert data["proxy"]["failure_threshold"] == 4
    assert data["proxy"]["retry_attempts"] == 5
    assert data["proxy"]["max_body_mb"] == 96


def test_proxy_max_body_bytes_uses_mebibytes() -> None:
    data = default_config()
    data["proxy"]["max_body_mb"] = 64
    assert proxy_max_body_bytes(data) == 64 * 1024 * 1024


def test_make_app_uses_configured_client_max_size() -> None:
    app = make_app(8 * 1024 * 1024)
    assert app._client_max_size == 8 * 1024 * 1024


def test_summarize_upstream_error_extracts_clean_json_message() -> None:
    detail = summarize_upstream_error(
        403,
        {"Content-Type": "application/json; charset=utf-8"},
        b'{"error":{"message":"quota low \\u001b[31mboom\\u001b[0m"}}',
    )
    assert detail == "upstream status 403: quota low boom"


def test_summarize_upstream_error_hides_unreadable_binary_body() -> None:
    detail = summarize_upstream_error(
        502,
        {"Content-Type": "text/html"},
        b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x8b\x88\x8c",
    )
    assert detail == "upstream status 502 with unreadable text/html error body"


def test_build_proxy_error_payload_matches_claude_shape() -> None:
    payload = build_proxy_error_payload(
        "claude",
        detail="upstream status 429: quota low",
        provider_id="backup-claude",
        upstream_status=429,
    )
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "api_error"
    assert payload["error"]["message"] == "backup-claude: upstream status 429: quota low"
    assert payload["error"]["ccproxy_provider_id"] == "backup-claude"
    assert payload["error"]["ccproxy_upstream_status"] == 429


def test_detect_cli_lang_prefers_locale_environment() -> None:
    lang = detect_cli_lang({"LANG": "zh_CN.UTF-8"})
    assert lang == "zh"


def test_detect_cli_lang_prefers_lc_all_over_lang() -> None:
    lang = detect_cli_lang({"LC_ALL": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"})
    assert lang == "en"


def test_detect_cli_lang_prefers_lc_messages_over_lang() -> None:
    lang = detect_cli_lang({"LC_MESSAGES": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"})
    assert lang == "en"


def test_completion_provider_ids_reads_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "ccproxy.completion.load_config",
        lambda: {
            "apps": {
                "codex": {
                    "current": "b",
                    "providers": {
                        "b": {"name": "B"},
                        "a": {"name": "A"},
                    }
                },
                "claude": {"providers": {}},
            }
        },
    )
    assert completion_provider_ids("codex") == ["b", "a"]


def test_completion_provider_entries_include_human_readable_descriptions(monkeypatch) -> None:
    monkeypatch.setattr(
        "ccproxy.completion.load_config",
        lambda: {
            "apps": {
                "codex": {
                    "current": "b",
                    "providers": {
                        "b": {"name": "YesCodex", "priority": 20},
                        "a": {"name": "CodeZ", "priority": 30},
                    },
                },
                "claude": {"current": None, "providers": {}},
            }
        },
    )
    assert completion_provider_entries("codex") == [
        ("b", "YesCodex [current]"),
        ("a", "CodeZ"),
    ]


def test_render_bash_completion_mentions_complete_function() -> None:
    script = render_completion("bash")
    assert "complete -F _ccproxy_complete ccproxy" in script
    assert "_complete-providers" in script
    assert "cut -f1" in script
    assert "show" in script
    assert "--failure-threshold" in script
    assert "--timeout-sec" in script
    assert "--json" in script
    assert "test" in script


def test_render_zsh_completion_mentions_compdef() -> None:
    script = render_completion("zsh")
    assert "#compdef ccproxy" in script
    assert "_ccproxy_provider_ids_codex" in script
    assert "read -r value desc" in script
    assert "_complete-providers" in script
    assert "update provider config" in script
    assert "--retry-attempts" in script
    assert "--timeout-sec" in script
    assert "batch test providers" in script
    assert "completion:print shell completion" not in script


def test_render_fish_completion_mentions_complete_directive() -> None:
    script = render_completion("fish")
    assert "complete -c ccproxy" in script
    assert "_complete-providers $app" in script
    assert "show update delete" in script
    assert "check test next" in script
    assert "timeout-sec" in script
    assert "claude completion" not in script


def test_build_parser_hides_internal_completion_commands_from_help() -> None:
    parser = build_parser("en")
    help_text = parser.format_help()
    assert "test                Batch test providers" in help_text
    assert parser.parse_args(["test", "codex", "--timeout-sec", "5"]).timeout_sec == 5
    assert parser.parse_args(["--json"]).dashboard_json is True
    assert "completion          ==SUPPRESS==" not in help_text
    assert "_complete-providers" not in help_text
    assert "_proxy-run" not in help_text


def test_visible_command_registry_contains_expected_public_commands() -> None:
    assert visible_command_names() == (
        "init",
        "import-cc-switch",
        "add",
        "list",
        "current",
        "show",
        "update",
        "delete",
        "check",
        "test",
        "next",
        "health",
        "service",
        "use",
        "proxy",
        "codex",
        "claude",
    )


class _FakeTTY:
    def __init__(self, is_tty: bool):
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_classify_invocation_prefers_tui_for_bare_tty() -> None:
    args = build_parser("en").parse_args([])
    route = classify_invocation(args, _FakeTTY(True), _FakeTTY(True), {"TERM": "xterm-256color"})
    assert route == "tui"


def test_classify_invocation_prefers_dashboard_json_for_top_level_flag() -> None:
    args = build_parser("en").parse_args(["--json"])
    route = classify_invocation(args, _FakeTTY(True), _FakeTTY(True), {"TERM": "xterm-256color"})
    assert route == "dashboard-json"


def test_classify_invocation_falls_back_for_bare_non_tty() -> None:
    args = build_parser("en").parse_args([])
    route = classify_invocation(args, _FakeTTY(False), _FakeTTY(False), {"TERM": "xterm-256color"})
    assert route == "fallback"


def test_bare_non_tty_fallback_prints_guidance(capsys) -> None:
    exit_code = cmd_bare_non_tty_fallback()
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "interactive TUI requires a real terminal" in captured.err


def test_tui_display_width_handles_wide_characters() -> None:
    tui = CCProxyTUI()
    assert tui.display_width("Press q to quit") == len("Press q to quit")
    assert tui.display_width("按 q 退出") == 9


def test_tui_truncate_clips_by_terminal_cells_not_codepoints() -> None:
    tui = CCProxyTUI()
    text = "Tab 切 app  ↑↓/jk 移动"
    clipped = tui.truncate(text, 10)
    assert tui.display_width(clipped) <= 10
    assert clipped.endswith("…")


def test_tui_background_test_progress_updates_status() -> None:
    tui = CCProxyTUI()
    tui.background_job = {"kind": "test"}
    tui.handle_background_event(
        {
            "type": "progress",
            "kind": "test-row",
            "app": "codex",
            "index": 1,
            "total": 3,
            "row": {"provider_name": "YesCodex", "success": True},
        }
    )
    assert "1/3" in tui.status_message
    assert "YesCodex" in tui.status_message


def test_tui_background_test_complete_queues_popup_and_clears_busy(monkeypatch) -> None:
    tui = CCProxyTUI()
    tui.background_job = {"kind": "test"}
    monkeypatch.setattr(tui, "refresh_data", lambda: None)
    tui.handle_background_event(
        {
            "type": "complete",
            "kind": "test",
            "payload": {
                "app": "codex",
                "rows": [
                    {
                        "provider_id": "demo",
                        "duration_sec": 1.2,
                        "success": True,
                        "detail": None,
                    }
                ],
                "summary": {"ok": 1, "fail": 0, "total": 1},
            },
        }
    )
    assert tui.background_job is None
    assert tui.pending_popup is not None
    assert "ok=1" in tui.status_message


def test_current_provider_summary_distinguishes_selected_from_next_request(monkeypatch) -> None:
    data = {
        "proxy": {"auto_failover": True},
        "apps": {
            "codex": {
                "current": "a",
                "providers": {
                    "a": {"name": "YesCodex", "priority": 10, "base_url": "https://yes.example"},
                    "b": {"name": "CodeZ", "priority": 20, "base_url": "https://codez.example"},
                },
            },
            "claude": {"current": None, "providers": {}},
        },
    }
    state = default_health_state()
    state["apps"]["codex"]["a"] = {
        "provider_name": "YesCodex",
        "total_successes": 10,
        "total_failures": 1,
        "consecutive_failures": 3,
        "last_success_at": time.time() - 120,
        "last_failure_at": time.time() - 5,
        "last_error": "boom",
        "cooldown_until": time.time() + 60,
    }

    monkeypatch.setattr("ccproxy.read_models.load_config", lambda: data)
    monkeypatch.setattr("ccproxy.read_models.load_health_state", lambda: state)

    summary = build_current_provider_summary("codex")
    assert summary["selected_provider_id"] == "a"
    assert summary["effective_provider_id"] == "b"
    assert summary["effective_matches_selected"] is False
    assert "cooling down" in summary["effective_reason"]


def test_health_snapshot_marks_next_request_provider(monkeypatch) -> None:
    data = {
        "proxy": {"auto_failover": True},
        "apps": {
            "codex": {
                "current": "a",
                "providers": {
                    "a": {"name": "YesCodex", "priority": 10, "base_url": "https://yes.example"},
                    "b": {"name": "CodeZ", "priority": 20, "base_url": "https://codez.example"},
                },
            },
            "claude": {"current": None, "providers": {}},
        },
    }
    state = default_health_state()
    state["apps"]["codex"]["a"] = {
        "provider_name": "YesCodex",
        "total_successes": 10,
        "total_failures": 1,
        "consecutive_failures": 3,
        "last_success_at": time.time() - 120,
        "last_failure_at": time.time() - 5,
        "last_error": "boom",
        "cooldown_until": time.time() + 60,
    }

    monkeypatch.setattr("ccproxy.read_models.load_config", lambda: data)
    monkeypatch.setattr("ccproxy.read_models.load_health_state", lambda: state)
    monkeypatch.setattr("ccproxy.read_models.health_state_path", lambda: Path("/tmp/mock-health.json"))

    snapshot = build_health_snapshot("codex")
    rows = snapshot["apps"]["codex"]
    yes_row = next(row for row in rows if row["provider_id"] == "a")
    codez_row = next(row for row in rows if row["provider_id"] == "b")
    assert yes_row["current"] is True
    assert yes_row["effective"] is False
    assert codez_row["effective"] is True


def test_proxy_runtime_status_uses_health_probe_without_pid(monkeypatch) -> None:
    data = default_config()
    monkeypatch.setattr("ccproxy.config.remove_stale_pid_file", lambda: None)
    monkeypatch.setattr("ccproxy.config.pid_from_file", lambda: None)
    monkeypatch.setattr("ccproxy.config.is_pid_running", lambda pid: False)
    monkeypatch.setattr("ccproxy.config.proxy_health_ok", lambda host, port, timeout_sec=0.5: True)
    monkeypatch.setattr("ccproxy.config.systemd_service_scope", lambda: "system")

    status = proxy_runtime_status(data)
    assert status["running"] is True
    assert status["healthy"] is True
    assert status["manager"] == "systemd-system"
    assert status["failure_threshold"] == 3
    assert status["retry_attempts"] == 3


def test_build_health_snapshot_has_file_and_rows() -> None:
    snapshot = build_health_snapshot("claude")
    assert "health_state_file" in snapshot
    assert "apps" in snapshot
    assert "claude" in snapshot["apps"]


def test_build_test_snapshot_collects_results(monkeypatch) -> None:
    monkeypatch.setattr(
        "ccproxy.cli.load_config",
        lambda: {
            "apps": {
                "codex": {
                    "current": "b",
                    "providers": {
                        "a": {"name": "A", "priority": 20},
                        "b": {"name": "B", "priority": 10},
                    },
                },
                "claude": {"current": None, "providers": {}},
            }
        },
    )

    def fake_run_check(
        app: str,
        selector: str | None = None,
        timeout_sec: float | None = None,
    ) -> CheckResult:
        provider_id = selector or "missing"
        return CheckResult(
            app=app,
            provider_id=provider_id,
            provider_name=provider_id.upper(),
            success=(provider_id == "b"),
            duration_sec=1.25,
            summary="healthy" if provider_id == "b" else "failed",
            stdout=(
                "Reading additional input from stdin...\nCCPROXY_CHECK_OK\n"
                if provider_id == "b"
                else "\n".join(
                    [
                        "Reading additional input from stdin...",
                        "OpenAI Codex v0.118.0 (research preview)",
                        "ERROR: Reconnecting... 1/5",
                        "ERROR: unexpected status 403 Forbidden: balance low",
                    ]
                )
            ),
            stderr="",
            returncode=0 if provider_id == "b" else 1,
        )

    monkeypatch.setattr("ccproxy.cli.run_check", fake_run_check)

    snapshot = build_test_snapshot("codex")
    assert snapshot["summary"] == {"ok": 1, "fail": 1, "total": 2}
    rows = snapshot["apps"]["codex"]
    assert rows[0]["provider_id"] == "b"
    assert rows[0]["current"] is True
    assert rows[0]["success"] is True
    assert rows[0]["detail"] is None
    assert rows[1]["provider_id"] == "a"
    assert rows[1]["detail"] == "ERROR: unexpected status 403 Forbidden: balance low"


def test_run_subprocess_timeout_returns_human_readable_error(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=3,
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr("ccproxy.checks.subprocess.run", fake_run)

    returncode, stdout, stderr, timed_out = _run_subprocess(["codex", "exec"], timeout_sec=3)

    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert returncode == CHECK_TIMEOUT_RETURN_CODE
    assert stdout == "partial stdout"
    assert "partial stderr" in stderr
    assert "ERROR: check timed out after 3s" in stderr
    assert timed_out is True


def request_header(captured: dict[str, object], name: str) -> str | None:
    headers = captured["headers"]
    assert isinstance(headers, dict)
    for key, value in headers.items():
        if key.casefold() == name.casefold():
            return str(value)
    return None


def test_codex_check_user_agent_prefers_env_override(monkeypatch) -> None:
    monkeypatch.setenv(CODEX_CHECK_USER_AGENT_ENV, "custom-codex-check/1")
    assert codex_check_user_agent() == "custom-codex-check/1"


def test_codex_check_user_agent_uses_codex_tui_shape(monkeypatch) -> None:
    monkeypatch.delenv(CODEX_CHECK_USER_AGENT_ENV, raising=False)
    monkeypatch.setenv("KONSOLE_VERSION", "260401")
    monkeypatch.setattr("ccproxy.checks._detect_codex_version", lambda: "0.131.0")
    monkeypatch.setattr("ccproxy.checks._os_release_label", lambda: "Arch Linux Rolling Release")

    assert (
        codex_check_user_agent()
        == "codex-tui/0.131.0 (Arch Linux Rolling Release; x86_64) Konsole/26.04.1 (codex-tui; 0.131.0)"
    )


def test_run_codex_check_accepts_non_stream_responses_marker(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"output_text": "CCPROXY_CHECK_OK"}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode())
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr("ccproxy.checks.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv(
        CODEX_CHECK_USER_AGENT_ENV,
        "codex-tui/0.131.0 (Arch Linux Rolling Release; x86_64) Konsole/26.04.1 (codex-tui; 0.131.0)",
    )

    returncode, stdout, stderr, timed_out = _run_codex_check(
        {"base_url": "https://fast.poloai.top", "api_key": "sk-test", "model": "gpt-5.4"},
        timeout_sec=7,
    )

    assert captured["url"] == "https://fast.poloai.top/v1/responses"
    assert captured["timeout"] == 7
    assert captured["body"]["stream"] is False
    assert request_header(captured, "User-agent") == (
        "codex-tui/0.131.0 (Arch Linux Rolling Release; x86_64) Konsole/26.04.1 (codex-tui; 0.131.0)"
    )
    assert stdout == "CCPROXY_CHECK_OK"
    assert stderr == ""
    assert returncode == 0
    assert timed_out is False


def test_run_codex_check_keeps_usage_limited_provider_failed(monkeypatch) -> None:
    class FakeHTTPError(urllib.error.HTTPError):
        pass

    error = FakeHTTPError(
        url="https://mx.free.codesonline.dev/v1/responses",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=None,
    )
    error.read = lambda: b'{"error":{"message":"monthly usage limit exceeded"}}'

    monkeypatch.setattr(
        "ccproxy.checks.urllib.request.urlopen",
        lambda _request, timeout: (_ for _ in ()).throw(error),
    )

    returncode, stdout, stderr, timed_out = _run_codex_check(
        {"base_url": "https://mx.free.codesonline.dev", "api_key": "sk-test", "model": "gpt-5.4"},
        timeout_sec=7,
    )

    assert returncode == CHECK_HTTP_ERROR_RETURN_CODE
    assert stdout == ""
    assert "429 Too Many Requests" in stderr
    assert "monthly usage limit exceeded" in stderr
    assert timed_out is False


def test_cmd_test_text_mode_streams_rows_without_building_snapshot(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "ccproxy.cli.build_test_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("snapshot path should not be used")),
    )
    monkeypatch.setattr(
        "ccproxy.cli.load_config",
        lambda: {
            "apps": {
                "codex": {
                    "current": "b",
                    "providers": {
                        "a": {"name": "A", "priority": 20},
                        "b": {"name": "B", "priority": 10},
                    },
                },
                "claude": {"current": None, "providers": {}},
            }
        },
    )

    seen_timeouts: list[float | None] = []

    def fake_run_check(
        app: str,
        selector: str | None = None,
        timeout_sec: float | None = None,
    ) -> CheckResult:
        seen_timeouts.append(timeout_sec)
        provider_id = selector or "missing"
        return CheckResult(
            app=app,
            provider_id=provider_id,
            provider_name=provider_id.upper(),
            success=(provider_id == "b"),
            duration_sec=0.5 if provider_id == "b" else 1.0,
            summary="healthy" if provider_id == "b" else "failed",
            stdout="" if provider_id == "b" else "ERROR: boom",
            stderr="",
            returncode=0 if provider_id == "b" else 1,
        )

    monkeypatch.setattr("ccproxy.cli.run_check", fake_run_check)

    exit_code = cmd_test("codex", json_mode=False, timeout_sec=7)
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert seen_timeouts == [7, 7]
    assert "[codex]" in captured
    assert "* [OK]" in captured
    assert "[FAIL]" in captured
    assert "ERROR: boom" in captured
    assert "summary: ok=1 fail=1 total=2" in captured


def test_forward_normalizes_unreadable_upstream_error_for_codex(monkeypatch) -> None:
    monkeypatch.setattr(
        "ccproxy.proxy.load_config",
        lambda: {
            "proxy": {
                "auto_failover": False,
                "cooldown_sec": 60,
                "failure_threshold": 3,
                "retry_attempts": 1,
            },
            "apps": {
                "codex": {
                    "current": "bad",
                    "providers": {
                        "bad": {"name": "Bad", "base_url": "https://example.com", "api_key": "k"},
                    },
                },
                "claude": {"current": None, "providers": {}},
            },
        },
    )

    class FakeContent:
        async def iter_chunked(self, _size: int):
            if False:
                yield b""

    class FakeUpstream:
        def __init__(self):
            self.status = 403
            self.headers = {"Content-Type": "text/html"}
            self.content = FakeContent()

        async def read(self):
            return b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x8b\x88\x8c"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        def request(self, *args, **kwargs):
            return FakeUpstream()

    class FakeRequest:
        def __init__(self):
            self.path = "/responses"
            self.path_qs = "/responses"
            self.query_string = ""
            self.method = "POST"
            self.headers = {}
            self.app = {
                "session": FakeSession(),
                "health_state": default_health_state(),
                "health_lock": asyncio.Lock(),
            }

        async def read(self):
            return b"{}"

    response = asyncio.run(forward(FakeRequest()))
    assert response.status == 403
    payload = json.loads(response.body.decode())
    assert payload["error"]["type"] == "api_error"
    assert payload["error"]["ccproxy_provider_id"] == "bad"
    assert payload["error"]["message"] == "bad: upstream status 403 with unreadable text/html error body"


def test_forward_fails_over_on_quota_exceeded_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "ccproxy.proxy.load_config",
        lambda: {
            "proxy": {
                "auto_failover": True,
                "cooldown_sec": 60,
                "failure_threshold": 3,
                "retry_attempts": 3,
            },
            "apps": {
                "codex": {
                    "current": "bad",
                    "providers": {
                        "bad": {"name": "CodeZ", "base_url": "https://bad.example", "api_key": "k"},
                        "good": {"name": "YesCodex", "base_url": "https://good.example", "api_key": "k"},
                    },
                },
                "claude": {"current": None, "providers": {}},
            },
        },
    )

    class FakeContent:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def iter_chunked(self, _size: int):
            for chunk in self._chunks:
                yield chunk

    class FakeUpstream:
        def __init__(self, status: int, headers: dict[str, str], body: bytes = b"", chunks: list[bytes] | None = None):
            self.status = status
            self.headers = headers
            self._body = body
            self.content = FakeContent(chunks or [])

        async def read(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeStreamResponse:
        def __init__(self, *, status: int, headers: dict[str, str]):
            self.status = status
            self.headers = headers
            self.body = b""

        async def prepare(self, _request):
            return self

        async def write(self, chunk: bytes):
            self.body += chunk

        async def write_eof(self):
            return None

    class FakeSession:
        def request(self, _method, url, **_kwargs):
            if "bad.example" in url:
                return FakeUpstream(
                    403,
                    {"Content-Type": "application/json"},
                    b'{"error":{"message":"quota exceeded"}}',
                )
            return FakeUpstream(
                200,
                {"Content-Type": "application/json"},
                chunks=[b'{"ok":true}'],
            )

    class FakeRequest:
        def __init__(self):
            self.path = "/responses"
            self.path_qs = "/responses"
            self.query_string = ""
            self.method = "POST"
            self.headers = {}
            self.app = {
                "session": FakeSession(),
                "health_state": default_health_state(),
                "health_lock": asyncio.Lock(),
            }

        async def read(self):
            return b"{}"

    monkeypatch.setattr("ccproxy.proxy.web.StreamResponse", FakeStreamResponse)

    response = asyncio.run(forward(FakeRequest()))
    assert response.status == 200
    assert response.body == b'{"ok":true}'


def test_forward_quota_exceeded_enters_cooldown_immediately(monkeypatch) -> None:
    health_state = default_health_state()
    monkeypatch.setattr(
        "ccproxy.proxy.load_config",
        lambda: {
            "proxy": {
                "auto_failover": False,
                "cooldown_sec": 60,
                "failure_threshold": 3,
                "retry_attempts": 3,
            },
            "apps": {
                "codex": {
                    "current": "bad",
                    "providers": {
                        "bad": {"name": "CodeZ", "base_url": "https://bad.example", "api_key": "k"},
                    },
                },
                "claude": {"current": None, "providers": {}},
            },
        },
    )

    class FakeContent:
        async def iter_chunked(self, _size: int):
            if False:
                yield b""

    class FakeUpstream:
        def __init__(self):
            self.status = 403
            self.headers = {"Content-Type": "application/json"}
            self.content = FakeContent()

        async def read(self):
            return b'{"error":{"message":"quota exceeded"}}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        def request(self, *args, **kwargs):
            return FakeUpstream()

    class FakeRequest:
        def __init__(self):
            self.path = "/responses"
            self.path_qs = "/responses"
            self.query_string = ""
            self.method = "POST"
            self.headers = {}
            self.app = {
                "session": FakeSession(),
                "health_state": health_state,
                "health_lock": asyncio.Lock(),
            }

        async def read(self):
            return b"{}"

    response = asyncio.run(forward(FakeRequest()))
    assert response.status == 403
    entry = health_state["apps"]["codex"]["bad"]
    assert entry["consecutive_failures"] == 1
    assert entry["cooldown_until"] is not None


def test_forward_payload_too_large_fails_over_without_same_provider_retries(monkeypatch) -> None:
    monkeypatch.setattr(
        "ccproxy.proxy.load_config",
        lambda: {
            "proxy": {
                "auto_failover": True,
                "cooldown_sec": 60,
                "failure_threshold": 3,
                "retry_attempts": 3,
            },
            "apps": {
                "codex": {
                    "current": "bad",
                    "providers": {
                        "bad": {"name": "IkunCode", "base_url": "https://bad.example", "api_key": "k"},
                        "good": {"name": "CodesOnline", "base_url": "https://good.example", "api_key": "k"},
                    },
                },
                "claude": {"current": None, "providers": {}},
            },
        },
    )

    class FakeContent:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def iter_chunked(self, _size: int):
            for chunk in self._chunks:
                yield chunk

    class FakeUpstream:
        def __init__(self, status: int, headers: dict[str, str], body: bytes = b"", chunks: list[bytes] | None = None):
            self.status = status
            self.headers = headers
            self._body = body
            self.content = FakeContent(chunks or [])

        async def read(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeStreamResponse:
        def __init__(self, *, status: int, headers: dict[str, str]):
            self.status = status
            self.headers = headers
            self.body = b""

        async def prepare(self, _request):
            return self

        async def write(self, chunk: bytes):
            self.body += chunk

        async def write_eof(self):
            return None

    seen_urls: list[str] = []

    class FakeSession:
        def request(self, _method, url, **_kwargs):
            seen_urls.append(url)
            if "bad.example" in url:
                return FakeUpstream(
                    413,
                    {"Content-Type": "text/html"},
                    b"<html><title>413 Request Entity Too Large</title><body>413 Request Entity Too Large nginx/1.18.0 (Ubuntu)</body></html>",
                )
            return FakeUpstream(
                200,
                {"Content-Type": "application/json"},
                chunks=[b'{"ok":true}'],
            )

    class FakeRequest:
        def __init__(self):
            self.path = "/responses"
            self.path_qs = "/responses"
            self.query_string = ""
            self.method = "POST"
            self.headers = {}
            self.app = {
                "session": FakeSession(),
                "health_state": default_health_state(),
                "health_lock": asyncio.Lock(),
            }

        async def read(self):
            return b"{}"

    monkeypatch.setattr("ccproxy.proxy.web.StreamResponse", FakeStreamResponse)

    response = asyncio.run(forward(FakeRequest()))
    assert response.status == 200
    assert response.body == b'{"ok":true}'
    assert len([url for url in seen_urls if "bad.example" in url]) == 1
    assert any("good.example" in url for url in seen_urls)


def test_forward_payload_too_large_does_not_wait_on_only_cooling_fallbacks(monkeypatch) -> None:
    health_state = default_health_state()
    now_ts = time.time()
    record_failure(
        health_state,
        "codex",
        "good",
        "CodesOnline",
        "timeout",
        cooldown_sec=60,
        failure_threshold=1,
        now_ts=now_ts,
    )
    monkeypatch.setattr(
        "ccproxy.proxy.load_config",
        lambda: {
            "proxy": {
                "auto_failover": True,
                "cooldown_sec": 60,
                "failure_threshold": 3,
                "retry_attempts": 3,
            },
            "apps": {
                "codex": {
                    "current": "bad",
                    "providers": {
                        "bad": {"name": "IkunCode", "base_url": "https://bad.example", "api_key": "k"},
                        "good": {"name": "CodesOnline", "base_url": "https://good.example", "api_key": "k"},
                    },
                },
                "claude": {"current": None, "providers": {}},
            },
        },
    )

    class FakeContent:
        async def iter_chunked(self, _size: int):
            if False:
                yield b""

    class FakeUpstream:
        def __init__(self, status: int, headers: dict[str, str], body: bytes):
            self.status = status
            self.headers = headers
            self._body = body
            self.content = FakeContent()

        async def read(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    seen_urls: list[str] = []

    class FakeSession:
        def request(self, _method, url, **_kwargs):
            seen_urls.append(url)
            if "good.example" in url:
                raise AssertionError("cooling fallback should not be tried for deterministic 413")
            return FakeUpstream(
                413,
                {"Content-Type": "text/html"},
                b"<html><body>413 Request Entity Too Large nginx/1.18.0 (Ubuntu)</body></html>",
            )

    class FakeRequest:
        def __init__(self):
            self.path = "/responses"
            self.path_qs = "/responses"
            self.query_string = ""
            self.method = "POST"
            self.headers = {}
            self.app = {
                "session": FakeSession(),
                "health_state": health_state,
                "health_lock": asyncio.Lock(),
            }

        async def read(self):
            return b"{}"

    response = asyncio.run(forward(FakeRequest()))
    assert response.status == 413
    payload = json.loads(response.body.decode())
    assert "Request Entity Too Large" in payload["error"]["message"]
    assert len(seen_urls) == 1


def test_check_detail_prefers_result_field_from_json_error() -> None:
    result = CheckResult(
        app="claude",
        provider_id="x",
        provider_name="Claude X",
        success=False,
        duration_sec=1.0,
        summary="failed",
        stdout='{"result":"Failed to authenticate. API Error: 403 {\\"error\\":{\\"message\\":\\"quota low\\"}}"}\n',
        stderr="",
        returncode=1,
    )
    assert _check_detail(result) == 'Failed to authenticate. API Error: 403 {"error":{"message":"quota low"}}'


def test_format_provider_label_prefers_human_name() -> None:
    assert format_provider_label("9d03", "YesCodex") == "YesCodex (9d03)"
    assert format_provider_label("default", "default") == "default"


def test_remove_provider_promotes_best_remaining() -> None:
    data = {
        "apps": {
            "codex": {
                "current": "b",
                "providers": {
                    "a": {"name": "A", "priority": 10},
                    "b": {"name": "B", "priority": 30},
                    "c": {"name": "C", "priority": 20},
                },
            },
            "claude": {"current": None, "providers": {}},
        }
    }
    removed_id, _removed = remove_provider(data, "codex", "b")
    assert removed_id == "b"
    assert data["apps"]["codex"]["current"] == "a"
    assert "b" not in data["apps"]["codex"]["providers"]


def test_build_user_unit_contains_user_manager_target() -> None:
    unit = build_unit(
        "user",
        Path("/home/demo/.local/bin/ccproxy"),
        "demo",
        home=Path("/home/demo"),
    )
    assert "WantedBy=default.target" in unit
    assert "ExecStart=/home/demo/.local/bin/ccproxy proxy run" in unit
    assert "User=demo" not in unit


def test_build_system_unit_contains_explicit_user() -> None:
    unit = build_unit(
        "system",
        Path("/home/demo/.local/bin/ccproxy"),
        "demo",
        home=Path("/home/demo"),
    )
    assert "WantedBy=multi-user.target" in unit
    assert "User=demo" in unit
