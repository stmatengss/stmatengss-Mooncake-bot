"""Terminate Linux child processes when their supervising bot process disappears."""

import ctypes
import os
import signal
import sys


def guarded_args(args):
    if sys.platform == "linux":
        return [sys.executable, "-m", "mooncake_bot.child", str(os.getpid()), *args]
    return args


def main():
    parent = int(sys.argv[1])
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "Cannot bind child lifetime to supervisor")
        if os.getppid() != parent:
            raise SystemExit("Supervisor exited before child initialization")
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
