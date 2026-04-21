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

现在它的入口做成了双模式：

- `ccproxy` 不带参数时：在真实终端里直接进入交互式 TUI，适合 SSH / tmux / Termux
- `ccproxy --help`、`ccproxy --json`、以及所有显式子命令：保持非交互式 CLI
- 如果不带参数但当前不是 TTY：不会硬启动 TUI，而是打印简短提示并以退出码 `2` 退出

## 当前范围

当前版本已经可以作为 `Codex` / `Claude` 的日常切换代理使用。

它目前聚焦在远程终端场景下最核心的能力，而不是做一个面面俱到的通用 provider 管理平台。

当前版本已支持：

- `Codex`
- `Claude`
- provider 导入、增删改查、列出、切换
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
ccproxy
```

装好以后：

- 想进交互面板，直接 `ccproxy`
- 想看传统命令说明，用 `ccproxy --help`
- 想给脚本读取全局摘要，用 `ccproxy --json`

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

`completion` 属于辅助命令：默认不会出现在主 `--help` 里，但会继续保留可调用性，方便你手工生成补全脚本。

如果你本来就喜欢 `uv` 或 `pipx`，也可以：

```bash
uv tool install .
pipx install .
```

## 快速开始

### 0. 先看交互式 TUI

在真实终端里直接运行：

```bash
ccproxy
```

这会进入一个面向终端的 `curses` TUI。默认显示：

- 当前 app（`codex` / `claude`）
- 当前 provider
- 各 provider 的优先级、健康状态、连续失败次数
- 本地 proxy 的运行状态

常用按键：

- `Tab`：切换 app
- `↑↓` / `j k`：移动选择
- `Enter` / `u`：切换到选中 provider
- `c`：检查当前选中 provider
- `t`：批量测试当前 app 的 provider
- `h`：看健康详情
- `p`：看 proxy 状态
- `x`：尽可能切换后台 proxy 开关
- `e` / `a` / `d`：编辑 / 添加 / 删除 provider
- `q`：退出

这个 TUI 的目标就是像 `htop` 那样，默认可直接进入，且能在 SSH / tmux 里用得住。

如果当前不是交互式终端，比如脚本、管道或某些 agent 子进程里直接调用裸 `ccproxy`，它不会误起 TUI，而是提示你改用 `--help`、`--json` 或显式子命令。

### 1. 从现有 cc-switch 导入 provider

```bash
ccproxy import-cc-switch
```

### 2. 看看当前有哪些 provider / dashboard

如果你想给脚本或 agent 一次性读取当前总览，可以用：

```bash
ccproxy --json
```

如果你是人直接在终端里看，通常更推荐直接跑：

```bash
ccproxy
```

它会给你一个更适合人工操作的总览面板。

非交互式命令依然全部可用，例如：

```bash
ccproxy list codex
ccproxy list claude
ccproxy check codex
ccproxy check codex --timeout-sec 20
ccproxy check claude
ccproxy test codex
ccproxy test codex --timeout-sec 20
ccproxy test
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
ccproxy proxy config set --failure-threshold 3
ccproxy proxy config set --retry-attempts 3
ccproxy proxy config set --max-body-mb 128
ccproxy proxy config set --auto-failover off
```

默认会开启自动故障转移。当前 provider 如果在请求时连接失败，或者返回 `429/5xx`，proxy 会先在同一个 provider 上重试，再按优先级尝试下一个 provider。
`current` 不会被自动改写；它始终保留为你手动选择的主 provider。自动故障转移只影响这一次请求和运行期健康状态。
同时 proxy 会记录每个 provider 的成功/失败次数、最后错误和冷却时间。
如果上游失败时吐回来的是 HTML 报错页、压缩残片或带 ANSI 控制符的脏日志，ccproxy 现在会把它归一化成更短、更人类可读的 JSON 错误，而不是原样把乱码继续塞给前台 agent。

`cooldown_sec` 的意思是：某个 provider 刚失败后，先把它放冷一段时间，再允许重新尝试，避免在坏节点之间来回抖动。

`failure_threshold` 的意思是：同一个 provider 连续失败多少次之后，才真正进入 cooldown。

`retry_attempts` 的意思是：同一次请求里，同一个 provider 最多重试多少次后才触发 failover。

`max_body_mb` 是本地代理允许接收的最大请求体大小，默认是 `64` MiB。这个值主要用来避免 `aiohttp` 默认 `1` MiB 上限把大上下文请求提前拦死。

`ccproxy proxy status` 不只会看 pid 文件，也会看本地健康探针，所以如果你是用 `systemd` 托管代理，它现在也能正确显示为运行中。
如果你修改了 `host`、`port` 或 `max_body_mb`，需要重启代理进程后才会生效。

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
ccproxy show codex yescodex
ccproxy update codex yescodex --priority 10
ccproxy delete codex old-provider

ccproxy use claude ikun-1m
ccproxy use claude fallback-claude

ccproxy test codex
ccproxy test claude
ccproxy test --json
ccproxy test codex --timeout-sec 20

ccproxy next codex
ccproxy next claude
```

当前会话不会被重启。下一次发起请求时会走新 provider。

`ccproxy test` 普通文本模式现在会边测边打印：每个 provider 一测完就立刻输出，不再等整批全部结束后才统一返回。
如果只是想快速排雷，可以配合 `--timeout-sec` 给单个 provider 设更短超时，例如 `ccproxy test codex --timeout-sec 15`。
只有 `--json` 模式为了保持合法 JSON，仍然会等整批完成后一次性输出。

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

ccproxy add codex my-codex --base-url https://example.com/v1 --api-key sk-xxx --model gpt-5.4 --priority 10
ccproxy add claude my-claude --base-url https://example.com --api-key sk-xxx --auth-mode bearer

ccproxy list codex
ccproxy current codex
ccproxy show codex my-codex
ccproxy update codex my-codex --priority 10
ccproxy delete codex old-codex
ccproxy use codex my-codex
ccproxy check codex
ccproxy test codex
ccproxy test
ccproxy next codex

ccproxy proxy up
ccproxy proxy down
ccproxy proxy status
ccproxy proxy logs
ccproxy proxy config show
ccproxy proxy config set --cooldown-sec 120
ccproxy proxy config set --failure-threshold 3
ccproxy proxy config set --retry-attempts 3
ccproxy proxy config set --max-body-mb 128
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
ccproxy test claude
ccproxy next claude
```

## 配置

配置文件默认放在：

- `~/.config/ccproxy/config.json`
- `~/.local/state/ccproxy/proxy.pid`
- `~/.local/state/ccproxy/proxy.log`

当前 provider 状态也保存在 `config.json` 中，proxy 每次请求都会重新读取，因此切换是热的。

每个 provider 还可以带一个整数 `priority`。数字越小，自动故障转移时优先级越高；但 `current` 仍然永远优先于其他 provider。

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

所以它从一开始就是 terminal first。

现在默认入口是 TUI，但本质上仍然是“终端优先”而不是“图形优先”：

- 默认入口：交互式 TUI
- 自动化 / 脚本：显式参数和子命令
- 批处理集成：`--json`
- SSH / tmux / Termux：一等公民

## License

MIT

## 开发

如果你是仓库开发者，继续用 `uv` 最方便：

```bash
uv sync --dev
uv run python -m pytest -q
uv run ccproxy import-cc-switch
uv run python -m ccproxy --help
uv run python -m ccproxy --json
```
