from pathlib import Path

from ccproxy.adapters import build_upstream_url, route_request
from ccproxy.checks import next_provider_candidates
from ccproxy.completion import completion_provider_ids, render_completion
from ccproxy.checks import CheckResult
from ccproxy.cli import _check_detail, build_health_snapshot, build_parser, build_test_snapshot, detect_cli_lang
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
from ccproxy.proxy import make_app, provider_attempt_order, should_failover_status
from ccproxy.service import build_unit


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
                    "providers": {
                        "b": {"name": "B"},
                        "a": {"name": "A"},
                    }
                },
                "claude": {"providers": {}},
            }
        },
    )
    assert completion_provider_ids("codex") == ["a", "b"]


def test_render_bash_completion_mentions_complete_function() -> None:
    script = render_completion("bash")
    assert "complete -F _ccproxy_complete ccproxy" in script
    assert "_complete-providers" in script
    assert "show" in script
    assert "--failure-threshold" in script
    assert "test" in script


def test_render_zsh_completion_mentions_compdef() -> None:
    script = render_completion("zsh")
    assert "#compdef ccproxy" in script
    assert "_ccproxy_provider_ids_codex" in script
    assert "update provider config" in script
    assert "--retry-attempts" in script
    assert "batch test providers" in script
    assert "completion:print shell completion" not in script


def test_render_fish_completion_mentions_complete_directive() -> None:
    script = render_completion("fish")
    assert "complete -c ccproxy" in script
    assert "show update delete" in script
    assert "check test next" in script
    assert "claude completion" not in script


def test_build_parser_hides_internal_completion_commands_from_help() -> None:
    help_text = build_parser("en").format_help()
    assert "test                Batch test providers" in help_text
    assert "completion          ==SUPPRESS==" not in help_text
    assert "_complete-providers" not in help_text
    assert "_proxy-run" not in help_text


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

    def fake_run_check(app: str, selector: str | None = None) -> CheckResult:
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
