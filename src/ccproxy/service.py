from __future__ import annotations

import getpass
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path


def current_username() -> str:
    return getpass.getuser()


def user_home(username: str | None = None) -> Path:
    if username is None:
        return Path.home()
    return Path(pwd.getpwnam(username).pw_dir)


def resolve_ccproxy_executable() -> Path:
    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.is_absolute() and argv0.exists():
        return argv0.resolve()

    found = shutil.which("ccproxy")
    if not found:
        raise FileNotFoundError("could not find `ccproxy` in PATH")
    return Path(found).resolve()


def systemd_service_scope() -> str | None:
    if not shutil.which("systemctl"):
        return None

    system = subprocess.run(
        ["systemctl", "is-active", "ccproxy.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    if system.returncode == 0 and system.stdout.strip() == "active":
        return "system"

    user = subprocess.run(
        ["systemctl", "--user", "is-active", "ccproxy.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    if user.returncode == 0 and user.stdout.strip() == "active":
        return "user"

    return None


def unit_path(scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".config/systemd/user/ccproxy.service"
    if scope == "system":
        return Path("/etc/systemd/system/ccproxy.service")
    raise ValueError(f"unsupported scope: {scope}")


def build_unit(
    scope: str,
    exec_path: Path,
    username: str | None = None,
    home: Path | None = None,
) -> str:
    username = username or current_username()
    home = home or user_home(username)
    xdg_config_home = home / ".config"
    xdg_state_home = home / ".local/state"

    if scope == "user":
        install_target = "default.target"
        service_user_line = ""
    elif scope == "system":
        install_target = "multi-user.target"
        service_user_line = f"User={username}\n"
    else:
        raise ValueError(f"unsupported scope: {scope}")

    return "\n".join(
        [
            "[Unit]",
            "Description=ccproxy local proxy for Codex and Claude",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            service_user_line.rstrip("\n"),
            f"WorkingDirectory={home}",
            f"Environment=HOME={home}",
            f"Environment=XDG_CONFIG_HOME={xdg_config_home}",
            f"Environment=XDG_STATE_HOME={xdg_state_home}",
            "Environment=PYTHONUNBUFFERED=1",
            f"ExecStart={exec_path} proxy run",
            "Restart=always",
            "RestartSec=2",
            "",
            "[Install]",
            f"WantedBy={install_target}",
            "",
        ]
    )


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def install_service(scope: str, enable_now: bool, username: str | None = None) -> Path:
    exec_path = resolve_ccproxy_executable()
    target = unit_path(scope)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_unit(scope, exec_path, username))

    if scope == "user":
        _run(["systemctl", "--user", "daemon-reload"])
        if enable_now:
            _run(["systemctl", "--user", "enable", "--now", "ccproxy.service"])
    else:
        if os.geteuid() != 0:
            raise PermissionError(
                "system scope install requires root; rerun with sudo or use `ccproxy service print --scope system`"
            )
        _run(["systemctl", "daemon-reload"])
        if enable_now:
            _run(["systemctl", "enable", "--now", "ccproxy.service"])
    return target


def uninstall_service(scope: str, disable_now: bool) -> Path:
    target = unit_path(scope)

    if scope == "user":
        if disable_now:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", "ccproxy.service"],
                check=False,
            )
        target.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    else:
        if os.geteuid() != 0:
            raise PermissionError("system scope uninstall requires root")
        if disable_now:
            subprocess.run(["systemctl", "disable", "--now", "ccproxy.service"], check=False)
        target.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=False)

    return target
