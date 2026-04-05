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
- 本地 proxy 前台/后台运行
- `codex` / `claude` 临时启动器
- 从现有 `cc-switch` 数据库导入 provider

## 安装

开发安装：

```bash
cd ccproxy
uv sync
```

运行：

```bash
uv run ccproxy --help
```

安装成命令：

```bash
uv tool install .
```

## 快速开始

### 1. 从现有 cc-switch 导入 provider

```bash
uv run ccproxy import-cc-switch
```

### 2. 看看当前有哪些 provider

```bash
uv run ccproxy list codex
uv run ccproxy list claude
```

### 3. 启动本地 proxy

```bash
uv run ccproxy proxy up
uv run ccproxy proxy status
```

### 4. 用代理模式启动 Codex / Claude

```bash
uv run ccproxy codex
uv run ccproxy claude
```

如果你想在启动前切到指定 provider：

```bash
uv run ccproxy codex --provider yescodex
uv run ccproxy claude --provider ikun-1m
```

### 5. 运行中热切 provider

在另一个终端里：

```bash
uv run ccproxy use codex yescodex
uv run ccproxy use codex backup-provider

uv run ccproxy use claude ikun-1m
uv run ccproxy use claude fallback-claude
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

ccproxy proxy up
ccproxy proxy down
ccproxy proxy status
ccproxy proxy logs

ccproxy codex
ccproxy codex --provider my-codex -- --model gpt-5.4

ccproxy claude
ccproxy claude --provider my-claude -- --resume
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
