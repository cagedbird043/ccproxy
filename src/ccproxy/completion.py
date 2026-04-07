from __future__ import annotations

from textwrap import dedent

from ccproxy.config import APP_CHOICES, current_provider_id, load_config, ordered_provider_items

COMMANDS = [
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
]


def completion_provider_entries(app: str) -> list[tuple[str, str]]:
    data = load_config()
    current = current_provider_id(data, app)
    entries: list[tuple[str, str]] = []
    for provider_id, provider in ordered_provider_items(data, app):
        name = provider.get("name", provider_id)
        description = name
        if provider_id == current:
            description = f"{description} [current]"
        entries.append((provider_id, description))
    return entries


def completion_provider_ids(app: str) -> list[str]:
    return [provider_id for provider_id, _description in completion_provider_entries(app)]


def render_completion(shell: str) -> str:
    if shell == "bash":
        return render_bash_completion()
    if shell == "zsh":
        return render_zsh_completion()
    if shell == "fish":
        return render_fish_completion()
    raise ValueError(f"unsupported shell: {shell}")


def render_bash_completion() -> str:
    commands = " ".join(COMMANDS)
    apps = " ".join(APP_CHOICES)
    return dedent(
        f"""
        # bash completion for ccproxy
        _ccproxy_provider_ids() {{
          local app="$1"
          ccproxy _complete-providers "$app" 2>/dev/null | cut -f1
        }}

        _ccproxy_complete() {{
          local cur prev cmd subcmd subsubcmd
          cur="${{COMP_WORDS[COMP_CWORD]}}"
          prev=""
          if (( COMP_CWORD > 0 )); then
            prev="${{COMP_WORDS[COMP_CWORD-1]}}"
          fi
          cmd="${{COMP_WORDS[1]:-}}"
          subcmd="${{COMP_WORDS[2]:-}}"
          subsubcmd="${{COMP_WORDS[3]:-}}"

          case "$prev" in
            --scope)
              COMPREPLY=( $(compgen -W "user system" -- "$cur") )
              return 0
              ;;
            --auth-mode)
              COMPREPLY=( $(compgen -W "bearer x-api-key both" -- "$cur") )
              return 0
              ;;
            --auto-failover)
              COMPREPLY=( $(compgen -W "on off" -- "$cur") )
              return 0
              ;;
            --provider)
              case "$cmd" in
                codex)
                  COMPREPLY=( $(compgen -W "$(_ccproxy_provider_ids codex)" -- "$cur") )
                  return 0
                  ;;
                claude)
                  COMPREPLY=( $(compgen -W "$(_ccproxy_provider_ids claude)" -- "$cur") )
                  return 0
                  ;;
              esac
              ;;
          esac

          if [[ "$cur" == -* ]]; then
            local opts=""
            case "$cmd" in
              import-cc-switch)
                opts="--db-path"
                ;;
              add)
                opts="--name --base-url --api-key --model --auth-mode --priority --set-current"
                ;;
              update)
                opts="--name --base-url --api-key --model --auth-mode --priority --set-current"
                ;;
              test|health)
                opts="--json"
                ;;
              service)
                case "$subcmd" in
                  install)
                    opts="--scope --enable-now --user"
                    ;;
                  print)
                    opts="--scope --user"
                    ;;
                  uninstall)
                    opts="--scope --disable-now"
                    ;;
                esac
                ;;
              proxy)
                case "$subcmd" in
                  up|run)
                    opts="--host --port"
                    ;;
                  config)
                    case "$subsubcmd" in
                      set)
                        opts="--host --port --auto-failover --cooldown-sec --failure-threshold --retry-attempts --max-body-mb"
                        ;;
                    esac
                    ;;
                esac
                ;;
              codex|claude)
                opts="--provider"
                ;;
              completion)
                opts=""
                ;;
            esac
            COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
            return 0
          fi

          if (( COMP_CWORD == 1 )); then
            COMPREPLY=( $(compgen -W "{commands}" -- "$cur") )
            return 0
          fi

          case "$cmd" in
            add|list|current|show|update|delete|check|test|next|health|use)
              if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W "{apps}" -- "$cur") )
                return 0
              fi
              ;;
            show|update|delete|check|use)
              if (( COMP_CWORD == 3 )); then
                COMPREPLY=( $(compgen -W "$(_ccproxy_provider_ids "$subcmd")" -- "$cur") )
                return 0
              fi
              ;;
            service)
              if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W "install print uninstall" -- "$cur") )
                return 0
              fi
              ;;
            proxy)
              if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W "up run config down status logs" -- "$cur") )
                return 0
              fi
              if [[ "$subcmd" == "config" && COMP_CWORD -eq 3 ]]; then
                COMPREPLY=( $(compgen -W "show set" -- "$cur") )
                return 0
              fi
              ;;
            completion)
              if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
                return 0
              fi
              ;;
          esac
        }}

        complete -F _ccproxy_complete ccproxy
        """
    ).strip() + "\n"


