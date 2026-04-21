from __future__ import annotations

import curses
import unicodedata
from typing import Callable

from ccproxy.actions import (
    add_provider_action,
    build_test_snapshot,
    delete_provider_action,
    proxy_down_action,
    proxy_up_action,
    run_check_action,
    update_provider_action,
    use_provider_action,
)
from ccproxy.config import APP_CHOICES
from ccproxy.read_models import build_dashboard_snapshot, build_health_snapshot, format_provider_label


class TUIError(RuntimeError):
    pass


class CCProxyTUI:
    def __init__(self, lang: str = "en"):
        self.lang = lang
        self.apps = list(APP_CHOICES)
        self.current_app_index = 0
        self.selected_index = 0
        self.status_message = self.t("Ready", "就绪")
        self.screen: curses.window | None = None
        self.dashboard: dict[str, object] = {"proxy": {}, "apps": {}}
        self.rows: list[dict[str, object]] = []

    def t(self, en: str, zh: str) -> str:
        return zh if self.lang == "zh" else en

    @property
    def current_app(self) -> str:
        return self.apps[self.current_app_index]

    def run(self) -> int:
        return curses.wrapper(self._main)

    def _main(self, stdscr: curses.window) -> int:
        self.screen = stdscr
        curses.curs_set(0)
        curses.use_default_colors()
        stdscr.nodelay(False)
        stdscr.keypad(True)
        self.refresh_data()

        while True:
            self.draw()
            key = stdscr.getch()
            if not self.handle_key(key):
                return 0

    def refresh_data(self) -> None:
        self.dashboard = build_dashboard_snapshot()
        self.rows = list(build_health_snapshot(self.current_app)["apps"][self.current_app])
        if self.selected_index >= len(self.rows):
            self.selected_index = max(0, len(self.rows) - 1)

    def selected_row(self) -> dict[str, object] | None:
        if not self.rows:
            return None
        return self.rows[self.selected_index]

    def set_status(self, message: str, refresh: bool = False) -> None:
        self.status_message = message
        if refresh and self.screen is not None:
            self.draw()
            self.screen.refresh()

    def truncate(self, text: str, width: int) -> str:
        if width <= 0:
            return ""
        if self.display_width(text) <= width:
            return text

        if width == 1:
            return "…"

        clipped = self.clip_to_width(text, width - 1)
        if not clipped:
            return "…"
        return clipped + "…"

    def char_width(self, char: str) -> int:
        if not char:
            return 0
        if unicodedata.combining(char):
            return 0
        return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1

    def display_width(self, text: str) -> int:
        return sum(self.char_width(char) for char in text)

    def clip_to_width(self, text: str, width: int) -> str:
        if width <= 0:
            return ""
        total = 0
        parts: list[str] = []
        for char in text:
            char_cells = self.char_width(char)
            if total + char_cells > width:
                break
            parts.append(char)
            total += char_cells
        return "".join(parts)

    def add_line(self, window: curses.window, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
        if width <= 0:
            return
        clipped = self.clip_to_width(text, width)
        if not clipped:
            return
        try:
            window.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def draw_hline(self, window: curses.window, y: int, x: int, width: int) -> None:
        if width <= 0:
            return
        try:
            window.hline(y, x, curses.ACS_HLINE, width)
        except curses.error:
            try:
                window.hline(y, x, curses.ACS_HLINE, max(0, width - 1))
            except curses.error:
                pass

    def draw(self) -> None:
        assert self.screen is not None
        stdscr = self.screen
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 18 or width < 60:
            self.add_line(stdscr, 0, 0, self.t("Terminal too small for ccproxy TUI", "终端太小，无法显示 ccproxy TUI"), width - 1)
            self.add_line(stdscr, 2, 0, self.t("Need at least 60x18. Resize or use CLI subcommands.", "至少需要 60x18。请调整终端大小或使用 CLI 子命令。"), width - 1)
            self.add_line(stdscr, height - 1, 0, self.t("Press q to quit", "按 q 退出"), width - 1)
            stdscr.refresh()
            return

        proxy = self.dashboard.get("proxy", {})
        proxy_state = self.t("running", "运行中") if proxy.get("running") else self.t("stopped", "已停止")
        header1 = f"ccproxy TUI | app={self.current_app} | proxy={proxy_state} | healthy={'yes' if proxy.get('healthy') else 'no'}"
        current_id = self.dashboard.get("apps", {}).get(self.current_app, {}).get("current_provider_id")
        current_name = self.dashboard.get("apps", {}).get(self.current_app, {}).get("current_provider_name")
        current_label = self.t("none", "无") if not current_id else format_provider_label(str(current_id), None if current_name is None else str(current_name))
        header2 = f"current={current_label} | providers={len(self.rows)} | {self.status_message}"
        self.add_line(stdscr, 0, 0, header1, width - 1, curses.A_BOLD)
        self.add_line(stdscr, 1, 0, header2, width - 1)
        self.draw_hline(stdscr, 2, 0, width)

        list_top = 3
        list_bottom = height - 5
        visible_rows = max(1, list_bottom - list_top + 1)
        start = 0
        if self.selected_index >= visible_rows:
            start = self.selected_index - visible_rows + 1
        for offset, row in enumerate(self.rows[start : start + visible_rows]):
            line_y = list_top + offset
            selected = (start + offset) == self.selected_index
            marker = ">" if selected else " "
            current = "*" if row["current"] else " "
            status = str(row["status"])
            line = (
                f"{marker}{current} p={row['priority']:<4} "
                f"{row['provider_id']:<20} {self.truncate(str(row['provider_name']), 20):<20} "
                f"{status:<8} cf={row['consecutive_failures']}"
            )
            attr = curses.A_REVERSE if selected else curses.A_NORMAL
            self.add_line(stdscr, line_y, 0, line, width - 1, attr)

        if not self.rows:
            self.add_line(stdscr, list_top, 0, self.t("No providers configured for this app. Press a to add one.", "这个 app 还没有 provider。按 a 添加。"), width - 1)

        self.draw_hline(stdscr, height - 4, 0, width)
        row = self.selected_row()
        if row is None:
            detail1 = self.t("No provider selected", "当前没有选中 provider")
            detail2 = ""
        else:
            detail1 = f"base={row['base_url']} | model={row.get('model') or '-'} | auth={row.get('auth_mode') or '-'}"
            detail2 = f"error={row.get('last_error') or '-'}"
        self.add_line(stdscr, height - 3, 0, detail1, width - 1)
        self.add_line(stdscr, height - 2, 0, detail2, width - 1)
        keys = self.t(
            "Tab app  ↑↓/jk move  Enter/u use  c check  t test  h health  p proxy  x toggle-proxy  e edit  a add  d del  r refresh  q quit",
            "Tab 切 app  ↑↓/jk 移动  Enter/u 切换  c 检查  t 测试  h 健康  p 代理  x 切代理  e 编辑  a 添加  d 删除  r 刷新  q 退出",
        )
        self.add_line(stdscr, height - 1, 0, keys, width - 1, curses.A_DIM)
        stdscr.refresh()

    def handle_key(self, key: int) -> bool:
        if key in (ord("q"), 27):
            return False
        if key in (curses.KEY_UP, ord("k")):
            if self.rows:
                self.selected_index = max(0, self.selected_index - 1)
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            if self.rows:
                self.selected_index = min(len(self.rows) - 1, self.selected_index + 1)
            return True
        if key == 9:  # Tab
            self.current_app_index = (self.current_app_index + 1) % len(self.apps)
            self.selected_index = 0
            self.refresh_data()
            return True
        if key in (ord("r"),):
            self.refresh_data()
            self.set_status(self.t("Refreshed", "已刷新"))
            return True
        if key in (10, 13, curses.KEY_ENTER, ord("u")):
            self.use_selected_provider()
            return True
        if key == ord("c"):
            self.check_selected_provider()
            return True
        if key == ord("t"):
            self.test_current_app()
            return True
        if key == ord("h"):
            self.show_health_popup()
            return True
        if key == ord("p"):
            self.show_proxy_popup()
            return True
        if key == ord("x"):
            self.toggle_proxy()
            return True
        if key == ord("e"):
            self.edit_selected_provider()
            return True
        if key == ord("a"):
            self.add_provider()
            return True
        if key == ord("d"):
            self.delete_selected_provider()
            return True
        if key == ord("?"):
            self.show_help_popup()
            return True
        return True

    def show_popup(self, lines: list[str], title: str | None = None) -> None:
        assert self.screen is not None
        height, width = self.screen.getmaxyx()
        body = list(lines)
        if title:
            body.insert(0, title)
            body.insert(1, "")
        body.append("")
        body.append(self.t("Press any key to continue", "按任意键继续"))
        popup_h = min(height - 2, max(6, len(body) + 2))
        body_width = max(self.display_width(line) for line in body) if body else 0
        popup_w = min(width - 2, max(40, min(width - 2, body_width + 4)))
        top = max(1, (height - popup_h) // 2)
        left = max(1, (width - popup_w) // 2)
        win = curses.newwin(popup_h, popup_w, top, left)
        win.box()
        for idx, line in enumerate(body[: popup_h - 2], start=1):
            self.add_line(win, idx, 2, line, popup_w - 4)
        win.refresh()
        win.getch()
        del win
        self.draw()

    def prompt_input(self, prompt: str, default: str | None = None, allow_empty: bool = True) -> str | None:
        assert self.screen is not None
        height, width = self.screen.getmaxyx()
        label = prompt if not default else f"{prompt} [{default}]"
        curses.curs_set(1)
        curses.echo()
        self.screen.move(height - 1, 0)
        self.screen.clrtoeol()
        prompt_text = label + ": "
        self.add_line(self.screen, height - 1, 0, prompt_text, width - 1)
        self.screen.refresh()
        prompt_width = self.display_width(self.clip_to_width(prompt_text, width - 2))
        input_x = min(width - 2, prompt_width)
        input_limit = max(1, width - input_x - 1)
        raw = self.screen.getstr(height - 1, input_x, input_limit)
        curses.noecho()
        curses.curs_set(0)
        value = raw.decode(errors="replace").strip()
        if not value:
            if default is not None:
                return default
            if allow_empty:
                return ""
            return None
        return value

    def prompt_confirm(self, prompt: str, default_no: bool = True) -> bool:
        default = "N" if default_no else "Y"
        answer = self.prompt_input(f"{prompt} [y/{default.lower()}]", allow_empty=True)
        if not answer:
            return not default_no
        return answer.lower() in {"y", "yes"}

    def use_selected_provider(self) -> None:
        row = self.selected_row()
        if row is None:
            self.set_status(self.t("No provider selected", "未选中 provider"))
            return
        try:
            result = use_provider_action(self.current_app, str(row["provider_id"]))
        except Exception as exc:
            self.set_status(self.t(f"use failed: {exc}", f"切换失败: {exc}"))
            return
        self.refresh_data()
        self.set_status(self.t(f"current {self.current_app}: {result['provider_label']}", f"当前 {self.current_app}: {result['provider_label']}"))

    def check_selected_provider(self) -> None:
        row = self.selected_row()
        if row is None:
            self.set_status(self.t("No provider selected", "未选中 provider"))
            return
        self.set_status(self.t("Running check…", "正在运行检查…"), refresh=True)
        try:
            result = run_check_action(self.current_app, str(row["provider_id"]))
        except Exception as exc:
            self.set_status(self.t(f"check failed: {exc}", f"检查失败: {exc}"))
            return
        detail = result.stderr.strip() or result.stdout.strip() or result.summary
        self.show_popup(
            [
                f"app={result.app}",
                f"provider={format_provider_label(result.provider_id, result.provider_name)}",
                f"success={result.success}",
                f"duration={result.duration_sec:.1f}s",
                self.truncate(detail, 200),
            ],
            title=self.t("Check result", "检查结果"),
        )
        self.refresh_data()
        status = self.t("OK", "成功") if result.success else self.t("FAIL", "失败")
        self.set_status(f"[{status}] {result.provider_id} {result.duration_sec:.1f}s")

    def test_current_app(self) -> None:
        self.set_status(self.t(f"Testing {self.current_app}…", f"正在测试 {self.current_app}…"), refresh=True)
        try:
            snapshot = build_test_snapshot(self.current_app)
        except Exception as exc:
            self.set_status(self.t(f"test failed: {exc}", f"测试失败: {exc}"))
            return
        rows = snapshot["apps"][self.current_app]
        summary = snapshot["summary"]
        lines = []
        for row in rows[:10]:
            state = self.t("OK", "成功") if row["success"] else self.t("FAIL", "失败")
            lines.append(f"[{state}] {row['provider_id']} {row['duration_sec']:.1f}s")
            if row["detail"]:
                lines.append(f"  {self.truncate(str(row['detail']), 120)}")
        lines.append(self.t(
            f"summary: ok={summary['ok']} fail={summary['fail']} total={summary['total']}",
            f"汇总: 成功={summary['ok']} 失败={summary['fail']} 总计={summary['total']}",
        ))
        self.show_popup(lines, title=self.t(f"Test {self.current_app}", f"测试 {self.current_app}"))
        self.refresh_data()
        self.set_status(self.t(
            f"test {self.current_app}: ok={summary['ok']} fail={summary['fail']}",
            f"测试 {self.current_app}: 成功={summary['ok']} 失败={summary['fail']}",
        ))

    def show_health_popup(self) -> None:
        row = self.selected_row()
        if row is None:
            self.set_status(self.t("No provider selected", "未选中 provider"))
            return
        lines = [
            f"provider={format_provider_label(str(row['provider_id']), str(row['provider_name']))}",
            f"status={row['status']}",
            f"succ={row['total_successes']} fail={row['total_failures']} cfail={row['consecutive_failures']}",
            f"last_ok={row['last_success_at']}",
            f"last_fail={row['last_failure_at']}",
            f"cooldown_until={row['cooldown_until']}",
            f"last_error={row.get('last_error') or '-'}",
        ]
        self.show_popup(lines, title=self.t("Health details", "健康详情"))

    def show_proxy_popup(self) -> None:
        proxy = self.dashboard.get("proxy", {})
        lines = [
            f"running={proxy.get('running')}",
            f"healthy={proxy.get('healthy')}",
            f"listen=http://{proxy.get('host')}:{proxy.get('port')}",
            f"auto_failover={proxy.get('auto_failover')}",
            f"cooldown_sec={proxy.get('cooldown_sec')}",
            f"failure_threshold={proxy.get('failure_threshold')}",
            f"retry_attempts={proxy.get('retry_attempts')}",
            f"max_body_mb={proxy.get('max_body_mb')}",
            f"manager={proxy.get('manager')}",
            self.t("Controls: x toggles background proxy when possible.", "控制: 可用时按 x 切换后台代理。"),
        ]
        self.show_popup(lines, title=self.t("Proxy status", "代理状态"))

    def toggle_proxy(self) -> None:
        proxy = self.dashboard.get("proxy", {})
        try:
            if proxy.get("running"):
                stopped = proxy_down_action()
                if stopped:
                    self.set_status(self.t("Stopped background proxy", "已停止后台代理"))
                else:
                    self.set_status(self.t("Proxy was not a background job; maybe systemd-managed", "代理不是后台 job，可能由 systemd 托管"))
            else:
                status = proxy_up_action()
                self.set_status(self.t(f"Proxy running on http://{status['host']}:{status['port']}", f"代理已运行: http://{status['host']}:{status['port']}"))
        except Exception as exc:
            self.set_status(self.t(f"proxy toggle failed: {exc}", f"切换代理失败: {exc}"))
            return
        self.refresh_data()

    def edit_selected_provider(self) -> None:
        row = self.selected_row()
        if row is None:
            self.set_status(self.t("No provider selected", "未选中 provider"))
            return
        try:
            name = self.prompt_input(self.t("Name", "名称"), str(row["provider_name"]))
            base_url = self.prompt_input(self.t("Base URL", "Base URL"), str(row["base_url"]))
            model = self.prompt_input(self.t("Model", "模型"), str(row.get("model") or ""))
            auth_mode = self.prompt_input(self.t("Auth mode", "鉴权模式"), str(row.get("auth_mode") or "bearer"))
            priority_raw = self.prompt_input(self.t("Priority", "优先级"), str(row["priority"]))
            api_key = self.prompt_input(self.t("API key (blank keeps current)", "API key（留空保持不变）"), "", allow_empty=True)
            changed = update_provider_action(
                self.current_app,
                str(row["provider_id"]),
                name=name,
                base_url=base_url,
                model=model or None,
                auth_mode=auth_mode or None,
                priority=int(priority_raw) if priority_raw else None,
                api_key=api_key or None,
            )
        except Exception as exc:
            self.set_status(self.t(f"edit failed: {exc}", f"编辑失败: {exc}"))
            return
        self.refresh_data()
        if changed["changed"]:
            self.set_status(self.t(f"Updated {changed['provider_label']}", f"已更新 {changed['provider_label']}"))
        else:
            self.set_status(self.t("No provider changes", "provider 没有变化"))

    def add_provider(self) -> None:
        try:
            provider_id = self.prompt_input(self.t("Provider ID", "Provider ID"), allow_empty=False)
            if not provider_id:
                self.set_status(self.t("Add cancelled", "已取消添加"))
                return
            name = self.prompt_input(self.t("Name", "名称"), provider_id)
            base_url = self.prompt_input(self.t("Base URL", "Base URL"), allow_empty=False)
            api_key = self.prompt_input(self.t("API key", "API key"), allow_empty=False)
            if not base_url or not api_key:
                self.set_status(self.t("Base URL and API key are required", "Base URL 和 API key 必填"))
                return
            model = self.prompt_input(self.t("Model", "模型"), "")
            auth_mode = self.prompt_input(self.t("Auth mode", "鉴权模式"), "bearer")
            priority_raw = self.prompt_input(self.t("Priority", "优先级"), "1000")
            set_current = self.prompt_confirm(self.t("Set as current provider?", "设为当前 provider？"))
            result = add_provider_action(
                self.current_app,
                provider_id,
                name=name,
                base_url=base_url,
                api_key=api_key,
                model=model or None,
                auth_mode=auth_mode or None,
                priority=int(priority_raw) if priority_raw else None,
                set_current=set_current,
            )
        except Exception as exc:
            self.set_status(self.t(f"add failed: {exc}", f"添加失败: {exc}"))
            return
        self.refresh_data()
        self.selected_index = next((idx for idx, item in enumerate(self.rows) if item["provider_id"] == result["provider_id"]), self.selected_index)
        self.set_status(self.t(f"Added {result['provider_label']}", f"已添加 {result['provider_label']}"))

    def delete_selected_provider(self) -> None:
        row = self.selected_row()
        if row is None:
            self.set_status(self.t("No provider selected", "未选中 provider"))
            return
        if not self.prompt_confirm(self.t(f"Delete {row['provider_id']}?", f"删除 {row['provider_id']}？")):
            self.set_status(self.t("Delete cancelled", "已取消删除"))
            return
        try:
            result = delete_provider_action(self.current_app, str(row["provider_id"]))
        except Exception as exc:
            self.set_status(self.t(f"delete failed: {exc}", f"删除失败: {exc}"))
            return
        self.refresh_data()
        self.set_status(self.t(f"Deleted {result['provider_label']}", f"已删除 {result['provider_label']}"))

    def show_help_popup(self) -> None:
        self.show_popup(
            [
                self.t("Tab: switch app", "Tab：切换 app"),
                self.t("Arrows / j k: move selection", "方向键 / j k：移动选择"),
                self.t("Enter/u: switch current provider", "Enter/u：切换当前 provider"),
                self.t("c: run check on selected provider", "c：对选中 provider 运行检查"),
                self.t("t: test current app providers", "t：测试当前 app 的 provider"),
                self.t("h: show health details", "h：显示健康详情"),
                self.t("p: show proxy status", "p：显示代理状态"),
                self.t("x: toggle background proxy when possible", "x：在可行时切换后台代理"),
                self.t("e/a/d: edit / add / delete provider", "e/a/d：编辑 / 添加 / 删除 provider"),
                self.t("q: quit", "q：退出"),
            ],
            title=self.t("Key help", "按键帮助"),
        )


def run_tui(lang: str = "en") -> int:
    return CCProxyTUI(lang=lang).run()
