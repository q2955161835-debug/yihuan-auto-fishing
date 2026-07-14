from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from auto_fishing.model import FishingState, RuntimeSnapshot
from auto_fishing.storage.settings import AppSettings


_WINDOW_WIDTH = 400
_WINDOW_HEIGHT = 240


class MainWindow:
    """Always-on-top control window for the fishing automation."""

    def __init__(
        self,
        root: tk.Misc,
        controller: Any,
        settings_store: Any,
        *,
        window_title: str = "异环自动钓鱼",
        diagnostics_enabled: bool = False,
    ) -> None:
        self.root = root
        self.controller = controller
        self.settings_store = settings_store
        self.settings = settings_store.load()
        self._closed = False
        self._countdown_active = False
        self._runtime_active = False
        self._has_binding = False
        self._start_block_reason = ""
        self._state = FishingState.UNBOUND
        self._diagnostics_enabled = diagnostics_enabled
        self._diagnostic_path: Any | None = None

        root.title(window_title)
        window_height = 300 if diagnostics_enabled else _WINDOW_HEIGHT
        root.geometry(
            f"{_WINDOW_WIDTH}x{window_height}"
            f"{self.settings.window_x:+d}{self.settings.window_y:+d}"
        )
        root.minsize(_WINDOW_WIDTH, window_height)
        root.attributes("-topmost", True)

        self.binding_var = tk.StringVar(master=root, value="未绑定")
        self.count_var = tk.StringVar(
            master=root, value=str(self.settings.target_count)
        )
        self.auto_activate_var = tk.BooleanVar(
            master=root,
            value=self.settings.auto_activate_game,
        )
        self.state_var = tk.StringVar(
            master=root, value=FishingState.UNBOUND.value
        )
        self.progress_var = tk.StringVar(
            master=root, value=f"0/{self.settings.target_count}"
        )
        self.fps_var = tk.StringVar(master=root, value="0.0 FPS")
        self.error_var = tk.StringVar(master=root, value="无")
        self.diagnostic_path_var = tk.StringVar(master=root, value="")

        self._build_widgets()
        root.protocol("WM_DELETE_WINDOW", self.close)
        controller.subscribe(self._queue_snapshot)
        self._refresh_control_states()

    def _build_widgets(self) -> None:
        content = ttk.Frame(self.root, padding=(10, 7))
        content.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.columnconfigure(3, weight=1)

        ttk.Label(content, textvariable=self.binding_var).grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        ttk.Label(content, text="数量：").grid(row=1, column=0, sticky="w")
        self.count_spinbox = ttk.Spinbox(
            content,
            from_=1,
            to=999,
            textvariable=self.count_var,
            width=7,
        )
        self.count_spinbox.grid(row=1, column=1, sticky="w")
        ttk.Label(content, text="阶段：").grid(
            row=1, column=2, padx=(16, 4), sticky="e"
        )
        ttk.Label(content, textvariable=self.state_var).grid(
            row=1, column=3, sticky="w"
        )

        ttk.Label(content, text="进度：").grid(row=2, column=0, sticky="w")
        ttk.Label(content, textvariable=self.progress_var).grid(
            row=2, column=1, sticky="w"
        )
        ttk.Label(content, text="帧率：").grid(
            row=2, column=2, padx=(16, 4), sticky="e"
        )
        ttk.Label(content, textvariable=self.fps_var).grid(
            row=2, column=3, sticky="w"
        )

        ttk.Label(content, text="最近错误：").grid(row=3, column=0, sticky="nw")
        ttk.Label(
            content,
            textvariable=self.error_var,
            wraplength=305,
        ).grid(row=3, column=1, columnspan=3, sticky="w")

        buttons = ttk.Frame(content)
        buttons.grid(row=4, column=0, columnspan=4, pady=(6, 2), sticky="ew")
        for column in range(3):
            buttons.columnconfigure(column, weight=1)

        self.bind_button = ttk.Button(
            buttons, text="绑定并开始", command=self.on_bind
        )
        self.bind_button.grid(row=0, column=0, padx=2, sticky="ew")
        self.start_button = ttk.Button(buttons, text="开始", command=self.on_start)
        self.start_button.grid(row=0, column=1, padx=2, sticky="ew")
        self.pause_button = ttk.Button(
            buttons,
            text="暂停",
            command=self.on_pause_or_resume,
            state="disabled",
        )
        self.pause_button.grid(row=0, column=2, padx=2, sticky="ew")
        self.rebind_button = ttk.Button(
            buttons, text="重新绑定", command=self.on_rebind
        )
        self.rebind_button.grid(row=1, column=0, padx=2, pady=3, sticky="ew")
        self.auto_activate_check = ttk.Checkbutton(
            buttons,
            text="自动切回游戏",
            variable=self.auto_activate_var,
        )
        self.auto_activate_check.grid(
            row=1,
            column=1,
            padx=2,
            pady=3,
            sticky="w",
        )
        ttk.Button(buttons, text="退出", command=self.close).grid(
            row=1, column=2, padx=2, pady=3, sticky="ew"
        )

        if self._diagnostics_enabled:
            self.report_button = ttk.Button(
                buttons,
                text="报告错误",
                command=self.on_report_error,
            )
            self.report_button.grid(
                row=2,
                column=0,
                padx=2,
                pady=3,
                sticky="ew",
            )
            self.open_report_button = ttk.Button(
                buttons,
                text="打开文件位置",
                command=self.on_open_report,
                state="disabled",
            )
            self.open_report_button.grid(
                row=2,
                column=1,
                columnspan=2,
                padx=2,
                pady=3,
                sticky="ew",
            )
            ttk.Label(
                content,
                textvariable=self.diagnostic_path_var,
                wraplength=370,
            ).grid(row=5, column=0, columnspan=4, sticky="w")

        ttk.Label(content, text="F8 紧急暂停").grid(
            row=6 if self._diagnostics_enabled else 5,
            column=0,
            columnspan=4,
            sticky="w",
        )

    def on_bind(self) -> None:
        target = self._target_count()
        if target is None:
            self.error_var.set("数量必须是 1～999 的整数")
            return
        self._begin_binding(
            lambda on_tick, on_done: (
                self.controller.bind_and_start_after_countdown(
                    target,
                    on_tick,
                    on_done,
                )
            ),
            on_done=self._on_bind_start_done,
        )

    def on_rebind(self) -> None:
        self._begin_binding(self.controller.rebind, allow_paused=True)

    def _begin_binding(
        self,
        action: Any,
        *,
        allow_paused: bool = False,
        on_done: Any | None = None,
    ) -> None:
        if self._countdown_active or (
            self._runtime_active
            and not (allow_paused and self._state is FishingState.PAUSED)
        ):
            return
        self._countdown_active = True
        self._refresh_control_states()
        done = on_done or self._on_bind_done
        try:
            action(self._on_bind_tick, done)
        except Exception as error:
            done(None, str(error))

    def _on_bind_tick(self, seconds: int) -> None:
        self.binding_var.set(f"绑定倒计时：{seconds}")

    def _on_bind_done(self, title: str | None, error: str | None) -> None:
        self._countdown_active = False
        if error:
            self._has_binding = bool(title)
            self.binding_var.set(
                f"绑定失败，仍绑定：{title}" if title else "未绑定"
            )
            self.error_var.set(error)
        elif title:
            self._has_binding = True
            self.binding_var.set(f"已绑定：{title}")
            self.error_var.set("无")
        self._refresh_control_states()

    def _on_bind_start_done(
        self,
        title: str | None,
        error: str | None,
    ) -> None:
        self._on_bind_done(title, error)
        if title and not error:
            self._runtime_active = True
            self._refresh_control_states()

    def on_start(self) -> None:
        if (
            self._start_block_reason
            or not self._has_binding
            or self._runtime_active
            or self._countdown_active
        ):
            return
        target = self._target_count()
        if target is None:
            self.error_var.set("数量必须是 1～999 的整数")
            return
        self._countdown_active = True
        self.error_var.set("无")
        self._refresh_control_states()
        try:
            if self.auto_activate_var.get():
                self.controller.start(target, activate=True)
                self._on_start_done(None)
            else:
                self.controller.start_after_countdown(
                    target,
                    self._on_start_tick,
                    self._on_start_done,
                )
        except Exception as error:
            self._on_start_done(str(error))

    def _on_start_tick(self, seconds: int) -> None:
        self.state_var.set(f"开始倒计时：{seconds}")
        self.error_var.set("请在倒计时结束前切回已绑定的游戏窗口")

    def _on_start_done(self, error: str | None) -> None:
        self._countdown_active = False
        self.state_var.set(self._state.value)
        if error:
            self.error_var.set(error)
        else:
            self._runtime_active = True
        self._refresh_control_states()

    def on_pause_or_resume(self) -> None:
        try:
            if self._state is FishingState.PAUSED:
                self._countdown_active = True
                self.error_var.set("无")
                self._refresh_control_states()
                if self.auto_activate_var.get():
                    self.controller.resume(activate=True)
                    self._on_resume_done(None)
                else:
                    self.controller.resume_after_countdown(
                        self._on_resume_tick,
                        self._on_resume_done,
                    )
            else:
                self.controller.pause()
        except Exception as error:
            if self._state is FishingState.PAUSED:
                self._on_resume_done(str(error))
            else:
                self.error_var.set(str(error))

    def _on_resume_tick(self, seconds: int) -> None:
        self.state_var.set(f"继续倒计时：{seconds}")
        self.error_var.set("请在倒计时结束前切回已绑定的游戏窗口")

    def _on_resume_done(self, error: str | None) -> None:
        self._countdown_active = False
        self.state_var.set(self._state.value)
        self.error_var.set(error or "无")
        self._refresh_control_states()

    def block_start(self, reason: str) -> None:
        self._start_block_reason = reason
        self.error_var.set(reason)
        self._refresh_control_states()

    def show_warning(self, reason: str) -> None:
        self.error_var.set(reason)

    def on_report_error(self) -> None:
        self._diagnostic_path = None
        self.diagnostic_path_var.set("正在生成诊断包……")
        self.open_report_button.configure(state="disabled")
        try:
            self.controller.report_error()
        except Exception as error:
            self.error_var.set(f"诊断包生成失败：{error}")

    def show_diagnostic_result(self, result: Any) -> None:
        if self._closed:
            return
        if result.error or result.path is None:
            self._diagnostic_path = None
            self.diagnostic_path_var.set("")
            self.error_var.set(f"诊断包生成失败：{result.error or '未知错误'}")
            self.open_report_button.configure(state="disabled")
            return
        self._diagnostic_path = result.path
        self.diagnostic_path_var.set(f"诊断包：{result.path}")
        self.error_var.set("诊断包已生成")
        self.open_report_button.configure(state="normal")

    def on_open_report(self) -> None:
        path = self._diagnostic_path
        if path is None:
            return
        try:
            self.controller.open_report_location(path)
        except Exception as error:
            self.error_var.set(f"打开文件位置失败：{error}")
            self.open_report_button.configure(state="disabled")

    def _queue_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        if self._closed:
            return
        try:
            self.root.after(
                0,
                lambda current=snapshot: self.apply_snapshot(current),
            )
        except (RuntimeError, tk.TclError):
            if not self._closed:
                raise

    def apply_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        if self._closed:
            return
        self._state = snapshot.state
        self.state_var.set(snapshot.state.value)
        self.progress_var.set(f"{snapshot.completed}/{snapshot.target}")
        self.fps_var.set(f"{snapshot.fps:.1f} FPS")
        self.error_var.set(snapshot.error or "无")
        self._runtime_active = snapshot.state not in {
            FishingState.UNBOUND,
            FishingState.COMPLETE,
        }
        self.pause_button.configure(
            text="继续" if snapshot.state is FishingState.PAUSED else "暂停"
        )
        self._refresh_control_states()

    def _refresh_control_states(self) -> None:
        lock_binding = self._countdown_active or self._runtime_active
        self.count_spinbox.configure(
            state="disabled" if lock_binding else "normal"
        )
        self.bind_button.configure(
            state="disabled" if lock_binding else "normal"
        )
        rebind_locked = self._countdown_active or (
            self._runtime_active and self._state is not FishingState.PAUSED
        )
        self.rebind_button.configure(
            state="disabled" if rebind_locked else "normal"
        )
        self.start_button.configure(
            state=(
                "disabled"
                if (
                    self._runtime_active
                    or self._countdown_active
                    or self._start_block_reason
                    or not self._has_binding
                )
                else "normal"
            )
        )
        self.pause_button.configure(
            state=(
                "normal"
                if self._runtime_active and not self._countdown_active
                else "disabled"
            )
        )

    def _target_count(self) -> int | None:
        try:
            target = int(self.count_var.get())
        except ValueError:
            return None
        return target if 1 <= target <= 999 else None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        target = self._target_count() or self.settings.target_count
        settings = AppSettings(
            target_count=target,
            window_x=self.root.winfo_x(),
            window_y=self.root.winfo_y(),
            auto_activate_game=bool(self.auto_activate_var.get()),
        )
        try:
            self.settings_store.save(settings)
        finally:
            try:
                self.controller.shutdown()
            finally:
                self.root.destroy()
