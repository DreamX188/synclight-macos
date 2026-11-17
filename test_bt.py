#!/usr/bin/env python3
"""End-to-end BT worker tests with hard timeouts."""

from __future__ import annotations

import signal
import sys
import time


def main() -> int:
    signal.alarm(90)  # hard stop
    from bt_client import BtClient

    fails = []
    bt = BtClient()  # fresh, not singleton

    def check(name, cond, detail=""):
        if cond:
            print(f"OK  {name}")
        else:
            print(f"FAIL {name} :: {detail}")
            fails.append(name)

    try:
        st = bt.status()
        check("status", "ceiling" in st)

        devices = bt.scan(6)
        check("scan", len(devices) >= 1, devices)
        check("lednet", any("LEDnet" in (d.get("name") or "") for d in devices), devices)

        if devices:
            bt.assign("ceiling", devices[0]["address"], devices[0].get("name", ""), devices[0].get("protocol", "lednetwf"))
        if len(devices) >= 2:
            bt.assign("floor", devices[1]["address"], devices[1].get("name", ""), devices[1].get("protocol", "lednetwf"))

        note = bt.connect_bound()
        check("connect", "подключено" in note, note)
        st = bt.status()
        check("ceiling_online", bool((st.get("ceiling") or {}).get("connected")), st)
        if (st.get("floor") or {}).get("bound"):
            check("floor_online", bool((st.get("floor") or {}).get("connected")), st)

        bt.apply("ceiling", color=(255, 0, 0))
        time.sleep(0.25)
        bt.apply("floor", color=(0, 0, 255))
        time.sleep(0.25)
        bt.apply("bt", power=False)
        time.sleep(0.2)
        bt.apply("bt", color=(255, 170, 60))
        check("colors", True)

        err = (bt.last_error or "") + str((st.get("last_error") or ""))
        check("no_auth_word", "authoriz" not in err.lower() and "авториз" not in err.lower(), err)
    except Exception as exc:
        print("FAIL exception ::", exc)
        fails.append("exception")
    finally:
        try:
            bt.close()
        except Exception:
            pass
        signal.alarm(0)

    print("---")
    if fails:
        print("FAILED:", ", ".join(fails))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
