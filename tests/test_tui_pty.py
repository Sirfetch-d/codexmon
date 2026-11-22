"""Launch the real curses UI inside a pty and prove it starts, draws and quits."""

import os
import pty
import select
import signal
import struct
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_pty(argv, cols=120, rows=40, keys=b"q", settle=2.5):
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.environ["TERM"] = "xterm-256color"
        os.environ["LANG"] = "en_US.UTF-8"
        os.chdir(str(ROOT))
        try:
            os.execv(argv[0], argv)
        except Exception:
            os._exit(127)
    # parent
    import fcntl
    import termios

    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    chunks = []
    deadline = time.time() + settle
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    if keys:
        try:
            os.write(fd, keys)
        except OSError:
            pass
    exit_code = None
    deadline = time.time() + 5
    while time.time() < deadline:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            exit_code = status
            break
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunks.append(os.read(fd, 65536))
            except OSError:
                pass
    if exit_code is None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        exit_code = -1
    try:
        os.close(fd)
    except OSError:
        pass
    return b"".join(chunks).decode("utf-8", "replace"), exit_code


def _run_pty_until_exit(mode, cols=110, rows=30, settle=2.5, limit=8.0):
    """Start the TUI, trigger `mode`, and return the wait status (or 'HUNG')."""
    import fcntl
    import termios

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ["LANG"] = "en_US.UTF-8"
        os.chdir(str(ROOT))
        try:
            os.execv(sys.executable, [sys.executable, str(ROOT / "codexmon"),
                                      "--no-usage", "--interval", "1"])
        except Exception:
            os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    start = time.time()
    sent = False
    status = None
    while time.time() - start < limit:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:  # keep draining or the child blocks on write
                os.read(fd, 65536)
            except OSError:
                pass
        if not sent and time.time() - start > settle:
            sent = True
            if mode == "sigint":
                os.kill(pid, signal.SIGINT)
            elif mode == "sigterm":
                os.kill(pid, signal.SIGTERM)
            elif mode == "ctrl-c":
                os.write(fd, b"\x03")
            else:
                os.write(fd, b"q")
        wpid, st = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            status = os.waitstatus_to_exitcode(st)
            break
    if status is None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        status = "HUNG"
    try:
        os.close(fd)
    except OSError:
        pass
    return status


class TuiPtyTests(unittest.TestCase):
    def test_tui_boots_draws_and_quits(self):
        out, status = _run_pty(
            [sys.executable, str(ROOT / "codexmon"), "--no-usage", "--interval", "0.5"],
            cols=120,
            rows=40,
            keys=b"q",
            settle=3.0,
        )
        self.assertIn("AGENTS", out, "agents panel never rendered")
        self.assertIn("CHATGPT", out)
        self.assertIn("OPENCODE GO", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(status, 0, "TUI did not exit cleanly on 'q' (status %r)" % (status,))

    def test_tui_survives_small_terminal(self):
        out, status = _run_pty(
            [sys.executable, str(ROOT / "codexmon"), "--no-usage", "--interval", "0.5"],
            cols=60,
            rows=18,
            keys=b"q",
            settle=2.5,
        )
        self.assertNotIn("Traceback", out)
        self.assertEqual(status, 0)

    def test_every_exit_path_is_clean(self):
        for mode in ("q", "ctrl-c", "sigterm", "sigint"):
            status = _run_pty_until_exit(mode)
            self.assertEqual(status, 0, "%s did not exit cleanly (got %r)" % (mode, status))


if __name__ == "__main__":
    unittest.main()
