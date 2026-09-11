"""Linux virtual-display manager for headed Camoufox runs.

PageFetch keeps Windows/macOS on Firefox's native headless mode and switches
Linux to a real window rendered against an in-process ``Xvfb`` server. The
headed path eliminates well-known fingerprinting tells that ship with
Firefox's ``--headless`` flag (e.g. ``window.matchMedia`` quirks, the
``HeadlessChrome``-style navigator leaks some libraries detect).

The display lives as long as the :class:`XvfbDisplay` instance; the
:class:`~pagefetch.fetching.browser.BrowserFetcher` starts one lazily and
stops it from :meth:`~pagefetch.fetching.browser.BrowserFetcher.close`.
"""

from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger("pagefetch.fetching.virtual_display")


class XvfbError(RuntimeError):
    """Base class for Xvfb-related errors raised by this module."""


class XvfbNotFound(XvfbError):
    """The ``Xvfb`` binary is not on ``PATH``."""


class XvfbLaunchError(XvfbError):
    """``Xvfb`` could not be started or never opened its socket."""


class XvfbDisplay:
    """Manage one ``Xvfb`` subprocess.

    The instance owns a single child process. ``start()`` is idempotent and
    returns the ``DISPLAY`` string (e.g. ``":99"``); ``stop()`` terminates
    the process and resets internal state so a later ``start()`` re-runs
    cleanly.
    """

    _MIN_DISPLAY = 99
    _MAX_DISPLAY = 199
    _STARTUP_TIMEOUT = 5.0
    _STARTUP_POLL = 0.02
    _SHUTDOWN_TIMEOUT = 3.0
    _KILL_TIMEOUT = 1.0

    def __init__(
        self,
        *,
        width: int = 1920,
        height: int = 1080,
        depth: int = 24,
    ) -> None:
        self.width = width
        self.height = height
        self.depth = depth
        self._display_num: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._display: str | None = None

    @property
    def display(self) -> str:
        """The ``DISPLAY`` value (e.g. ``":99"``) the Xvfb is serving on."""
        if self._display is None:
            raise XvfbError("XvfbDisplay is not running")
        return self._display

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @staticmethod
    def _binary_path() -> str:
        path = shutil.which("Xvfb")
        if path is None:
            raise XvfbNotFound(
                "Xvfb binary not found on PATH; install it via "
                "'apt install xvfb' (Debian/Ubuntu), "
                "'dnf install xorg-x11-server-Xvfb' (Fedora/RHEL) "
                "or the equivalent for your distro."
            )
        return path

    @staticmethod
    def _is_display_free(num: int) -> bool:
        """Return True when no X server is bound to ``num``.

        Relies on the lock file Xorg writes when a server claims a display.
        We launch Xvfb with ``-nolisten tcp``, so a TCP probe would always
        be refused and could not distinguish "no server" from "our server".
        """
        return not Path(f"/tmp/.X{num}-lock").exists()

    @classmethod
    def _find_free_display(cls) -> int:
        for num in range(cls._MIN_DISPLAY, cls._MAX_DISPLAY + 1):
            if cls._is_display_free(num):
                return num
        raise XvfbLaunchError(
            f"no free X display in range :{cls._MIN_DISPLAY}-{cls._MAX_DISPLAY}"
        )

    def start(self) -> str:
        """Start the Xvfb server and return the ``DISPLAY`` string."""
        if self.is_running and self._display is not None:
            return self._display

        binary = self._binary_path()
        self._display_num = self._find_free_display()
        self._display = f":{self._display_num}"

        # ``-nolisten tcp`` closes the TCP listener so other processes
        # cannot attach over the network; Camoufox uses the local Unix
        # socket that Xvfb always opens. ``+extension RANDR`` lets Firefox
        # query screen sizes normally; without it, some screen APIs misbehave.
        args = [
            binary,
            self._display,
            "-screen",
            "0",
            f"{self.width}x{self.height}x{self.depth}",
            "-nolisten",
            "tcp",
            "-dpi",
            "96",
            "+extension",
            "RANDR",
        ]
        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # New session group keeps cleanup safe even if the parent
                # is signalled.
                start_new_session=True,
            )
        except OSError as exc:
            self._display = None
            self._display_num = None
            raise XvfbLaunchError(f"failed to spawn Xvfb: {exc}") from exc

        deadline = time.monotonic() + self._STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if not self._is_display_free(self._display_num):
                logger.debug(
                    "Xvfb ready on display %s (pid=%d)",
                    self._display,
                    self._process.pid,
                )
                return self._display
            if self._process.poll() is not None:
                self._display = None
                self._display_num = None
                self._process = None
                raise XvfbLaunchError(
                    f"Xvfb exited before opening display {self._display}"
                )
            time.sleep(self._STARTUP_POLL)

        self.stop()
        raise XvfbLaunchError(
            f"Xvfb did not open display {self._display} within "
            f"{self._STARTUP_TIMEOUT:.1f}s"
        )

    def stop(self) -> None:
        """Terminate the Xvfb subprocess (no-op if not running)."""
        proc = self._process
        self._process = None
        self._display = None
        self._display_num = None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        with suppress(ProcessLookupError):
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=self._SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                proc.kill()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=self._KILL_TIMEOUT)

    def __enter__(self) -> XvfbDisplay:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
