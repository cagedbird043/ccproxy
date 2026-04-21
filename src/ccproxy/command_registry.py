from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    help_en: str
    help_zh: str
    hidden: bool = False


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("init", "Create config skeleton if missing.", "初始化配置骨架。"),
    CommandSpec(
        "import-cc-switch",
        "Import codex/claude providers from ~/.cc-switch/cc-switch.db.",
        "从 ~/.cc-switch/cc-switch.db 导入 codex/claude provider。",
    ),
    CommandSpec("add", "Add a provider manually.", "手动添加 provider。"),
    CommandSpec("list", "List providers for an app.", "列出某个 app 的 provider。"),
    CommandSpec("current", "Show current provider.", "显示当前 provider。"),
    CommandSpec("show", "Show a provider config.", "显示单个 provider 配置。"),
    CommandSpec("update", "Update a provider config.", "更新 provider 配置。"),
    CommandSpec("delete", "Delete a provider.", "删除 provider。"),
    CommandSpec(
        "check",
        "Run a real non-interactive health check against a provider.",
        "对 provider 跑一次真实的非交互健康检查。",
    ),
    CommandSpec("test", "Batch test providers and print a readable summary.", "批量测试 provider，并输出人类可读汇总。"),
    CommandSpec(
        "next",
        "Rotate to the next healthy provider. Failed candidates are skipped automatically.",
        "切到下一个健康 provider，失败候选会自动跳过。",
    ),
    CommandSpec(
        "health",
        "Show runtime provider health and cooldown state recorded by the proxy.",
        "显示代理记录的运行期健康状态和冷却状态。",
    ),
    CommandSpec(
        "service",
        "Install or print a systemd service for boot-time startup.",
        "安装或打印开机自启用的 systemd 服务。",
    ),
    CommandSpec("use", "Switch current provider.", "切换当前 provider。"),
    CommandSpec("proxy", "Manage the local proxy.", "管理本地代理。"),
    CommandSpec(
        "codex",
        "Launch Codex with a temporary CODEX_HOME pointed at localhost proxy.",
        "用指向 localhost 代理的临时 CODEX_HOME 启动 Codex。",
    ),
    CommandSpec(
        "claude",
        "Launch Claude with a temporary --settings file pointed at localhost proxy.",
        "用指向 localhost 代理的临时 --settings 启动 Claude。",
    ),
    CommandSpec("completion", "Generate shell completion script.", "生成 shell 补全脚本。", hidden=True),
    CommandSpec("_complete-providers", "Complete provider ids.", "补全 provider id。", hidden=True),
    CommandSpec("_proxy-run", "Internal proxy runner.", "内部代理执行器。", hidden=True),
)


def all_command_specs() -> tuple[CommandSpec, ...]:
    return COMMAND_SPECS


def visible_command_specs() -> tuple[CommandSpec, ...]:
    return tuple(spec for spec in COMMAND_SPECS if not spec.hidden)


def visible_command_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in visible_command_specs())


def hidden_command_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in COMMAND_SPECS if spec.hidden)


def command_spec(name: str) -> CommandSpec:
    for spec in COMMAND_SPECS:
        if spec.name == name:
            return spec
    raise KeyError(name)
