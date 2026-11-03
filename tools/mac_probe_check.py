#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 端的手动体检小工具（Linux 也可跑）：

    python3 tools/mac_probe_check.py

检查三件事：
1. 当前解释器/平台；
2. kbsync 能不能读到光标位置、空闲时间、鼠标按键；
3. 5 秒内能不能把光标移动识别成「活动事件」。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kbsync  # noqa: E402


def main() -> int:
    print("platform: %s  python: %s" % (sys.platform, sys.version.split()[0]))
    probe = kbsync.make_probe()
    try:
        x, y = probe.cursor()
        print("cursor: (%.0f, %.0f)" % (x, y))
    except Exception as exc:  # noqa: BLE001
        print("cursor: 读取失败 - %s" % exc)
        return 2
    idle = probe.seconds_since_last_input()
    print("seconds_since_last_input: %s" % ("未知" if idle is None else "%.2f" % idle))
    try:
        print("button_down: %s" % probe.button_down())
    except Exception as exc:  # noqa: BLE001
        print("button_down: 读取失败 - %s" % exc)

    detector = kbsync.ActivityDetector()
    print("接下来 5 秒请晃动鼠标……")
    hits = 0
    started = time.monotonic()
    while time.monotonic() - started < 5.0:
        t = time.monotonic()
        try:
            x, y = probe.cursor()
            if detector.feed(t, x, y, probe.button_down(), probe.seconds_since_last_input()):
                hits += 1
        except Exception as exc:  # noqa: BLE001
            print("采样失败: %s" % exc)
            return 2
        time.sleep(0.016)
    print("activity events: %d" % hits)
    return 0 if hits else 1


if __name__ == "__main__":
    sys.exit(main())