def render_zsh_completion() -> str:
    return dedent(
        """
        #compdef ccproxy

        _ccproxy_provider_ids() {
          local app="$1"
          local line value desc
          local -a providers
          while IFS=$'\t' read -r value desc; do
            [[ -z "$value" ]] && continue
            if [[ -n "$desc" ]]; then
              desc="${desc//\\/\\\\}"
              desc="${desc//:/\\:}"
              providers+=("${value}:${desc}")
            else
              providers+=("${value}")
            fi
          done < <(ccproxy _complete-providers "$app" 2>/dev/null)
          _describe -t providers 'provider' providers
        }

        _ccproxy_provider_ids_codex() {
          _ccproxy_provider_ids codex
        }

        _ccproxy_provider_ids_claude() {
          _ccproxy_provider_ids claude
        }

        _ccproxy() {
          local curcontext="$curcontext" state line
          typeset -A opt_args

          local -a commands
          commands=(
            'init:create config skeleton'
            'import-cc-switch:import providers from cc-switch'
            'add:add a provider'
            'list:list providers'
            'current:show current provider'
            'show:show provider config'
            'update:update provider config'
            'delete:delete a provider'
            'check:run provider health check'
            'test:batch test providers'
            'next:rotate to next healthy provider'
            'health:show runtime health'
            'service:manage systemd service'
            'use:switch current provider'
            'proxy:manage local proxy'
            'codex:launch codex through proxy'
            'claude:launch claude through proxy'
          )

          if (( CURRENT == 2 )); then
            _describe -t commands 'ccproxy command' commands
            return
          fi

          case $words[2] in
            init)
              return
              ;;
            import-cc-switch)
              _arguments '--db-path[cc-switch database path]:file:_files'
              ;;
            add)
              _arguments \
                '2:app:(codex claude)' \
                '3:provider id:' \
                '--name[provider display name]:name:' \
                '--base-url[upstream base url]:url:' \
                '--api-key[upstream api key]:api key:' \
                '--model[default model for codex]:model:' \
                '--auth-mode[auth mode]:(bearer x-api-key both)' \
                '--priority[lower number means higher failover priority]:priority:' \
                '--set-current[set as current immediately]'
              ;;
            list|current|next)
              _arguments '2:app:(codex claude)'
              ;;
            test|health)
              _arguments '--json[print result as json]' '2:app:(codex claude)'
              ;;
            show|delete|check|use)
              if (( CURRENT == 3 )); then
                _values 'app' codex claude
                return
              fi
              if (( CURRENT == 4 )); then
                _ccproxy_provider_ids "$words[3]"
                return
              fi
              ;;
            update)
              if (( CURRENT == 3 )); then
                _values 'app' codex claude
                return
              fi
              if (( CURRENT == 4 )); then
                _ccproxy_provider_ids "$words[3]"
                return
              fi
              _arguments \
                '--name[provider display name]:name:' \
                '--base-url[upstream base url]:url:' \
                '--api-key[upstream api key]:api key:' \
                '--model[default model for codex]:model:' \
                '--auth-mode[auth mode]:(bearer x-api-key both)' \
                '--priority[lower number means higher failover priority]:priority:' \
                '--set-current[set as current immediately]'
              ;;
            service)
              if (( CURRENT == 3 )); then
                _describe -t service-commands 'service command' \
                  'install:install systemd unit' \
                  'print:print systemd unit' \
                  'uninstall:remove systemd unit'
                return
              fi
              case $words[3] in
                install)
                  _arguments \
                    '--scope[service scope]:(user system)' \
                    '--enable-now[enable and start immediately]' \
                    '--user[target user]:user:_users'
                  ;;
                print)
                  _arguments \
                    '--scope[service scope]:(user system)' \
                    '--user[target user]:user:_users'
                  ;;
                uninstall)
                  _arguments \
                    '--scope[service scope]:(user system)' \
                    '--disable-now[disable and stop immediately]'
                  ;;
              esac
              ;;
            proxy)
              if (( CURRENT == 3 )); then
                _describe -t proxy-commands 'proxy command' \
                  'up:start proxy in background' \
                  'run:run proxy in foreground' \
                  'config:show or update config' \
                  'down:stop background proxy' \
                  'status:show proxy status' \
                  'logs:show proxy logs'
                return
              fi
              case $words[3] in
                up|run)
                  _arguments '--host[listen host]:host:' '--port[listen port]:port:'
                  ;;
                config)
                  if (( CURRENT == 4 )); then
                    _describe -t proxy-config-commands 'proxy config command' \
                      'show:show proxy config' \
                      'set:update proxy config'
                    return
                  fi
                  case $words[4] in
                    set)
                      _arguments \
                        '--host[listen host]:host:' \
                        '--port[listen port]:port:' \
                        '--auto-failover[enable or disable auto failover]:(on off)' \
                        '--cooldown-sec[cooldown seconds]:seconds:' \
                        '--failure-threshold[failures before cooldown]:count:' \
                        '--retry-attempts[same-provider retries before failover]:count:' \
                        '--max-body-mb[maximum accepted request body size in MiB]:mebibytes:'
                      ;;
                  esac
                  ;;
              esac
              ;;
            codex)
              _arguments '--provider[provider id]:provider:_ccproxy_provider_ids_codex'
              ;;
            claude)
              _arguments '--provider[provider id]:provider:_ccproxy_provider_ids_claude'
              ;;
            completion)
              _arguments '2:shell:(bash zsh fish)'
              ;;
          esac
        }

        _ccproxy "$@"
        """
    ).strip() + "\n"


