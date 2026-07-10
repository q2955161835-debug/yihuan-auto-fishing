from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from typing import Any

from ctypes import wintypes


HOTKEY_ID = 1
VK_F8 = 0x77
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class GlobalHotkey:
    """Run the Win32 F8 message loop on a dedicated thread."""

    def __init__(
        self,
        user32: Any | None = None,
        kernel32: Any | None = None,
        startup_timeout: float = 2.0,
        shutdown_timeout: float = 2.0,
    ) -> None:
        self.user32 = user32 or ctypes.windll.user32
        self.kernel32 = kernel32 or ctypes.windll.kernel32
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._registered = False
        self._startup_ready: threading.Event | None = None
        self._registration_result: bool | None = None
        self._last_error = ""

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(self._registered and thread is not None and thread.is_alive())

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def start(self, callback: Callable[[], None]) -> bool:
        with self._lock:
            if self.is_running:
                return True
            if self._thread is not None and not self._thread.is_alive():
                self._clear_thread_locked(self._thread)
            ready = threading.Event()
            thread = threading.Thread(
                target=self._message_loop,
                args=(callback, ready),
                name="global-f8-hotkey",
                daemon=True,
            )
            self._thread = thread
            self._thread_id = None
            self._registered = False
            self._startup_ready = ready
            self._registration_result = None
            self._last_error = ""
            thread.start()

        if not ready.wait(self.startup_timeout):
            self.stop()
            return False
        with self._lock:
            registration_succeeded = self._registration_result is True
        if not registration_succeeded:
            thread.join(self.shutdown_timeout)
            with self._lock:
                if self._thread is thread and not thread.is_alive():
                    self._clear_thread_locked(thread)
            return False
        return self.is_running

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            ready = self._startup_ready
            if thread is None:
                return

        if thread.is_alive() and ready is not None:
            if not ready.wait(self.startup_timeout):
                self._set_error("F8 热键消息线程启动超时")
                raise RuntimeError("F8 热键消息线程启动超时，无法安全停止")

        with self._lock:
            thread_id = self._thread_id
            registered = self._registered

        if thread.is_alive() and registered:
            if thread_id is None:
                self._set_error("F8 热键消息线程未发布线程号")
                raise RuntimeError("F8 热键消息线程未发布线程号")
            if not self.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0):
                self._set_error("无法请求 F8 热键消息线程退出")
                raise RuntimeError("无法请求 F8 热键消息线程退出")

        if thread.is_alive():
            thread.join(self.shutdown_timeout)
            if thread.is_alive():
                self._set_error("F8 热键消息线程无法退出")
                raise RuntimeError("F8 热键消息线程无法退出")

        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._clear_thread_locked(thread)

    def _message_loop(
        self,
        callback: Callable[[], None],
        ready: threading.Event,
    ) -> None:
        registered = False
        try:
            thread_id = int(self.kernel32.GetCurrentThreadId())
            registered = bool(
                self.user32.RegisterHotKey(None, HOTKEY_ID, 0, VK_F8)
            )
            with self._lock:
                self._thread_id = thread_id
                self._registered = registered
                self._registration_result = registered
            ready.set()
            if not registered:
                return

            message = wintypes.MSG()
            while True:
                result = int(
                    self.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                )
                if result == -1:
                    self._set_error("F8 热键消息循环读取失败")
                    break
                if result == 0:
                    break
                if message.message == WM_HOTKEY:
                    callback()
        except BaseException as error:
            self._set_error(f"F8 热键消息线程异常: {error}")
        finally:
            if not ready.is_set():
                with self._lock:
                    self._registration_result = False
                ready.set()
            if registered:
                self.user32.UnregisterHotKey(None, HOTKEY_ID)
            with self._lock:
                self._registered = False

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def _clear_thread_locked(self, thread: threading.Thread) -> None:
        if self._thread is not thread:
            return
        self._thread = None
        self._thread_id = None
        self._registered = False
        self._startup_ready = None
        self._registration_result = None
