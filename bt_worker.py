#!/usr/bin/env python3
"""
Standalone Bluetooth worker process.

Runs outside the Cocoa GUI so CoreBluetooth/Bleak are not blocked by
NSApplication / TCC attribution quirks. Speaks simple JSON lines on stdin/stdout.
"""

from __future__ import annotations

import json
import sys
import traceback

ROOT_HINT = __file__


def _reply(ok: bool, **payload):
    msg = {"ok": ok, **payload}
    sys.stdout.buffer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    from bt_strips import get_bt_manager
    from settings import load_settings, save_settings

    mgr = get_bt_manager()
    cfg = load_settings()
    mgr.load_bindings(cfg)

    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            _reply(False, error=f"bad json: {exc}")
            continue

        cmd = str(req.get("cmd", ""))
        try:
            if cmd == "ping":
                _reply(True, pong=True)
            elif cmd == "status":
                _reply(True, status=mgr.status())
            elif cmd == "scan":
                timeout = float(req.get("timeout", 8))
                found = mgr.scan(timeout)
                _reply(
                    True,
                    devices=[
                        {
                            "address": d.address,
                            "name": d.name,
                            "rssi": d.rssi,
                            "protocol": d.protocol,
                        }
                        for d in found
                    ],
                    error=mgr.last_error,
                    status=mgr.status(),
                )
            elif cmd == "assign":
                role = str(req.get("role", ""))
                address = str(req.get("address", ""))
                name = str(req.get("name", ""))
                protocol = str(req.get("protocol", ""))
                mgr.assign(role, address, name=name, protocol=protocol)
                cfg.update(mgr.dump_bindings())
                save_settings(cfg)
                _reply(True, status=mgr.status())
            elif cmd == "swap":
                mgr.swap_roles()
                cfg.update(mgr.dump_bindings())
                save_settings(cfg)
                _reply(True, status=mgr.status(), note="Потолок ↔ Пол")
            elif cmd == "connect":
                force = bool(req.get("force"))
                print(f"worker connect force={force}", file=sys.stderr, flush=True)
                note = mgr.connect_bound(force=force)
                print(f"worker connect done: {note}", file=sys.stderr, flush=True)
                _reply(True, note=note, status=mgr.status())
            elif cmd == "apply":
                target = str(req.get("target", "bt"))
                power = req.get("power", None)
                color = req.get("color")
                brightness = req.get("brightness")
                effect = req.get("effect")
                kwargs = {}
                if power is not None:
                    kwargs["power"] = bool(power)
                if color is not None:
                    kwargs["color"] = tuple(int(x) for x in color)
                if brightness is not None:
                    kwargs["brightness"] = int(brightness)
                if effect is not None:
                    kwargs["effect"] = int(effect)
                mgr.apply(target, **kwargs)
                _reply(True, status=mgr.status())
            elif cmd == "ambient":
                color = req.get("color") or [0, 0, 0]
                top = (int(color[0]), int(color[1]), int(color[2]))
                bot_raw = req.get("color_bottom")
                bottom = None
                if bot_raw is not None:
                    bottom = (int(bot_raw[0]), int(bot_raw[1]), int(bot_raw[2]))
                force = bool(req.get("force"))
                mgr.set_ambient(top, bottom, force=force)
                # Fire-and-forget from GUI: no ack keeps IPC off the critical path
                if req.get("ack", True):
                    _reply(True)
            elif cmd == "rename":
                address = str(req.get("address", ""))
                label = str(req.get("label", "")).strip()
                mgr.rename(address, label)
                cfg = load_settings()
                labels = dict(cfg.get("bt_labels") or {})
                if label:
                    labels[address] = label
                else:
                    labels.pop(address, None)
                cfg["bt_labels"] = labels
                cfg.update(mgr.dump_bindings())
                save_settings(cfg)
                _reply(True, status=mgr.status())
            elif cmd == "clear":
                role = str(req.get("role", ""))
                mgr.clear(role)
                cfg = load_settings()
                cfg.update(mgr.dump_bindings())
                save_settings(cfg)
                _reply(True, status=mgr.status())
            elif cmd == "blink":
                from bt_strips import blink_identify

                role = str(req.get("role", "ceiling"))
                color = req.get("color") or [0, 180, 255]
                blink_identify(role, tuple(int(x) for x in color))
                _reply(True, status=mgr.status())
            elif cmd == "quit":
                mgr.disconnect_all()
                _reply(True, bye=True)
                return 0
            else:
                _reply(False, error=f"unknown cmd {cmd}")
        except Exception as exc:
            try:
                from bt_strips import _friendly_bt_error

                err = _friendly_bt_error(exc)
            except Exception:
                err = str(exc)
            _reply(False, error=err, trace=traceback.format_exc()[-500:])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