def render_fish_completion() -> str:
    return dedent(
        """
        function __fish_ccproxy_provider_ids
            set -l app $argv[1]
            ccproxy _complete-providers $app 2>/dev/null
        end

        complete -c ccproxy -f -n '__fish_use_subcommand' -a 'init import-cc-switch add list current show update delete check test next health service use proxy codex claude'

        complete -c ccproxy -n '__fish_seen_subcommand_from add list current show update delete check test next health use' -f -a 'codex claude'

        complete -c ccproxy -n '__fish_seen_subcommand_from service; and not __fish_seen_subcommand_from install print uninstall' -f -a 'install print uninstall'
        complete -c ccproxy -n '__fish_seen_subcommand_from service install print uninstall' -l scope -a 'user system'
        complete -c ccproxy -n '__fish_seen_subcommand_from service install' -l enable-now
        complete -c ccproxy -n '__fish_seen_subcommand_from service install print' -l user
        complete -c ccproxy -n '__fish_seen_subcommand_from service uninstall' -l disable-now

        complete -c ccproxy -n '__fish_seen_subcommand_from proxy; and not __fish_seen_subcommand_from up run config down status logs' -f -a 'up run config down status logs'
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy up run' -l host
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy up run' -l port
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy config; and not __fish_seen_subcommand_from show set' -f -a 'show set'
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy config set' -l host
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy config set' -l port
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy config set' -l auto-failover -a 'on off'
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy config set' -l cooldown-sec
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy config set' -l failure-threshold
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy config set' -l retry-attempts
        complete -c ccproxy -n '__fish_seen_subcommand_from proxy config set' -l max-body-mb

        complete -c ccproxy -n '__fish_seen_subcommand_from import-cc-switch' -l db-path
        complete -c ccproxy -n '__fish_seen_subcommand_from add' -l name
        complete -c ccproxy -n '__fish_seen_subcommand_from add' -l base-url
        complete -c ccproxy -n '__fish_seen_subcommand_from add' -l api-key
        complete -c ccproxy -n '__fish_seen_subcommand_from add' -l model
        complete -c ccproxy -n '__fish_seen_subcommand_from add' -l auth-mode -a 'bearer x-api-key both'
        complete -c ccproxy -n '__fish_seen_subcommand_from add' -l priority
        complete -c ccproxy -n '__fish_seen_subcommand_from add' -l set-current
        complete -c ccproxy -n '__fish_seen_subcommand_from update' -l name
        complete -c ccproxy -n '__fish_seen_subcommand_from update' -l base-url
        complete -c ccproxy -n '__fish_seen_subcommand_from update' -l api-key
        complete -c ccproxy -n '__fish_seen_subcommand_from update' -l model
        complete -c ccproxy -n '__fish_seen_subcommand_from update' -l auth-mode -a 'bearer x-api-key both'
        complete -c ccproxy -n '__fish_seen_subcommand_from update' -l priority
        complete -c ccproxy -n '__fish_seen_subcommand_from update' -l set-current
        complete -c ccproxy -n '__fish_seen_subcommand_from health test' -l json

        complete -c ccproxy -n '__fish_seen_subcommand_from codex' -l provider -a '(__fish_ccproxy_provider_ids codex)'
        complete -c ccproxy -n '__fish_seen_subcommand_from claude' -l provider -a '(__fish_ccproxy_provider_ids claude)'

        complete -c ccproxy -n '__fish_seen_subcommand_from use; and test (count (commandline -opc)) -ge 3; and test (commandline -opc)[3] = codex' -f -a '(__fish_ccproxy_provider_ids codex)'
        complete -c ccproxy -n '__fish_seen_subcommand_from use; and test (count (commandline -opc)) -ge 3; and test (commandline -opc)[3] = claude' -f -a '(__fish_ccproxy_provider_ids claude)'
        complete -c ccproxy -n '__fish_seen_subcommand_from show update delete; and test (count (commandline -opc)) -ge 3; and test (commandline -opc)[3] = codex' -f -a '(__fish_ccproxy_provider_ids codex)'
        complete -c ccproxy -n '__fish_seen_subcommand_from show update delete; and test (count (commandline -opc)) -ge 3; and test (commandline -opc)[3] = claude' -f -a '(__fish_ccproxy_provider_ids claude)'
        complete -c ccproxy -n '__fish_seen_subcommand_from check; and test (count (commandline -opc)) -ge 3; and test (commandline -opc)[3] = codex' -f -a '(__fish_ccproxy_provider_ids codex)'
        complete -c ccproxy -n '__fish_seen_subcommand_from check; and test (count (commandline -opc)) -ge 3; and test (commandline -opc)[3] = claude' -f -a '(__fish_ccproxy_provider_ids claude)'

        complete -c ccproxy -n '__fish_seen_subcommand_from completion' -f -a 'bash zsh fish'
        """
    ).strip() + "\n"
