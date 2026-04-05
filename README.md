# ccproxy

`ccproxy` 是一个面向 `Codex` 和 `Claude` CLI 的独立开源工具。

它解决的核心问题只有一个：

- 你在远程终端里长期跑 `codex` / `claude`
- 上游 API 提供商经常炸
- GUI 不可用，手改配置文件很痛苦
- 已经跑起来的前台进程又不想因为切 provider 被迫重启

`ccproxy` 的做法不是继续改全局 live config，而是：

1. 起一个本地 `localhost` proxy
2. 用临时配置启动 `codex` / `claude`
3. 前台 CLI 进程始终只连本地 proxy
4. 真正的 provider 选择在 proxy 后面热切

这样，`ccproxy use codex <provider>` 或 `ccproxy use claude <provider>` 会在下一次请求时立即生效，不需要重启当前前台会话。

## 当前范围

MVP 只支持：

- `Codex`
- `Claude`
- provider 导入、列出、切换
- proxy 内自动故障转移
- 本地 proxy 前台/后台运行
- `codex` / `claude` 临时启动器
- 从现有 `cc-switch` 数据库导入 provider

## 安装

你不需要每次都写 `uv run`。

推荐直接执行仓库自带安装脚本。它会创建一个独立虚拟环境，并把 `ccproxy` 链接到 `~/.local/bin/ccproxy`。

```bash
cd ccproxy
./install.sh
ccproxy --help
```

如果你在本地开发这个仓库，希望改代码后命令立即生效：

```bash
cd ccproxy
CCPROXY_EDITABLE=1 ./install.sh
ccproxy --help
```

卸载：

```bash
./uninstall.sh
```

如果你本来就喜欢 `uv` 或 `pipx`，也可以：

```bash
uv tool install .
pipx install .
```

## 快速开始

### 1. 从现有 cc-switch 导入 provider

```bash
ccproxy import-cc-switch
```

### 2. 看看当前有哪些 provider

```bash
ccproxy list codex
ccproxy list claude
ccproxy check codex
ccproxy check claude
```

### 3. 启动本地 proxy

```bash
ccproxy proxy up
ccproxy proxy status
```

默认会开启自动故障转移。当前 provider 如果在请求时连接失败，或者返回 `429/5xx`，proxy 会自动尝试下一个 provider，并把 current 更新为新的健康 provider。

### 4. 用代理模式启动 Codex / Claude

```bash
ccproxy codex
ccproxy claude
```

如果你想在启动前切到指定 provider：

```bash
ccproxy codex --provider yescodex
ccproxy claude --provider ikun-1m
```

### 5. 运行中热切 provider

在另一个终端里：

```bash
ccproxy use codex yescodex
ccproxy use codex backup-provider

ccproxy use claude ikun-1m
ccproxy use claude fallback-claude

ccproxy next codex
ccproxy next claude
```

当前会话不会被重启。下一次发起请求时会走新 provider。

## 命令

```bash
ccproxy init
ccproxy import-cc-switch

ccproxy add codex my-codex --base-url https://example.com/v1 --api-key sk-xxx --model gpt-5.4
ccproxy add claude my-claude --base-url https://example.com --api-key sk-xxx --auth-mode bearer

ccproxy list codex
ccproxy current codex
ccproxy use codex my-codex
ccproxy check codex
ccproxy next codex

ccproxy proxy up
ccproxy proxy down
ccproxy proxy status
ccproxy proxy logs

ccproxy codex
ccproxy codex --provider my-codex -- --model gpt-5.4

ccproxy claude
ccproxy claude --provider my-claude -- --resume
ccproxy check claude
ccproxy next claude
```

## 配置

配置文件默认放在：

- `~/.config/ccproxy/config.json`
- `~/.local/state/ccproxy/proxy.pid`
- `~/.local/state/ccproxy/proxy.log`

当前 provider 状态也保存在 `config.json` 中，proxy 每次请求都会重新读取，因此切换是热的。

## 设计边界

- `ccproxy` 不接管你真实的 `~/.codex` / `~/.claude` 全局配置
- `ccproxy codex` 会用一个临时 `CODEX_HOME`
- `ccproxy claude` 会用 `claude --settings <tempfile>`
- 这样退出后不会把你的全局 live config 改脏

## 为什么不是 GUI

这个工具的目标场景就是：

- SSH
- tmux / zellij
- 服务器
- 手机 Termux
- 无图形环境

所以它从一开始就是 CLI first。

## License

MIT

## 开发

如果你是仓库开发者，继续用 `uv` 最方便：

```bash
uv sync --dev
uv run python -m pytest -q
uv run ccproxy import-cc-switch
```
