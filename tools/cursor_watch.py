#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采样光标 N 秒，用来确认 kbsync 能否看到鼠标移动。

    python3 tools/cursor_watch.py [秒数，默认 6]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kbsync  # noqa: E402


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    probe = kbsync.make_probe()
    start = time.monotonic()
    last = None
    moves = 0
    while time.monotonic() - start < seconds:
        try:
            x, y = probe.cursor()
        except Exception as exc:  # noqa: BLE001
            print("cursor error:", exc)
            return 2
        if last is not None and (abs(x - last[0]) > 1 or abs(y - last[1]) > 1):
            moves += 1
        last = (x, y)
        idle = probe.seconds_since_last_input()
        print("%5.2f  (%6.0f,%6.0f)  idle=%s" % (
            time.monotonic() - start, x, y,
            "?" if idle is None else "%.2f" % idle,
        ))
        time.sleep(0.25)
    print("cursor moves detected:", moves)
    return 0 if moves else 1


if __name__ == "__main__":
    sys.exit(main())
