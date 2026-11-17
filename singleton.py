#!/usr/bin/env python3
"""
Ensure only one SyncLight UI is running — across glass, dialog, .app and old revisions.

Hard-kills competing processes (SIGKILL) so BLE/USB are free for this instance.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
from typing import List, Optional, Sequence, Set

from version import BT_VERSION, VERSION, as_dict

LOCK_DIR = os.path.expanduser("~/Library/Application Support/SyncLight")
LOCK_PATH = os.path.join(LOCK_DIR, "instance.lock")
META_PATH = os.path.join(LOCK_DIR, "instance.json")

# Substrings matched against full process command lines.
# Keep path-ish to avoid killing unrelated tools / Cursor helpers.
_COMPETE = (
    "synclight/glass_gui.py",
    "synclight\\glass_gui.py",
    "glass_gui.py",
    "synclight/dialog_gui.py",
    "dialog_gui.py",
    "synclight/bt_worker.py",
    "bt_worker.py",
    "synclight/window_gui.py",
    "window_gui.py",
    "synclight/gui.py",
    "/synclight/gui.py",
    "synclight/synclight.py",
    "/synclight.py",
    "SyncLight.app/Contents/MacOS/SyncLight",
    "SyncLight.app/Contents/MacOS/",
    "com.robobloq.synclight",
)

# Direct pkill -9 patterns (hard close). Same idea, belt-and-suspenders.
_PKILL9 = (
    "glass_gui.py",
    "dialog_gui.py",
    "bt_worker.py",
    "window_gui.py",
    "synclight/gui.py",
    "synclight.py",
    "SyncLight.app/Contents/MacOS/SyncLight",
    "com.robobloq.synclight",
)

_lock_fd: Optional[object] = None

# LaunchAgents that respawn SyncLight (esp. KeepAlive USB daemon).
# Do NOT include com.robobloq.synclight.gui.run — that IS the Dock GUI agent;
# bootout would kill the live glass_gui we just started.
_AGENT_LABELS = (
    "com.robobloq.synclight",  # synclight.py USB sleep daemon — holds the strip
    "com.robobloq.synclight.gui",
)


def _uid() -> int:
    try:
        return os.getuid()
    except Exception:
        return 501


def disable_synclight_agents() -> List[str]:
    """Stop LaunchAgents so KeepAlive cannot respawn competitors under us."""
    stopped: List[str] = []
    uid = _uid()
    for label in _AGENT_LABELS:
        domain = f"gui/{uid}/{label}"
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
        for cmd in (
            ["launchctl", "bootout", domain],
            ["launchctl", "unload", "-w", plist],
            ["launchctl", "unload", plist],
        ):
            try:
                subprocess.run(cmd, capture_output=True, check=False)
            except Exception:
                pass
        stopped.append(label)
    return stopped


def restore_usb_daemon() -> None:
    """Intentionally a no-op: KeepAlive USB daemon steals the strip from GUI/Dock."""
    return


def disable_keepalive_agent_permanently() -> None:
    """Rewrite USB daemon plist without KeepAlive and unload it."""
    label = "com.robobloq.synclight"
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
    disable_synclight_agents()
    if not os.path.exists(plist):
        return
    try:
        with open(plist) as f:
            body = f.read()
        body = body.replace("<key>KeepAlive</key>\n    <true/>", "<key>KeepAlive</key>\n    <false/>")
        body = body.replace("<key>KeepAlive</key>\n\t<true/>", "<key>KeepAlive</key>\n\t<false/>")
        body = body.replace("<key>RunAtLoad</key>\n    <true/>", "<key>RunAtLoad</key>\n    <false/>")
        body = body.replace("<key>RunAtLoad</key>\n\t<true/>", "<key>RunAtLoad</key>\n\t<false/>")
        with open(plist, "w") as f:
            f.write(body)
    except Exception:
        pass
    try:
        subprocess.run(["launchctl", "unload", "-w", plist], capture_output=True, check=False)
    except Exception:
        pass


def _pids_for(pattern: str) -> List[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    pids: List[int] = []
    for line in out.split():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmdline(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="], text=True
        ).strip()
    except Exception:
        return ""


def _is_protected(cmd: str) -> bool:
    low = (cmd or "").lower()
    # Never touch editor / agent tooling that merely mentions the repo name
    needles = (
        "cursor helper",
        "cursor/",
        "extension-host",
        "pgrep",
        "pkill",
        "code helper",
        "electron",
    )
    return any(n in low for n in needles)


def _looks_like_synclight(cmd: str) -> bool:
    low = (cmd or "").lower()
    if _is_protected(cmd):
        return False
    keys = (
        "glass_gui.py",
        "dialog_gui.py",
        "bt_worker.py",
        "window_gui.py",
        "synclight.py",
        "synclight/gui.py",
        "synclight.app",
        "com.robobloq.synclight",
    )
    return any(k in low for k in keys)


def _family_pids(root: Optional[int] = None) -> Set[int]:
    """Current process + all descendants (so we never kill our own bt_worker)."""
    root = os.getpid() if root is None else root
    try:
        out = subprocess.check_output(["ps", "-ax", "-o", "pid=,ppid="], text=True)
    except Exception:
        return {root}
    children: dict = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    family = {root}
    stack = [root]
    while stack:
        cur = stack.pop()
        for kid in children.get(cur, []):
            if kid not in family:
                family.add(kid)
                stack.append(kid)
    return family


def competing_pids(exclude: Sequence[int] = ()) -> List[int]:
    skip = _family_pids() | set(exclude)
    found: Set[int] = set()
    for pat in _COMPETE:
        for pid in _pids_for(pat):
            if pid in skip or not _alive(pid):
                continue
            cmd = _cmdline(pid)
            if not _looks_like_synclight(cmd):
                continue
            found.add(pid)
    return sorted(found)


def _signal_pids(pids: Sequence[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _pkill9_sweep(*, protect: Optional[Set[int]] = None) -> None:
    """
    Last-resort pkill. NEVER use while our own glass/bt_worker is running —
    macOS pkill -f bt_worker.py will murder the live room connection.
    """
    protect = protect or set()
    for pat in _PKILL9:
        # Prefer targeted kills collected earlier; pkill only for orphans
        # when protect is empty (pre-launch Dock script).
        if protect:
            continue
        try:
            subprocess.run(
                ["pkill", "-9", "-f", pat],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass


def stop_competitors(settle_s: float = 0.9, *, prelaunch: bool = False) -> List[int]:
    """
    Hard-close every *other* SyncLight process, then wait so BLE/USB settle.

    prelaunch=True: Dock/script before GUI exists — may use blanket pkill.
    prelaunch=False (default): only kill PIDs outside our process family
    so we never shoot our own bt_worker.
    """
    # Must disable KeepAlive agents FIRST — otherwise synclight.py respawns instantly
    disable_synclight_agents()

    family = _family_pids()
    victims = competing_pids()
    if victims:
        _signal_pids(victims, signal.SIGTERM)
        time.sleep(0.12)
        _signal_pids([p for p in victims if _alive(p)], signal.SIGKILL)

    if prelaunch:
        # No GUI yet — safe to sweep orphans by name
        _pkill9_sweep(protect=set())
        time.sleep(0.1)
    else:
        # Never blanket-pkill: it kills our live bt_worker and breaks «Комната»
        pass

    left = [p for p in competing_pids() if p not in family]
    if left:
        _signal_pids(left, signal.SIGKILL)
        victims = sorted(set(victims) | set(left))

    if settle_s > 0:
        time.sleep(settle_s)
    return victims


def prelaunch_cleanup(settle_s: float = 1.0) -> List[int]:
    """Call from Dock wrapper *before* glass_gui starts."""
    return stop_competitors(settle_s=settle_s, prelaunch=True)


def _write_meta(role: str) -> None:
    os.makedirs(LOCK_DIR, exist_ok=True)
    payload = {
        **as_dict(),
        "pid": os.getpid(),
        "role": role,
        "script": os.path.abspath(sys_argv0()),
        "started": int(time.time()),
    }
    with open(META_PATH, "w") as f:
        json.dump(payload, f, indent=2)


def sys_argv0() -> str:
    import sys

    return sys.argv[0] if sys.argv else __file__


def read_running_meta() -> Optional[dict]:
    try:
        with open(META_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def claim(role: str = "glass") -> str:
    """
    Hard-kill other revisions, take exclusive lock, write identity.
    """
    global _lock_fd
    os.makedirs(LOCK_DIR, exist_ok=True)

    previous = read_running_meta()
    try:
        disable_keepalive_agent_permanently()
    except Exception:
        pass
    killed = stop_competitors(settle_s=0.35)

    fd = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stop_competitors(settle_s=0.5)
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    fd.seek(0)
    fd.truncate()
    fd.write(f"{os.getpid()} {VERSION} BT{BT_VERSION} {role}\n")
    fd.flush()
    _lock_fd = fd

    _write_meta(role)

    note = f"SyncLight {VERSION} · BT{BT_VERSION}"
    if killed:
        note += f" · жёстко закрыто: {len(killed)}"
        if previous and (
            str(previous.get("version")) != VERSION
            or str(previous.get("bt_version")) != BT_VERSION
        ):
            old = previous.get("display") or previous.get("version") or "?"
            note += f" (была {old})"
    return note


def ensure_singleton(role: str = "glass") -> str:
    return claim(role)
