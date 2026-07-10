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

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(self._registered and thread is not None and thread.is_alive())

    def start(self, callback: Callable[[], None]) -> bool:
        with self._lock:
            if self.is_running:
                return True
            ready = threading.Event()
            registration: list[bool] = []
            thread = threading.Thread(
                target=self._message_loop,
                args=(callback, ready, registration),
                name="global-f8-hotkey",
                daemon=True,
            )
            self._thread = thread
            thread.start()

        if not ready.wait(self.startup_timeout):
            self.stop()
            return False
        if not registration or not registration[0]:
            thread.join(self.shutdown_timeout)
            with self._lock:
                if self._thread is thread:
                    self._thread = None
                    self._thread_id = None
            return False
        return True

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            thread_id = self._thread_id
            if thread is None:
                return

        if thread.is_alive() and thread_id is not None:
            self.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            thread.join(self.shutdown_timeout)
            if thread.is_alive():
                raise RuntimeError("F8 热键消息线程无法退出")

        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._thread_id = None
                self._registered = False

    def _message_loop(
        self,
        callback: Callable[[], None],
        ready: threading.Event,
        registration: list[bool],
    ) -> None:
        registered = False
        try:
            self._thread_id = int(self.kernel32.GetCurrentThreadId())
            registered = bool(
                self.user32.RegisterHotKey(None, HOTKEY_ID, 0, VK_F8)
            )
            self._registered = registered
            registration.append(registered)
            ready.set()
            if not registered:
                return

            message = wintypes.MSG()
            while True:
                result = int(
                    self.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                )
                if result <= 0:
                    break
                if message.message == WM_HOTKEY:
                    callback()
        finally:
            if not ready.is_set():
                registration.append(False)
                ready.set()
            if registered:
                self.user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered = False
