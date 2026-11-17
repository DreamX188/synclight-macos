#!/usr/bin/env python3
"""Client for bt_worker.py — BLE runs in a child process, not in Cocoa GUI."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(ROOT, "bt_worker.py")
PYTHON = "/usr/bin/python3"
BT_LOG = os.path.expanduser("~/Library/Logs/SyncLight-bt.log")
BT_ERR_LOG = os.path.expanduser("~/Library/Logs/SyncLight-bt-worker.log")


def _bt_log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(BT_LOG), exist_ok=True)
        ts = time.strftime("%H:%M:%S")
        ms = int((time.time() % 1) * 1000)
        thr = threading.current_thread().name
        with open(BT_LOG, "a") as f:
            f.write(f"{ts}.{ms:03d} [{thr}] {msg}\n")
    except Exception:
        pass


def _seed_status() -> Dict[str, Any]:
    """Show bound devices immediately (before first worker round-trip)."""
    try:
        from settings import load_settings

        cfg = load_settings()
    except Exception:
        cfg = {}
    out: Dict[str, Any] = {
        "ceiling": {"bound": False, "connected": False, "name": "", "address": ""},
        "floor": {"bound": False, "connected": False, "name": "", "address": ""},
        "last_error": "",
        "scan": [],
    }
    for role in ("ceiling", "floor"):
        raw = cfg.get(f"bt_{role}") or {}
        if isinstance(raw, str):
            raw = {"address": raw}
        addr = str(raw.get("address") or "")
        out[role] = {
            "bound": bool(addr),
            "connected": False,
            "name": str(raw.get("name") or ""),
            "address": addr,
        }
    return out


class BtClient:
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._closing = False
        self.last_error = ""
        self.last_status: Dict[str, Any] = _seed_status()
        # Coalesce Mac→room colors (ambilight + scenes): latest wins, fire-and-forget IPC
        self._ambi_cli_lock = threading.Lock()
        self._ambi_cli_pending: Optional[
            Tuple[Tuple[int, int, int], Optional[Tuple[int, int, int]], bool]
        ] = None
        self._ambi_cli_pump = False
        self._ambi_force_next = False

    def _ensure(self) -> None:
        if self._closing:
            raise RuntimeError("BT client shutting down")
        if self._proc and self._proc.poll() is None:
            return
        _bt_log("spawn worker")
        try:
            err_f = open(BT_ERR_LOG, "a")
            err_f.write(f"\n--- worker spawn {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            err_f.flush()
        except Exception:
            err_f = subprocess.DEVNULL
        # stderr to file (not PIPE) — PIPE deadlocks when bleak is noisy
        self._proc = subprocess.Popen(
            [PYTHON, "-u", WORKER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_f,
            cwd=ROOT,
            bufsize=0,
            start_new_session=True,
        )
        _bt_log(f"spawn ok pid={self._proc.pid}")

    def _read_line(self, wait: float) -> bytes:
        assert self._proc and self._proc.stdout
        deadline = time.time() + wait
        buf = bytearray()
        while time.time() < deadline:
            if self._closing:
                raise RuntimeError("BT client shutting down")
            if self._proc.poll() is not None:
                code = self._proc.returncode
                self._proc = None
                raise RuntimeError(f"BT worker exited (code {code})")
            r, _, _ = select.select([self._proc.stdout], [], [], 0.2)
            if not r:
                continue
            chunk = self._proc.stdout.read(1)
            if not chunk:
                continue
            if chunk == b"\n":
                return bytes(buf)
            buf.extend(chunk)
            if len(buf) > 2_000_000:
                raise RuntimeError("BT worker reply too large")
        raise RuntimeError(f"BT worker timeout after {wait:.1f}s (buf={len(buf)}B)")

    def _call(self, cmd: str, wait: float = 30.0, **payload) -> Dict[str, Any]:
        t0 = time.time()
        with self._lock:
            if self._closing and cmd != "quit":
                raise RuntimeError("BT client shutting down")
            self._ensure()
            assert self._proc and self._proc.stdin
            req = {"cmd": cmd, **payload}
            raw = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
            _bt_log(f"→ {cmd} wait={wait:.0f}s keys={list(payload)}")
            try:
                self._proc.stdin.write(raw)
                self._proc.stdin.flush()
            except Exception as exc:
                self.last_error = f"worker write: {exc}"
                _bt_log(f"write fail {cmd}: {exc}")
                self._kill()
                raise RuntimeError(self.last_error)
            try:
                line = self._read_line(wait)
            except Exception as exc:
                dt = time.time() - t0
                _bt_log(f"read fail {cmd} after {dt:.2f}s: {exc}")
                self._kill()
                raise
            try:
                resp = json.loads(line.decode("utf-8"))
            except Exception as exc:
                _bt_log(f"bad json {cmd}: {line[:200]!r} ({exc})")
                raise RuntimeError(f"bad worker reply: {line!r} ({exc})")
            dt = time.time() - t0
            if resp.get("status"):
                prev_scan = self.last_status.get("scan") or []
                self.last_status = resp["status"]
                if not self.last_status.get("scan") and prev_scan:
                    self.last_status["scan"] = prev_scan
            if not resp.get("ok"):
                self.last_error = str(resp.get("error") or "BT error")
                trace = str(resp.get("trace") or "")
                _bt_log(f"← {cmd} ERR {dt:.2f}s {self.last_error}")
                if trace:
                    _bt_log(f"← {cmd} TRACE {trace[-400:]}")
                raise RuntimeError(self.last_error)
            self.last_error = str(resp.get("error") or "")
            c = (self.last_status.get("ceiling") or {}) if self.last_status else {}
            f = (self.last_status.get("floor") or {}) if self.last_status else {}
            if cmd in ("connect", "status", "scan", "apply", "assign", "swap", "blink"):
                _bt_log(
                    f"← {cmd} ok {dt:.2f}s ceil={c.get('connected')} "
                    f"floor={f.get('connected')} note={resp.get('note', '')!s}"
                )
            elif cmd == "quit":
                _bt_log(f"← quit ok {dt:.2f}s")
            return resp

    def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc:
            return
        pid = getattr(proc, "pid", None)
        _bt_log(f"kill worker pid={pid}")
        try:
            if pid:
                os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=0.4)
        except Exception:
            pass
        _bt_log(f"kill done pid={pid}")

    def status(self) -> Dict[str, Any]:
        try:
            return self._call("status", wait=8).get("status") or self.last_status
        except Exception as exc:
            self.last_error = str(exc)
            st = dict(self.last_status)
            st["last_error"] = self.last_error
            return st

    def scan(self, timeout: float = 8.0) -> List[Dict[str, Any]]:
        resp = self._call("scan", wait=timeout + 8, timeout=timeout)
        devices = list(resp.get("devices") or [])
        st = dict(resp.get("status") or self.last_status)
        st["scan"] = devices
        if resp.get("error"):
            st["last_error"] = resp["error"]
        self.last_status = st
        return devices

    def assign(self, role: str, address: str, name: str = "", protocol: str = "") -> None:
        self._call("assign", wait=10, role=role, address=address, name=name, protocol=protocol)

    def swap_roles(self) -> str:
        resp = self._call("swap", wait=8)
        return str(resp.get("note") or "Потолок ↔ Пол")

    def connect_bound(self, force: bool = False) -> str:
        _bt_log(f"connect_bound force={force}")
        try:
            resp = self._call("connect", wait=45, force=bool(force))
        except Exception as exc:
            _bt_log(f"connect_bound FAIL: {exc}")
            _bt_log(traceback.format_exc()[-600:])
            raise
        try:
            self.status()
        except Exception as exc:
            _bt_log(f"connect_bound status refresh: {exc}")
        note = str(resp.get("note") or "ok")
        _bt_log(f"connect_bound done: {note}")
        return note

    def apply(
        self,
        target: str,
        *,
        power: Optional[bool] = None,
        color: Optional[Tuple[int, int, int]] = None,
        brightness: Optional[int] = None,
        effect: Optional[int] = None,
    ) -> None:
        payload: Dict[str, Any] = {"target": target}
        if power is not None:
            payload["power"] = power
        if color is not None:
            payload["color"] = list(color)
        if brightness is not None:
            payload["brightness"] = brightness
        if effect is not None:
            payload["effect"] = effect
        self._call("apply", wait=25, **payload)

    def force_ambient(self) -> None:
        """Next set_ambient bypasses worker delta-skip (scene/FX start)."""
        self._ambi_force_next = True

    def set_ambient(
        self,
        top: Tuple[int, int, int],
        bottom: Optional[Tuple[int, int, int]] = None,
        force: bool = False,
    ) -> None:
        """
        Mac→room color push for ambilight and scenes (non-blocking).

        Coalesces to the latest colors; IPC is fire-and-forget so BLE keeps up.
        """
        if self._closing:
            return
        if force or self._ambi_force_next:
            force = True
            self._ambi_force_next = False
        with self._ambi_cli_lock:
            prev = self._ambi_cli_pending
            # Preserve force if a forced frame is still queued
            if prev and prev[2]:
                force = True
            self._ambi_cli_pending = (
                tuple(top),
                tuple(bottom) if bottom is not None else None,
                force,
            )
            start = not self._ambi_cli_pump
            if start:
                self._ambi_cli_pump = True
        if start:
            threading.Thread(
                target=self._ambient_cli_pump, name="bt-ambi-cli", daemon=True
            ).start()

    def _ambient_cli_pump(self) -> None:
        """Push coalesced colors; write-only IPC (no ack wait)."""
        try:
            while True:
                with self._ambi_cli_lock:
                    pending = self._ambi_cli_pending
                    self._ambi_cli_pending = None
                if pending is None:
                    with self._ambi_cli_lock:
                        self._ambi_cli_pump = False
                    return
                if self._closing:
                    with self._ambi_cli_lock:
                        self._ambi_cli_pump = False
                        self._ambi_cli_pending = None
                    return
                top, bottom, force = pending
                payload: Dict[str, Any] = {
                    "cmd": "ambient",
                    "color": list(top),
                    "ack": False,
                    "force": bool(force),
                }
                if bottom is not None:
                    payload["color_bottom"] = list(bottom)
                try:
                    with self._lock:
                        if self._closing:
                            return
                        self._ensure()
                        assert self._proc and self._proc.stdin
                        raw = (
                            json.dumps(payload, ensure_ascii=False) + "\n"
                        ).encode("utf-8")
                        self._proc.stdin.write(raw)
                        self._proc.stdin.flush()
                except Exception as exc:
                    _bt_log(f"ambient pump: {exc}")
                    try:
                        with self._lock:
                            self._kill()
                    except Exception:
                        pass
                    time.sleep(0.08)
        finally:
            with self._ambi_cli_lock:
                self._ambi_cli_pump = False

    def rename(self, address: str, label: str) -> None:
        self._call("rename", wait=8, address=address, label=label)

    def clear_role(self, role: str) -> None:
        self._call("clear", wait=8, role=role)

    def blink(self, role: str, color: Tuple[int, int, int] = (0, 180, 255)) -> None:
        self._call("blink", wait=20, role=role, color=list(color))

    def restart_worker(self) -> None:
        """Drop the live worker so the next call spawns a fresh one (retries)."""
        _bt_log("restart_worker")
        with self._lock:
            self._kill()

    def close(self) -> None:
        """
        Always non-blocking for the UI thread.

        Politely quitting the worker runs BLE disconnect_all and can freeze
        Cocoa for tens of seconds (or forever if connect holds the lock).
        Kill the process group instead — OS tears down CoreBluetooth sockets.
        """
        _bt_log("close: begin")
        self._closing = True
        got = False
        try:
            got = self._lock.acquire(timeout=0.12)
        except Exception:
            got = False
        try:
            proc = self._proc
            self._proc = None
        finally:
            if got:
                try:
                    self._lock.release()
                except Exception:
                    pass
        if not proc:
            _bt_log("close: no worker")
            return
        pid = getattr(proc, "pid", None)
        _bt_log(f"close: SIGKILL pid={pid}")
        try:
            if pid:
                os.killpg(pid, signal.SIGKILL)
        except Exception as exc:
            _bt_log(f"close: killpg {exc}")
            try:
                proc.kill()
            except Exception as exc2:
                _bt_log(f"close: kill {exc2}")
        try:
            proc.wait(timeout=0.35)
        except Exception:
            pass
        _bt_log("close: done")


_client: Optional[BtClient] = None
_client_lock = threading.Lock()


def get_bt_client() -> BtClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = BtClient()
        return _client
