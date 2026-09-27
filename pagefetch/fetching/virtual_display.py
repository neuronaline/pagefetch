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

import atexit
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger("pagefetch.fetching.virtual_display")

# Linux-only parent-death signal. When the Python process exits — cleanly,
# via signal, or OOM-killed — the kernel delivers ``PR_SET_PDEATHSIG`` to
# every child registered with it, so Xvfb cannot outlive its parent.
_PR_SET_PDEATHSIG = 1


def _set_pdeathsig() -> None:
    """Ask the kernel to ``SIGTERM`` this process when its parent dies.

    Runs in the forked child before ``execve`` so the prctl state is
    established before Xvfb starts. No-op on non-Linux platforms where the
    symbol is unavailable; the caller must guard invocation.
    """
    import ctypes

    try:
        libc = ctypes.CDLL(None)
    except OSError:
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
        except OSError:
            # Containers without glibc/prctl fall back to the process-group
            # based cleanup already handled by ``stop()``.
            return
    if not hasattr(libc, "prctl"):
        return
    # ``prctl`` returns -1 on error; raising is unnecessary — failing the
    # child launch here would mask any downstream Xvfb diagnostic.
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)



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
    def _socket_path(num: int) -> Path:
        return Path(f"/tmp/.X11-unix/X{num}")

    def start(self) -> str:
        """Start the Xvfb server and return the ``DISPLAY`` string."""
        if self.is_running and self._display is not None:
            return self._display

        binary = self._binary_path()

        # ``-nolisten tcp`` closes the TCP listener so other processes
        # cannot attach over the network; Camoufox uses the local Unix
        # socket that Xvfb always opens. ``+extension RANDR`` lets Firefox
        # query screen sizes normally; without it, some screen APIs misbehave.
        args = [
            binary,
            # Let Xvfb atomically reserve a server number and report it over
            # stdout. Checking lock files before spawning is a TOCTOU race.
            "-displayfd",
            "1",
            "-screen",
            "0",
            f"{self.width}x{self.height}x{self.depth}",
            "-nolisten",
            "tcp",
            "-ac",
            "-dpi",
            "96",
            "+extension",
            "RANDR",
        ]
        try:
            # On Linux, register ``PR_SET_PDEATHSIG`` so the kernel cleans up
            # this Xvfb if the parent Python process is killed unexpectedly
            # (``SIGKILL``, OOM, ``os._exit``); ``start_new_session`` alone
            # detaches the child from the controlling terminal but does not
            # bind its lifetime to ours. ``atexit`` covers the graceful exit
            # path, which prctl does not trigger.
            popen_kwargs: dict[str, object] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "start_new_session": True,
            }
            if sys.platform.startswith("linux"):
                popen_kwargs["preexec_fn"] = _set_pdeathsig
            self._process = subprocess.Popen(args, **popen_kwargs)
            atexit.register(self.stop)
        except OSError as exc:
            self._display = None
            self._display_num = None
            raise XvfbLaunchError(f"failed to spawn Xvfb: {exc}") from exc

        assert self._process.stdout is not None
        # Hold the stdout read end explicitly so we can close it once the
        # display number has been parsed. ``Popen.__exit__`` only closes
        # the pipe at process teardown; leaving the read FD open across
        # the whole client lifetime leaks one descriptor per ``XvfbDisplay``
        # and eventually trips ``EMFILE`` on long-running consumers.
        display_fd = self._process.stdout.fileno()
        os.set_blocking(display_fd, False)
        display_data = b""
        deadline = time.monotonic() + self._STARTUP_TIMEOUT
        try:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    self.stop()
                    raise XvfbLaunchError("Xvfb exited before allocating a display")
                try:
                    display_data += os.read(display_fd, 32)
                except BlockingIOError:
                    pass
                if b"\n" in display_data:
                    raw_display = display_data.split(b"\n", 1)[0]
                    try:
                        display_num = int(raw_display)
                    except ValueError:
                        self.stop()
                        raise XvfbLaunchError("Xvfb returned an invalid display number") from None
                    if display_num < 0:
                        self.stop()
                        raise XvfbLaunchError("Xvfb returned an invalid display number")
                    self._display_num = display_num
                    self._display = f":{display_num}"
                if self._display_num is not None and self._socket_path(self._display_num).exists():
                    logger.debug(
                        "Xvfb ready on display %s (pid=%d)",
                        self._display,
                        self._process.pid,
                    )
                    return self._display
                time.sleep(self._STARTUP_POLL)
        finally:
            # Always release the read end of the stdout pipe — both on
            # success and on every error path. Closing the high-level
            # stream is sufficient: ``BufferedReader.close`` cascades to
            # ``FileIO.close`` which calls ``os.close`` on the
            # descriptor. We use ``suppress`` because test doubles do
            # not always implement ``close``.
            if self._process and self._process.stdout is not None:
                with suppress(Exception):  # noqa: BLE001 — best-effort cleanup
                    self._process.stdout.close()

        display = self._display or (
            f"display number {self._display_num}" if self._display_num is not None else "an allocated display"
        )
        self.stop()
        raise XvfbLaunchError(
            f"Xvfb did not open {display} within "
            f"{self._STARTUP_TIMEOUT:.1f}s"
        )

    def stop(self) -> None:
        """Terminate the Xvfb subprocess (no-op if not running)."""
        atexit.unregister(self.stop)
        proc = self._process
        self._process = None
        self._display = None
        self._display_num = None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        # Signal the entire process group rather than only the Xvfb
        # parent. ``start_new_session=True`` puts Xvfb in its own
        # session/process-group, so ``os.killpg`` reaches any helper
        # subprocesses Xvfb may have spawned (font/fontconfig/glvnd
        # helpers, the X server's own internal threads from the kernel's
        # perspective) without needing to track them individually.
        pgid: int | None
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = None
        current_pgrp = os.getpgrp()
        if pgid is not None and pgid > 0 and pgid != current_pgrp:
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)
        else:
            with suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=self._SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Escalate to SIGKILL on the whole group. Falling back to
            # ``proc.kill()`` is not enough — a wedged Xvfb child
            # spawned before ``SIGTERM`` would still hold the
            # parent-death link and outlive us.
            if pgid is not None and pgid > 0 and pgid != current_pgrp:
                with suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
            else:
                with suppress(ProcessLookupError):
                    proc.kill()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=self._KILL_TIMEOUT)

    def __enter__(self) -> XvfbDisplay:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
