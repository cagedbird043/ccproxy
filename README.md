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
- 失败冷却和运行期健康状态
- 本地 proxy 前台/后台运行
- `codex` / `claude` 临时启动器
- 从现有 `cc-switch` 数据库导入 provider

## 安装

你不需要每次都写 `uv run`。

推荐直接执行仓库自带安装脚本。它会创建一个独立虚拟环境，并把 `ccproxy` 链接到 `~/.local/bin/ccproxy`。
同时会：

- 生成 `bash` / `zsh` / `fish` 自动补全脚本
- 给当前登录 shell 自动接入补全
- 把 `fish` 补全放进自动加载目录

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

如果你不是用 `install.sh` 安装，也可以手动导出补全脚本：

```bash
ccproxy completion zsh
ccproxy completion bash
ccproxy completion fish
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
ccproxy health
ccproxy health --json
```

### 3. 启动本地 proxy

```bash
ccproxy proxy up
ccproxy proxy status
ccproxy health
ccproxy proxy config show
ccproxy proxy config set --cooldown-sec 120
ccproxy proxy config set --auto-failover off
```

默认会开启自动故障转移。当前 provider 如果在请求时连接失败，或者返回 `429/5xx`，proxy 会自动尝试下一个 provider，并把 current 更新为新的健康 provider。
同时 proxy 会记录每个 provider 的成功/失败次数、最后错误和冷却时间。

`cooldown_sec` 的意思是：某个 provider 刚失败后，先把它放冷一段时间，再允许重新尝试，避免在坏节点之间来回抖动。

`ccproxy proxy status` 不只会看 pid 文件，也会看本地健康探针，所以如果你是用 `systemd` 托管代理，它现在也能正确显示为运行中。

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

### 6. 开机自启

如果你只需要“登录后自动启动”，用用户态 service：

```bash
ccproxy service install --scope user --enable-now
systemctl --user status ccproxy.service
```

如果你需要“开机后、登录前也可用”，用系统级 service：

```bash
sudo ccproxy service install --scope system --user "$USER" --enable-now
sudo systemctl status ccproxy.service
```

也可以先只打印 unit 文件：

```bash
ccproxy service print --scope user
ccproxy service print --scope system --user "$USER"
```

### 7. 中文 CLI

`ccproxy` 只做中文显示，不做中文命令、中文参数或中文取值。命令面始终保持英文。

如果你的终端 locale 是中文，比如 `zh_CN.UTF-8`，`ccproxy` 会自动输出中文；如果 locale 是英文，就输出英文。

```bash
LANG=zh_CN.UTF-8 ccproxy proxy status
LANG=zh_CN.UTF-8 ccproxy health
```

默认语言会按下面的优先级自动检测：

1. `LC_ALL`
2. `LC_MESSAGES`
3. `LANG`

这和 Linux 常见 CLI 的思路一致，适合 SSH / tmux / Termux 这种环境。

### 8. 自动补全

命令面始终保持英文，但安装脚本会把自动补全接好。

例如：

```bash
ccproxy pro<Tab>
ccproxy proxy st<Tab>
ccproxy use claude <Tab>
ccproxy codex --provider <Tab>
```

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
ccproxy proxy config show
ccproxy proxy config set --cooldown-sec 120
ccproxy proxy config set --auto-failover off
ccproxy completion zsh
ccproxy completion bash
ccproxy completion fish
ccproxy health
ccproxy health --json
ccproxy health claude
ccproxy service install --scope user --enable-now
ccproxy service print --scope system --user "$USER"

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
