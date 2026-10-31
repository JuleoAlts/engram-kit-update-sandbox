"""What the tests need on Windows (Git for Windows), where a process number of the shell is not one of Windows, a
symlink needs Developer Mode and only `.exe` files start without a shell. On macOS and Linux every function here does
exactly what the tests always did by hand, or nothing."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = os.name == "nt"

# The programs that the tools and their fakes run, for a PATH made by hand on Windows: its System32 alone holds
# thousands of files, and a copy of a shell's program away from its DLL does not start.
TOOLS = (
    "awk", "basename", "bash", "cat", "chmod", "cksum", "cp", "cut", "date", "dirname", "env", "false", "find", "git", "grep",
    "gzip", "head", "id", "ls", "mkdir", "mktemp", "mv", "ps", "rm", "rmdir", "sed", "seq", "sh", "sleep", "sort", "stat",
    "tail", "tee", "touch", "tr", "true", "uname", "uniq", "wc", "which",
)


def write_script(path, text: str):
    """A script with LF line breaks on every platform, and executable where that exists."""
    Path(path).write_bytes(text.encode("utf-8"))
    Path(path).chmod(0o755)


def link_command(target, link):
    """Makes `link` run `target`: a symlink, or on Windows a one-line script that starts it."""
    if WINDOWS:
        write_script(link, f'#!/bin/sh\nexec "{Path(target).as_posix()}" "$@"\n')
    else:
        os.symlink(target, link)


def tool_path(name: str):
    """Where a program of the shell is: System32 has other programs with the names find, sort and timeout, and only the
    git in cmd finds its own helpers whatever the PATH is."""
    where = '/cmd/git.exe' if name == "git" else f'$(command -v {name})'
    found = subprocess.run(["bash", "-c", f'PATH="/usr/bin:/mingw64/bin:$PATH"; cygpath -w "{where}"'], capture_output=True, text=True).stdout.strip()
    return found or shutil.which(name)


def fill_windows_farm(farm: Path, skip):
    """On Windows, fills a folder that stands for the whole PATH with the commands whose name `skip` does not refuse.
    Elsewhere it does nothing: the caller walks the real PATH."""
    if not WINDOWS:
        return
    for name in TOOLS:
        real = None if skip(name) else tool_path(name)
        if real:
            link_command(real, farm / name)
    for name in ("python", "python3"):
        if not skip(name):
            link_command(sys.executable, farm / name)
    launcher = None if skip("py") else shutil.which("py")
    if launcher:
        link_command(launcher, farm / "py")


def shell_kill(signal_name: str, pid: int) -> bool:
    """`kill` as the shell sees it, for a process number that came from the shell (Windows only: there `os.kill` takes a
    number of Windows, and would end whichever unrelated process has that number)."""
    return subprocess.run(["bash", "-c", f"kill -{signal_name} {pid}"], capture_output=True).returncode == 0


def start_live_process(test) -> int:
    """A process number that is alive for the shell that the tools run in: this process on macOS and Linux."""
    if not WINDOWS:
        return os.getpid()
    holder = subprocess.Popen(["bash", "-c", "echo $$; exec sleep 300"], stdout=subprocess.PIPE, text=True)
    test.addCleanup(holder.kill)
    test.addCleanup(holder.stdout.close)
    pid = int(holder.stdout.readline().strip())
    test.addCleanup(shell_kill, "9", pid)
    return pid
