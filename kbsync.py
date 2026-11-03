#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kbsync —— 让一把「多主机（Easy-Switch）」键盘跟随正在使用的那只鼠标。

背景
----
一台 MX Keys 可以用 Easy-Switch 同时配对 Windows 和 macOS 两台机器，但同一时刻
只有「当前主机」能收到它的按键。Logitech Flow 能自动跟随鼠标，可惜只认 Flow
鼠标（MX 系列）；G 系列（G Pro 等）走 G HUB，不能当触发源。

本工具绕开 Flow：两台机器各跑一个 kbsync，局域网内互相通知「我这边鼠标动了」，
再由「当前持有键盘」的那台通过 HID++ 的 CHANGE_HOST 指令把键盘推到对方通道。

    为什么只能「推」不能「拉」：
    多主机键盘同一时刻只对当前主机开放设备，非当前主机根本发不出 HID++ 指令。
    所以收到请求的一方必须自己就是键盘所在的那台，才能把键盘推走。

    状态机（每台机器各一份）
    ------------------------
    HOST      键盘在我这
    NOT_HOST  键盘不在我这
    UNKNOWN   启动时探测不到（键盘没开/没连上）

    本地鼠标活动时：
        HOST     -> 什么都不做（低频做一次复核，防止状态过期）
        NOT_HOST -> 向对端请求「如果你有键盘，请把键盘推到我这个通道」
    收到对端请求时：
        NOT_HOST -> 直接回 not_host（我没键盘，推不了）
        HOST     -> 本机若正在被使用（打字保护）回 busy；否则执行 mxswitch 推走

依赖
----
* Python 3.9+（只用标准库，无 pip 依赖）
* mxswitch —— 真正发 HID++ CHANGE_HOST 指令的小工具，MIT：
  https://github.com/marcocosta97/mxswitch
  - Windows: 直接用仓库里的 windows/mxswitch.ps1（无需编译）
  - macOS  : 在仓库里 `make` 编译出 ./mxswitch 二进制

用法
----
    python kbsync.py --config config.json        # 正常运行
    python kbsync.py --config config.json --probe    # 体检：通道/设备/对端连通性
    python kbsync.py --config config.json --self-test # 自检：鼠标活动检测
    python kbsync.py --config config.json --dry-run   # 只记日志，不真的切键盘
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

APP_NAME = "kbsync"
APP_VERSION = "0.1.0"
PROTOCOL_VERSION = 1

log = logging.getLogger(APP_NAME)


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
class ConfigError(Exception):
    pass


@dataclass
class Config:
    # 身份
    machine: str = "win"            # 任意名字，仅用于日志和拒绝自己发来的包
    my_channel: int = 1             # 这台机器对应键盘上的 Easy-Switch 通道号（1/2/3）
    peer: str = ""                  # 对端 IP 或主机名
    port: int = 52845               # 两端同一个 TCP 端口
    secret: str = ""                # 两端必须一致；留空则不校验（不建议）

    # 后端：auto = Windows 用原生 HID++，macOS 用 mxswitch；也可强制 hidpp / mxswitch
    backend: str = "auto"

    # macOS 原生 HID++ 助手（mxhid.c 编译出来的二进制），常驻、毫秒级
    mxhid_path: str = ""
    mxhid_probe_timeout_ms: int = 150   # mxhid 助手的探测超时（越小发现越快）

    # mxswitch
    mxswitch: str = ""              # mxswitch.ps1 / 编译出的 mxswitch 二进制 / mxswitch.py
    device_name: str = ""           # 可选：mxswitch --name 过滤（有多把 ChangeHost 键盘时才需要）
    mxswitch_timeout_s: float = 10.0
    switch_retries: int = 5         # 键盘休眠时第一次探测常常失败，多试几次能把它叫醒
    switch_retry_delay_ms: int = 400

    # 保活：HOST 状态下定期给键盘发一帧 getHostInfo，尽量别让它睡死
    keepalive_ms: int = 4000
    switch_cooldown_ms: int = 2500   # 一次切换后多久内不再响应/发起新的切换
    info_attempts: int = 3           # 探测键盘存在性的尝试次数（macOS 上 mxswitch 慢，可设 1）

    # 原生 HID++ 的发现结果缓存（默认 ~/.kbsync/hidpp-cache.json）
    cache_file: str = ""

    # 鼠标活动检测
    poll_hz: int = 60
    activity_move_px: float = 16.0  # 窗口内累计位移超过多少像素算「在用这只鼠标」
    activity_window_ms: int = 350
    use_input_recency: bool = False  # 兜底：仅在「确实有位移」时用系统输入时间确认（防抖动误触）

    # 协商
    min_send_interval_ms: int = 350
    max_send_interval_ms: int = 3000
    host_recheck_ms: int = 15000    # HOST 状态下多久复核一次（防止状态过期）
    release_idle_ms: int = 0        # >0 时启用打字保护（注意：会被鼠标抖动误触发，默认关闭）

    # 日志
    log_level: str = "INFO"
    log_file: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Config":
        if not isinstance(data, dict):
            raise ConfigError("配置文件顶层必须是一个 JSON 对象")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError("配置文件里有不认识的字段: %s" % ", ".join(unknown))
        cfg = cls(**data)  # type: ignore[arg-type]
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            # utf-8-sig：兼容 Windows 记事本/PowerShell 写出的带 BOM 文件
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConfigError("读不到配置文件 %s: %s" % (path, exc)) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError("配置文件不是合法 JSON (%s): %s" % (path, exc)) from exc
        return cls.from_dict(data)

    def validate(self) -> None:
        problems: List[str] = []
        if not str(self.machine).strip():
            problems.append("machine 不能为空")
        if self.my_channel not in (1, 2, 3):
            problems.append("my_channel 必须是 1/2/3（键盘上的通道编号）")
        if not str(self.peer).strip():
            problems.append("peer 必须填对端 IP 或主机名")
        if not (1 <= int(self.port) <= 65535):
            problems.append("port 必须在 1-65535 之间")
        if not (5 <= int(self.poll_hz) <= 240):
            problems.append("poll_hz 建议在 5-240 之间")
        if float(self.activity_move_px) <= 0:
            problems.append("activity_move_px 必须大于 0")
        if int(self.activity_window_ms) <= 0:
            problems.append("activity_window_ms 必须大于 0")
        backend = (self.backend or "auto").strip().lower()
        if backend not in ("auto", "hidpp", "mxhid", "mxswitch"):
            problems.append("backend 只能是 auto / hidpp / mxhid / mxswitch")
        if not self.mxswitch and backend not in ("hidpp", "mxhid"):
            problems.append("mxswitch 必须指向 mxswitch.ps1 / mxswitch 二进制（backend=hidpp/mxhid 时可不填）")
        if int(self.switch_retries) < 1:
            problems.append("switch_retries 至少为 1")
        if int(self.keepalive_ms) < 0:
            problems.append("keepalive_ms 不能为负")
        if problems:
            raise ConfigError("配置有问题:\n  - " + "\n  - ".join(problems))

    def summary(self) -> str:
        return (
            "machine=%s my_channel=%d peer=%s:%d backend=%s mxswitch=%s"
            % (self.machine, self.my_channel, self.peer, self.port,
               self.backend, self.mxswitch or "<none>")
        )


# ----------------------------------------------------------------------------
# 平台相关的输入探针
# ----------------------------------------------------------------------------
class InputProbe:
    """读取光标位置 / 鼠标按键 / 距上次输入的时间。"""

    def cursor(self) -> Tuple[float, float]:
        raise NotImplementedError

    def button_down(self) -> bool:
        return False

    def seconds_since_last_input(self) -> Optional[float]:
        """拿不到返回 None（此时引擎不会用打字保护拦切换）。"""
        return None

    def warp(self, x: int, y: int) -> None:
        """移动光标，仅用于 --self-test。"""
        raise NotImplementedError


class WindowsProbe(InputProbe):
    def __init__(self) -> None:
        from ctypes import wintypes  # 只在 Windows 上导入

        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self._user32.GetCursorPos.restype = wintypes.BOOL
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self._user32.SetCursorPos.restype = wintypes.BOOL
        self._kernel32.GetTickCount.argtypes = []
        self._kernel32.GetTickCount.restype = wintypes.DWORD

    def cursor(self) -> Tuple[float, float]:
        pt = self._wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(pt)):
            raise OSError("GetCursorPos 失败")
        return float(pt.x), float(pt.y)

    def button_down(self) -> bool:
        for vk in (0x01, 0x02, 0x04):  # 左/右/中键
            if self._user32.GetAsyncKeyState(vk) & 0x8000:
                return True
        return False

    def seconds_since_last_input(self) -> Optional[float]:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        self._user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        self._user32.GetLastInputInfo.restype = ctypes.c_bool
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = self._kernel32.GetTickCount()
        # GetLastInputInfo 用的是 32 位 tick，回绕时也能算对
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0

    def warp(self, x: int, y: int) -> None:
        self._user32.SetCursorPos(int(x), int(y))


class MacProbe(InputProbe):
    """用 CoreGraphics 读光标 / 按键 / 空闲时间；不需要 Accessibility 就能读位置。

    - CGEventGetLocation 只是读位置，不需要额外权限。
    - CGEventSourceSecondsSinceLastEventType 用来实现打字保护；万一系统不给，
      返回 None，引擎会跳过这道保护而不是永远拦着。
    """

    kCGEventSourceStateCombinedSessionState = 0
    kCGAnyInputEventType = 0xFFFFFFFF
    kCGMouseButtonLeft = 0

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    def __init__(self) -> None:
        self._cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        self._cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        self._cg.CGEventCreate.restype = ctypes.c_void_p
        self._cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        self._cg.CGEventGetLocation.restype = MacProbe.CGPoint
        self._cg.CGEventSourceButtonState.argtypes = [ctypes.c_int32, ctypes.c_int32]
        self._cg.CGEventSourceButtonState.restype = ctypes.c_bool
        self._cg.CGEventSourceSecondsSinceLastEventType.argtypes = [
            ctypes.c_int32,
            ctypes.c_uint32,
        ]
        self._cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        self._cg.CGWarpMouseCursorPosition.argtypes = [MacProbe.CGPoint]
        self._cg.CGWarpMouseCursorPosition.restype = ctypes.c_int32
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None

        self._button_state = self._probe_button_state

    def cursor(self) -> Tuple[float, float]:
        event = self._cg.CGEventCreate(None)
        if not event:
            raise OSError("CGEventCreate 失败")
        try:
            point = self._cg.CGEventGetLocation(event)
        finally:
            self._cf.CFRelease(event)
        return float(point.x), float(point.y)

    def _probe_button_state(self) -> bool:
        try:
            for button in (0, 1, 2):  # 左/右/中
                if self._cg.CGEventSourceButtonState(
                    self.kCGEventSourceStateCombinedSessionState, button
                ):
                    return True
            return False
        except Exception:  # noqa: BLE001 - 权限不足时宁可认为没按键
            self._button_state = lambda: False  # type: ignore[assignment]
            return False

    def button_down(self) -> bool:
        return self._button_state()

    def seconds_since_last_input(self) -> Optional[float]:
        try:
            value = float(
                self._cg.CGEventSourceSecondsSinceLastEventType(
                    self.kCGEventSourceStateCombinedSessionState,
                    self.kCGAnyInputEventType,
                )
            )
        except Exception:  # noqa: BLE001
            return None
        return max(0.0, value)

    def warp(self, x: int, y: int) -> None:
        point = MacProbe.CGPoint(float(x), float(y))
        self._cg.CGWarpMouseCursorPosition(point)


def make_probe() -> InputProbe:
    if sys.platform == "win32":
        return WindowsProbe()
    if sys.platform == "darwin":
        return MacProbe()
    raise RuntimeError("kbsync 目前只支持 Windows 和 macOS（当前平台: %s）" % sys.platform)


# ----------------------------------------------------------------------------
# 鼠标活动检测（纯逻辑，方便单元测试）
# ----------------------------------------------------------------------------
class ActivityDetector:
    """把高频光标采样变成「用户正在用这只鼠标」的稀疏事件。

    触发条件（满足其一）：
      * 按下鼠标键的瞬间；
      * 滑动窗口内的光标累计位移 >= move_px；
      * 系统「距上次输入」显示刚刚有输入（use_input_recency，兜底 raw input 游戏）。
    """

    def __init__(
        self,
        move_px: float = 16.0,
        window_s: float = 0.35,
        use_input_recency: bool = True,
        input_recency_s: float = 0.08,
    ) -> None:
        self.move_px = float(move_px)
        self.window_s = float(window_s)
        self.use_input_recency = use_input_recency
        self.input_recency_s = float(input_recency_s)
        self._samples: Deque[Tuple[float, float, float]] = collections.deque()
        self._prev_button = False
        self._last_trigger = float("-inf")
        self.last_strength = "move"
        self.last_detail = ""

    def feed(
        self,
        t: float,
        x: float,
        y: float,
        button_down: bool = False,
        seconds_since_input: Optional[float] = None,
    ) -> bool:
        clicked = bool(button_down) and not self._prev_button
        self._prev_button = bool(button_down)
        self._samples.append((t, x, y))
        # 保留窗口外一点点，避免长间隔后丢掉最后一段位移
        while len(self._samples) > 2 and t - self._samples[0][0] > self.window_s + 0.1:
            self._samples.popleft()

        triggered = False
        detail = ""
        if clicked:
            triggered = True
            detail = "click"
        else:
            points = [s for s in self._samples if t - s[0] <= self.window_s]
            travelled = 0.0
            for prev, cur in zip(points, points[1:]):
                travelled += math.hypot(cur[1] - prev[1], cur[2] - prev[2])
            if travelled >= self.move_px:
                triggered = True
                detail = "move %.0fpx" % travelled
            elif (
                self.use_input_recency
                and travelled >= 2.0
                and seconds_since_input is not None
                and seconds_since_input <= self.input_recency_s
            ):
                triggered = True

        if not triggered:
            return False
        # 去抖：200ms 内只报一次，避免每帧都叫醒引擎
        if t - self._last_trigger < 0.2:
            return False
        self._last_trigger = t
        self.last_strength = "click" if clicked else "move"
        self.last_detail = detail
        return True


# ----------------------------------------------------------------------------
# mxswitch 封装
# ----------------------------------------------------------------------------
@dataclass
class ControllerInfo:
    ok: bool                 # mxswitch 退出码为 0
    device_ok: bool          # 根据输出判断确实是这把键盘
    channel: Optional[int]   # 解析出的当前通道
    raw: str = ""


# 人类可读输出里形如 "channels   : 3, currently on 2"，先抓 "currently on N"
_CURRENT_CHANNEL_RE = re.compile(r"currently\s+on\s+(\d)", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"channel[^0-9]{0,20}(\d)", re.IGNORECASE)


class KeyboardController:
    #: 能否可靠判断「设备真的在我这」。
    #: 原生 HID++ 能读回当前通道 -> True；
    #: mxswitch / mxhid 只能看「设备是否应答」，而接收器会替「已配对但不在本机」
    #: 的设备应答，存在假阳性 -> False。
    reliable_state = True

    def info(self) -> ControllerInfo:
        raise NotImplementedError

    def switch_to(self, channel: int) -> bool:
        raise NotImplementedError

    def keepalive(self) -> bool:
        """给键盘发一帧无害请求，尽量阻止它休眠。默认不做。"""
        return False


class MxSwitchController(KeyboardController):
    reliable_state = False

    """调用 mxswitch 发 HID++ 指令。

    Windows 上 mxswitch 是 .ps1，必须用 powershell 跑；macOS 直接跑二进制。
    也兼容仓库里的 python/mxswitch.py（用当前解释器跑）。
    """

    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.path = str(cfg.mxswitch)
        self.kind = self._detect_kind(self.path)
        self._check_path()

    @staticmethod
    def _detect_kind(path: str) -> str:
        lowered = path.lower()
        if lowered.endswith(".ps1"):
            return "ps1"
        if lowered.endswith(".py"):
            return "py"
        return "exe"

    def _check_path(self) -> None:
        if self.dry_run:
            return
        p = Path(self.path)
        if not p.exists():
            raise ConfigError("找不到 mxswitch: %s（见 README 的安装一节）" % p)

    def _base_cmd(self) -> List[str]:
        if self.kind == "ps1":
            return [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                self.path,
            ]
        if self.kind == "py":
            return [sys.executable, self.path]
        return [self.path]

    def _name_flag(self) -> List[str]:
        if not self.cfg.device_name:
            return []
        return ["-Name", self.cfg.device_name] if self.kind == "ps1" else [
            "--name",
            self.cfg.device_name,
        ]

    def _run(self, args: List[str]) -> Tuple[int, str]:
        cmd = self._base_cmd() + args
        kwargs = dict(
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=self.cfg.mxswitch_timeout_s,
        )
        if os.name == "nt":
            # pythonw / 后台运行时不要闪黑窗
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(cmd, **kwargs)  # noqa: S603 - 路径来自本地配置
        except FileNotFoundError as exc:
            return 1, "命令不存在: %s" % exc
        except subprocess.TimeoutExpired:
            return 1, "mxswitch 超时"
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()

    # -- 对外接口 ---------------------------------------------------------
    def _matches_device(self, product: str) -> bool:
        if not self.cfg.device_name:
            return True
        return self.cfg.device_name.lower() in product.lower()

    def _info_json(self) -> Optional[ControllerInfo]:
        """三种 mxswitch 实现都支持 JSON 输出，比解析人类可读文本可靠。"""
        args = ["-Info", "-Json"] if self.kind == "ps1" else ["--json", "--info"]
        code, out = self._run(args)
        if code != 0:
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return None
        products = [str(d.get("product", "")) for d in data if isinstance(d, dict)]
        channel: Optional[int] = None
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("channel"), int):
                channel = int(item["channel"])
                break
        # 找到 ChangeHost 设备即说明键盘在本机（部分接收器不回复 getHostInfo，
        # 读不到通道也正常，通道由调用方按本机配置推断）
        device_ok = True
        if products and not any(self._matches_device(p) for p in products):
            device_ok = False
        return ControllerInfo(ok=True, device_ok=device_ok, channel=channel, raw=out)

    def info(self) -> ControllerInfo:
        if self.dry_run:
            return ControllerInfo(ok=False, device_ok=False, channel=None, raw="dry-run")
        info_args = ["-Info"] if self.kind == "ps1" else ["--info"]
        attempts = max(1, int(self.cfg.info_attempts))
        last_raw = ""
        for attempt in range(1, attempts + 1):
            if self.kind == "ps1":
                parsed = self._info_json()
                if parsed is not None:
                    log.debug("mxswitch -Info -Json: %s",
                              parsed.raw.replace("\n", " ")[:200])
                    return parsed
            code, out = self._run(info_args)
            last_raw = out
            if code == 0:
                channel: Optional[int] = None
                match = _CURRENT_CHANNEL_RE.search(out) or _CHANNEL_RE.search(out)
                if match:
                    try:
                        channel = int(match.group(1))
                    except ValueError:
                        channel = None
                # 找到设备即为宿主；通道读不到时按本机配置推断
                device_ok = True
                if self.cfg.device_name and not self._matches_device(out):
                    device_ok = False
                return ControllerInfo(ok=True, device_ok=device_ok, channel=channel, raw=out)
            log.debug("mxswitch info 第 %d/%d 次失败 (exit=%d): %s",
                      attempt, attempts, code, out[:160])
            if attempt < attempts:
                time.sleep(0.25)
        return ControllerInfo(ok=False, device_ok=False, channel=None, raw=last_raw)

    def keepalive(self) -> bool:
        # mxswitch 每次调用 1~5 秒，只在 HOST 时低频调用
        return self.info().device_ok

    def switch_to(self, channel: int) -> bool:
        if not 1 <= int(channel) <= 3:
            log.error("非法通道号: %s", channel)
            return False
        if self.dry_run:
            log.info("[dry-run] 本应把键盘切到通道 %d", channel)
            return True
        args = self._name_flag() + (
            ["-Channel", str(int(channel))] if self.kind == "ps1" else [str(int(channel))]
        )
        attempts = max(1, int(self.cfg.switch_retries))
        delay = max(0, int(self.cfg.switch_retry_delay_ms)) / 1000.0
        for attempt in range(1, attempts + 1):
            code, out = self._run(args)
            if code == 0:
                if attempt > 1:
                    log.debug("mxswitch 第 %d/%d 次尝试切换成功", attempt, attempts)
                return True
            log.debug("mxswitch 切换失败 (exit=%d, 第 %d/%d 次): %s",
                      code, attempt, attempts, out[:160])
            if attempt < attempts:
                time.sleep(delay)
        return False


class HidppKeyboardController(KeyboardController):
    """Windows 原生 HID++（hidpp_win.WindowsHidppController）的适配层。"""

    def __init__(self, dry_run: bool = False, cache_file: str = "") -> None:
        from hidpp_win import WindowsHidppController  # 仅在 Windows 上导入

        cache_path = Path(cache_file) if cache_file else None
        self._impl = WindowsHidppController(dry_run=dry_run, cache_path=cache_path)

    def info(self) -> ControllerInfo:
        found, channel = self._impl.info()
        return ControllerInfo(
            ok=found, device_ok=found, channel=channel,
            raw="hidpp native" if found else "",
        )

    def switch_to(self, channel: int) -> bool:
        return self._impl.switch_to(channel)

    def keepalive(self) -> bool:
        return self._impl.keepalive()

    def close(self) -> None:
        self._impl.close()



class MxhidController(KeyboardController):
    reliable_state = False

    """macOS 原生 HID++ 助手（tools/mxhid.c 的编译产物）的客户端。

    助手常驻打开键盘设备，每 500ms 自动 ping 一次：
      * 键盘不会休眠（蓝牙链路保持），随时可以被切走；
      * 单次 ping / 切换都是毫秒级，不像 mxswitch 那样要 1~5 秒。
    """

    def __init__(self, path: str, timeout: float = 20.0, probe_timeout_ms: int = 400,
                 my_channel: int = 1) -> None:
        self.path = path
        self.timeout = timeout
        self.probe_timeout_ms = probe_timeout_ms
        self.my_channel = my_channel
        self._proc = None
        self._lock = threading.Lock()
        # 助手在做后台全量扫描时可能十几秒才回一条；超时的请求它的回包还在路上，
        # 记下来，下一次读的时候先丢弃，避免把过期回包当成新回包。
        self._pending_replies = 0

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        try:
            self._proc = subprocess.Popen(
                [self.path, "--probe-timeout-ms", str(int(self.probe_timeout_ms))],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            log.error("无法启动 mxhid 助手 %s: %s", self.path, exc)
            self._proc = None
            return False
        return True

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.write("quit\n")
                        self._proc.stdin.flush()
                except OSError:
                    pass
                try:
                    self._proc.terminate()
                except OSError:
                    pass
                self._proc = None

    def _call(self, command: str) -> Dict[str, object]:
        import select
        with self._lock:
            if not self._ensure():
                return {"ok": 0}
            proc = self._proc
            assert proc is not None and proc.stdin and proc.stdout
            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
            except OSError:
                self._proc = None
                return {"ok": 0}
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 助手还在忙（后台扫描），回包在路上：记一笔，下次先丢弃。
                    self._pending_replies += 1
                    return {"ok": 0}
                ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
                if not ready:
                    continue
                line = proc.stdout.readline()
                if not line:
                    self._proc = None
                    return {"ok": 0}
                line = line.strip()
                if not line:
                    continue
                if self._pending_replies > 0:
                    self._pending_replies -= 1
                    continue          # 过期回包，丢弃
                try:
                    reply = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(reply, dict):
                    return reply
                return {"ok": 0}

    def info(self) -> ControllerInfo:
        reply = self._call("ping")
        if int(reply.get("ok", 0)) != 1:
            return ControllerInfo(ok=False, device_ok=False, channel=None, raw=str(reply))
        channel = reply.get("channel")
        if not isinstance(channel, int) or channel <= 0:
            # 接收器不回复 getHostInfo 时读不到通道；
            # 设备既然在本机可见，就只能是本机对应的那个通道。
            channel = self.my_channel
        return ControllerInfo(
            ok=True, device_ok=True, channel=int(channel),
            raw=json.dumps(reply, ensure_ascii=False),
        )

    def switch_to(self, channel: int) -> bool:
        if not 1 <= int(channel) <= 3:
            return False
        reply = self._call("switch %d" % int(channel))
        if int(reply.get("ok", 0)) != 1:
            return False
        # mxswitch 用「设备不再应答」判定成功，但设备休眠时写入会被丢弃、
        # 同样表现为「不再应答」——所以再复核一次：设备还在就说明根本没切成。
        for _ in range(3):
            time.sleep(0.15)
            if not self.info().device_ok:
                return True
        return False

    def keepalive(self) -> bool:
        # 助手自己每 500ms 保活，这里只确认它还活着
        return self._proc is not None and self._proc.poll() is None

def make_controller(cfg: Config, dry_run: bool = False) -> KeyboardController:
    backend = (cfg.backend or "auto").strip().lower()
    if backend in ("auto", "hidpp") and sys.platform == "win32" and not dry_run:
        try:
            return HidppKeyboardController(cache_file=cfg.cache_file)
        except Exception as exc:  # noqa: BLE001 - 原生失败就退回 mxswitch
            if backend == "hidpp":
                raise
            log.warning("原生 HID++ 不可用（%s），退回 mxswitch", exc)
    if backend in ("auto", "mxhid") and sys.platform == "darwin" and not dry_run:
        helper = cfg.mxhid_path or str(Path.home() / "kbsync" / "tools" / "mxhid")
        if Path(helper).exists():
            try:
                return MxhidController(helper, probe_timeout_ms=cfg.mxhid_probe_timeout_ms,
                                      my_channel=cfg.my_channel)
            except Exception as exc:  # noqa: BLE001
                if backend == "mxhid":
                    raise
                log.warning("mxhid 助手不可用（%s），退回 mxswitch", exc)
        elif backend == "mxhid":
            raise ConfigError("找不到 mxhid 助手: %s（先编译 tools/mxhid.c）" % helper)
    return MxSwitchController(cfg, dry_run=dry_run)


# ----------------------------------------------------------------------------
# 网络：一个极简的 TCP 请求/响应（可靠 + 同步拿到结果）
# ----------------------------------------------------------------------------
class TcpLink:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def request(self, payload: Dict[str, object], timeout: Optional[float] = None) -> Dict[str, object]:
        connect_timeout = 1.0
        read_timeout = timeout if timeout is not None else 8.0
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with socket.create_connection(
                (self.cfg.peer, int(self.cfg.port)), timeout=connect_timeout
            ) as sock:
                sock.settimeout(read_timeout)
                sock.sendall(data)
                buffer = b""
                while b"\n" not in buffer and len(buffer) < 65536:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                line = buffer.split(b"\n", 1)[0].strip()
                if not line:
                    return {"result": "error"}
                reply = json.loads(line.decode("utf-8", "replace"))
                return reply if isinstance(reply, dict) else {"result": "error"}
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("请求对端失败: %s", exc)
            return {"result": "error"}

    def ping(self) -> Tuple[bool, str]:
        reply = self.request(
            {"v": PROTOCOL_VERSION, "type": "ping", "machine": self.cfg.machine,
             "secret": self.cfg.secret},
            timeout=2.0,
        )
        if reply.get("result") == "pong":
            return True, "ok"
        return False, str(reply.get("result", "no reply"))


# ----------------------------------------------------------------------------
# 状态机
# ----------------------------------------------------------------------------
class State(str, Enum):
    HOST = "host"
    NOT_HOST = "not_host"
    UNKNOWN = "unknown"


class Engine:
    """串行处理事件：本机活动 / 对端请求。所有控制器调用都发生在同一个线程里。"""

    def __init__(
        self,
        cfg: Config,
        probe: InputProbe,
        controller: KeyboardController,
        link: TcpLink,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.probe = probe
        self.controller = controller
        self.link = link
        self.now = now
        self.state = State.UNKNOWN
        self._last_send = float("-inf")
        self._last_probe = float("-inf")
        self._send_failures = 0
        self._last_keepalive = self.now()
        self._suppress_until = float("-inf")
        self._activity_streak: List[float] = []
        self._streak_lock = threading.Lock()
        self._host_miss = 0
        self._host_hits = 0
        self._verify_until = 0.0
        self._verify_attempts = 0
        self._preverify_miss = 0
        self._initial_probe()

    # -- 保活 -------------------------------------------------------------
    def tick(self) -> None:
        """周期调用：切换后延迟复核 + HOST 保活。"""
        now = self.now()
        if self.state is State.HOST and self._verify_until:
            if now >= self._verify_until:
                self._verify_attempts += 1
                info = self.controller.info()
                if info.device_ok:
                    self._verify_until = 0.0
                    self._last_probe = now
                    log.debug("切换后复核通过（第 %d 次）", self._verify_attempts)
                elif self._verify_attempts >= 3:
                    self._verify_until = 0.0
                    self.state = State.NOT_HOST
                    log.info("切换后复核 3 次仍探测不到，转入 NOT_HOST")
                else:
                    self._verify_until = now + 1.0
        interval = self.cfg.keepalive_ms / 1000.0
        if interval <= 0:
            return
        if self.state is State.NOT_HOST:
            # 非宿主时也定期探测：键盘被切过来后自动认领，不必等用户再戳一下
            if now - self._last_probe >= interval:
                self._last_probe = now
                info = self.controller.info()
                if info.device_ok:
                    self._host_hits += 1
                    if self._host_hits >= 2:
                        self.state = State.HOST
                        log.info("定时自检确认键盘在本机 -> HOST（channel=%s）", info.channel)
                else:
                    self._host_hits = 0
            return
        now = self.now()
        if now - self._last_keepalive < interval:
            return
        self._last_keepalive = now
        if not self.controller.keepalive():
            log.debug("keepalive 无响应（键盘可能已休眠）")

    # -- 启动探测 ---------------------------------------------------------
    def _initial_probe(self) -> None:
        info = self.controller.info()
        self._last_probe = self.now()
        self.state = State.HOST if info.device_ok else State.NOT_HOST
        log.info(
            "启动探测: 键盘%s本机 (state=%s, channel=%s)",
            "在" if info.device_ok else "不在",
            self.state.value,
            info.channel,
        )

    # -- 本机鼠标活动 -----------------------------------------------------
    def note_activity(self, strength: str = "move") -> None:
        """由采样线程直接调用：记录活动次数，不经过队列合并。"""
        now = self.now()
        with self._streak_lock:
            self._activity_streak.append(now)
            self._activity_streak = [t for t in self._activity_streak if now - t <= 1.5]
        _ = strength

    def maybe_self_check(self) -> None:
        """任何输入（包括打字）后调用：若处于 NOT_HOST，廉价自检一次是否其实持有键盘。"""
        if self.state is not State.NOT_HOST:
            return
        now = self.now()
        if now - self._last_probe < 1.0:
            return
        self._last_probe = now
        info = self.controller.info()
        if info.device_ok:
            self._host_hits += 1
            if self._host_hits >= 2:
                self.state = State.HOST
                log.info("输入活动自检确认键盘在本机 -> HOST（channel=%s）", info.channel)
        else:
            self._host_hits = 0

    def on_local_activity(self, strength: str = "click") -> None:
        now = self.now()
        if now < self._suppress_until:
            log.debug("切换冷却中（还剩 %.1fs），忽略本机活动",
                      self._suppress_until - now)
            return
        if self.state is State.NOT_HOST:
            # 只有「持续移动」或「点击」才去要键盘，轻碰一下不算
            with self._streak_lock:
                recent = [t for t in self._activity_streak if now - t <= 1.5]
            if strength != "click" and len(recent) < 2:
                log.debug("本机活动不够持续（%d/2），先不请求切换", len(recent))
                return
            # 启动时键盘可能正在休眠；本机有活动就低频自检一次，
            # 发现它其实在本机就纠正状态（否则两端会一直互相说 not_host）。
            if now - self._last_probe > 5.0:
                self._last_probe = now
                info = self.controller.info()
                if info.device_ok:
                    self.state = State.HOST
                    log.info("自检发现键盘在本机 -> HOST")
                    return
        if self.state is State.HOST:
            # 低频复核，防止「键盘其实早被手动/对方切走了」的过期状态。
            if now - self._last_probe < self.cfg.host_recheck_ms / 1000.0:
                return
            self._last_probe = now
            info = self.controller.info()
            if info.device_ok:
                self._host_miss = 0
                return
            # 键盘休眠时 probing 会假阴性；连续 2 次探测不到才翻转状态
            self._host_miss += 1
            if self._host_miss < 2:
                log.debug("复核没探测到键盘（第 %d 次），暂保持 HOST", self._host_miss)
                return
            self._host_miss = 0
            log.info("复核连续失败，转入 NOT_HOST")
            self.state = State.NOT_HOST

        interval = self.cfg.min_send_interval_ms / 1000.0
        if self._send_failures:
            backoff = min(self._send_failures, 6)
            interval = min(
                self.cfg.max_send_interval_ms / 1000.0, interval * (2 ** backoff)
            )
        if now - self._last_send < interval:
            return
        self._last_send = now
        log.debug("向对端请求交接（本机通道 %d）", self.cfg.my_channel)

        reply = self.link.request(
            {
                "v": PROTOCOL_VERSION,
                "type": "active",
                "machine": self.cfg.machine,
                "channel": self.cfg.my_channel,
                "secret": self.cfg.secret,
            }
        )
        result = str(reply.get("result", "error"))
        if result == "switched":
            self.state = State.HOST
            self._last_probe = now
            self._send_failures = 0
            self._suppress_until = now + self.cfg.switch_cooldown_ms / 1000.0
            self._last_probe = now
            self._host_miss = 0
            self._verify_until = now + 2.0   # 给键盘 2 秒连接时间再复核
            self._verify_attempts = 0
            log.info("键盘已切到本机（通道 %d），冷却 %.0fs",
                     self.cfg.my_channel, self.cfg.switch_cooldown_ms / 1000.0)
        elif result == "not_host":
            # 对端没键盘。别急着认定自己是宿主，先探测一下本机。
            info = self.controller.info()
            if info.device_ok:
                self.state = State.HOST
                self._last_probe = now
                self._send_failures = 0
                log.info("对端无键盘，本机探测到设备 -> HOST")
            else:
                log.debug("对端无键盘，本机也探测不到（键盘可能休眠/在别处）")
        elif result == "busy":
            self._send_failures = 0
            self._last_send = now + 0.8
            log.debug("对端正在使用中/冷却中，稍后重试")
        else:
            self._send_failures += 1
            log.debug("移交请求失败（%s，第 %d 次）", result, self._send_failures)

    # -- 对端请求 ---------------------------------------------------------
    def handle_request(self, request: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(request, dict):
            return {"result": "error"}
        if request.get("v") != PROTOCOL_VERSION:
            return {"result": "error"}
        if request.get("type") == "status":
            info = self.controller.info()
            return {
                "result": "status",
                "machine": self.cfg.machine,
                "state": self.state.value,
                "channel": info.channel,
                "device": bool(info.device_ok),
            }
        if request.get("type") != "active":
            return {"result": "error"}
        if self.cfg.secret and request.get("secret") != self.cfg.secret:
            return {"result": "error"}
        if request.get("machine") == self.cfg.machine:
            return {"result": "error"}
        try:
            channel = int(request["channel"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            return {"result": "error"}
        if not 1 <= channel <= 3:
            return {"result": "error"}

        now = self.now()
        if now < self._suppress_until:
            log.debug("切换冷却中，暂不交出键盘")
            return {"result": "busy"}

        # 后端能否可靠自证「设备真的在我这」：
        #   原生 HID++ 能读回当前通道 -> 可以据此拒绝；
        #   mxswitch / mxhid 只能看「设备是否应答」，接收器会替不在本机的设备应答，
        #   存在假阳性 -> 不做前置判断，直接尝试切换，由「切换后复核」判定成败。
        reliable = getattr(self.controller, "reliable_state", True)
        if self.state is State.NOT_HOST and reliable:
            # 不在本机就直接回绝；状态恢复交给本地活动时的自检（避免每个请求都跑慢速探测）
            return {"result": "not_host"}

        idle = self.probe.seconds_since_last_input()
        if idle is not None and idle * 1000.0 < self.cfg.release_idle_ms:
            log.debug("本机 %.0fms 前还有输入，暂不交出键盘", idle * 1000.0)
            return {"result": "busy"}

        if reliable:
            # 交接前必须真正读到「键盘当前通道」，确认它醒着且确实在本机；
            # 否则「切换后不再应答」会在键盘休眠时假成功。
            before = self.controller.info()
            self._last_probe = now
            if not before.device_ok:
                self._preverify_miss += 1
                if self._preverify_miss < 2:
                    log.info("请求到达但探测不到键盘（可能休眠），让对端稍后重试")
                    return {"result": "busy"}
                # 连续探测不到：更可能是键盘真的不在这台机器上，如实告诉对端
                self._preverify_miss = 0
                self.state = State.NOT_HOST
                log.info("连续探测不到键盘，转入 NOT_HOST 并回复 not_host")
                return {"result": "not_host"}
            self._preverify_miss = 0

        ok = self.controller.switch_to(channel)
        if ok:
            self.state = State.NOT_HOST
            self._suppress_until = self.now() + self.cfg.switch_cooldown_ms / 1000.0
            log.info("键盘已交给对端（通道 %d），冷却 %.0fs",
                     channel, self.cfg.switch_cooldown_ms / 1000.0)
            return {"result": "switched"}
        # 切换失败：区分「我这儿根本没键盘」和「切不动（可能休眠）」
        if not reliable and not self.controller.info().device_ok:
            self.state = State.NOT_HOST
            return {"result": "not_host"}
        # 切换失败多半是键盘休眠；不要断言 not_host，让对端稍后重试
        log.warning("切换失败（键盘可能休眠），告诉对端稍后重试")
        return {"result": "busy"}


# ----------------------------------------------------------------------------
# 串行执行器 + TCP 服务端 + 主循环
# ----------------------------------------------------------------------------
class _Result:
    __slots__ = ("event", "value")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Dict[str, object] = {"result": "error"}

    def set(self, value: Dict[str, object]) -> None:
        self.value = value
        self.event.set()


class EngineRunner:
    """所有 engine 事件都进同一个队列，保证 controller 不会被并发调用。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="kbsync-engine", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)

    def submit(self, fn: Callable[..., Dict[str, object]], *args: object) -> None:
        self._queue.put((fn, args, None))

    def call(self, fn: Callable[..., Dict[str, object]], *args: object,
             timeout: float = 12.0) -> Dict[str, object]:
        result = _Result()
        self._queue.put((fn, args, result))
        if not result.event.wait(timeout):
            return {"result": "error"}
        return result.value

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, args, result = item
            try:
                value = fn(*args)
            except Exception:  # noqa: BLE001 - 后台线程不能死
                log.exception("处理事件时出错")
                value = {"result": "error"}
            if result is not None:
                result.set(value)  # type: ignore[arg-type]


class RequestServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], engine: Engine,
                 runner: EngineRunner, secret: str) -> None:
        self.engine = engine
        self.runner = runner
        self.secret = secret
        super().__init__(address, RequestHandler)


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            self.request.settimeout(12.0)
            line = self.rfile.readline(65536)
            if not line:
                return
            request = json.loads(line.decode("utf-8", "replace"))
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("收到无法解析的请求: %s", exc)
            return
        if not isinstance(request, dict):
            return
        server: RequestServer = self.server  # type: ignore[assignment]

        if server.secret and request.get("secret") != server.secret:
            reply: Dict[str, object] = {"result": "error"}
        elif request.get("type") == "ping":
            reply = {"result": "pong", "machine": server.engine.cfg.machine}
        else:
            reply = server.runner.call(server.engine.handle_request, request)
        try:
            self.wfile.write(
                (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
            )
        except OSError:
            pass


def run_monitor(
    cfg: Config,
    probe: InputProbe,
    detector: ActivityDetector,
    runner: EngineRunner,
    stop: threading.Event,
) -> None:
    interval = 1.0 / max(1, int(cfg.poll_hz))
    pending = False
    lock = threading.Lock()

    def submit_activity(strength: str) -> None:
        """把一次活动投递给引擎线程；合并积压，避免连续移动把队列塞满。"""
        nonlocal pending
        with lock:
            if pending:
                return
            pending = True

        def task() -> Dict[str, object]:
            nonlocal pending
            with lock:
                pending = False
            runner.engine.on_local_activity(strength)
            return {"result": "ok"}

        runner.submit(task)

    def submit_keepalive() -> None:
        def task() -> Dict[str, object]:
            runner.engine.tick()
            return {"result": "ok"}

        runner.submit(task)

    def submit_self_check() -> Dict[str, object]:
        runner.engine.maybe_self_check()
        return {"result": "ok"}

    last_tick = 0.0
    last_input_check = 0.0
    last_sample_error = 0.0
    while not stop.is_set():
        started = time.monotonic()
        try:
            x, y = probe.cursor()
            button = probe.button_down()
            idle = probe.seconds_since_last_input()
            if detector.feed(started, x, y, button, idle):
                log.debug("活动触发: %s", detector.last_detail or detector.last_strength)
                runner.engine.note_activity(detector.last_strength)
                submit_activity(detector.last_strength)
            elif idle is not None and idle < 0.10 and started - last_input_check >= 1.0:
                # 打字也算输入：只在 NOT_HOST 时做廉价自检，不发起切换
                last_input_check = started
                runner.submit(submit_self_check)
            if started - last_tick >= 0.5:
                last_tick = started
                submit_keepalive()
        except Exception as exc:  # noqa: BLE001 - 单次采样失败不能停掉整个循环
            # 锁屏 / 会话断开时 GetCursorPos 会持续失败，限频记录即可
            now = time.monotonic()
            if now - last_sample_error > 60.0:
                last_sample_error = now
                log.warning("鼠标采样失败（会话可能已锁定或断开）: %s", exc)
        elapsed = time.monotonic() - started
        stop.wait(max(0.0, interval - elapsed))


# ----------------------------------------------------------------------------
# 日志 / CLI
# ----------------------------------------------------------------------------
def setup_logging(cfg: Config, verbose: bool = False, console_only: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, cfg.log_level.upper(), logging.INFO)
    root = logging.getLogger(APP_NAME)
    root.setLevel(level)
    root.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(threadName)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    if not console_only:
        log_path = cfg.log_file or str(default_log_path())
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            print("无法写日志文件 %s: %s" % (log_path, exc), file=sys.stderr)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)


def default_log_path() -> Path:
    return Path.home() / (".%s" % APP_NAME) / ("%s.log" % APP_NAME)


def default_config_path() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    for candidate in (here / "config.json", Path.home() / (".%s" % APP_NAME) / "config.json"):
        if candidate.exists():
            return candidate
    return None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="让一把多主机键盘跟随正在使用的那只鼠标（Windows + macOS）",
    )
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--probe", action="store_true", help="体检：设备/通道/对端连通性")
    parser.add_argument("--self-test", action="store_true", help="自检鼠标活动检测")
    parser.add_argument("--dry-run", action="store_true", help="只记日志，不真的切键盘")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 日志")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    return parser.parse_args(argv)


def load_config(args: argparse.Namespace) -> Config:
    path = args.config or default_config_path()
    if path is None:
        raise ConfigError(
            "找不到配置文件：用 --config 指定，或把 config.json 放到脚本同目录 / ~/.kbsync/"
        )
    return Config.load(Path(path))


def cmd_probe(cfg: Config) -> int:
    print("kbsync %s  env: %s" % (APP_VERSION, sys.platform))
    print("config: %s" % cfg.summary())
    print("log file: %s" % (cfg.log_file or default_log_path()))
    print()

    probe = make_probe()
    try:
        x, y = probe.cursor()
        print("光标位置: (%.0f, %.0f)" % (x, y))
    except Exception as exc:  # noqa: BLE001
        print("读光标失败: %s" % exc)
    idle = probe.seconds_since_last_input()
    print("距上次输入: %s" % ("未知" if idle is None else "%.2f 秒" % idle))
    print()

    try:
        controller = make_controller(cfg, dry_run=False)
    except ConfigError as exc:
        print("键盘控制器: 无法使用（%s）" % exc)
    else:
        print("键盘控制器: %s (backend=%s)"
              % (type(controller).__name__, cfg.backend))
        started = time.monotonic()
        info = controller.info()
        elapsed_ms = (time.monotonic() - started) * 1000.0
        print("  判定键盘在本机: %s | 当前通道: %s | 耗时: %.0f ms"
              % (info.device_ok, info.channel, elapsed_ms))
        if info.raw and info.raw != "hidpp native":
            print("  ---- 控制器输出 ----")
            for line in info.raw.splitlines()[:20]:
                print("  " + line)
            print("  -------------------")
        if hasattr(controller, "close"):
            controller.close()
    print()

    link = TcpLink(cfg)
    ok, detail = link.ping()
    print("对端 %s:%d -> %s (%s)" % (cfg.peer, cfg.port, "可达" if ok else "不可达", detail))
    print()
    print("提示: 两台机器都要能互相访问该端口；Windows 首次运行会弹防火墙，记得允许专用网络。")
    return 0


def cmd_self_test(cfg: Config) -> int:
    probe = make_probe()
    detector = ActivityDetector(
        move_px=cfg.activity_move_px,
        window_s=cfg.activity_window_ms / 1000.0,
        use_input_recency=cfg.use_input_recency,
    )
    print("自检 4 秒：脚本会尝试移动光标，你也可以同时手动晃一下鼠标……")
    start_x, start_y = probe.cursor()
    triggered = 0
    cursor_moved = False
    started = time.monotonic()
    index = 0
    try:
        while time.monotonic() - started < 4.0:
            offset = int(30 * math.sin(index / 4.0))
            try:
                probe.warp(int(start_x) + offset, int(start_y))
            except Exception:  # noqa: BLE001
                pass
            t = time.monotonic()
            x, y = probe.cursor()
            if abs(x - start_x) > 1 or abs(y - start_y) > 1:
                cursor_moved = True
            idle = probe.seconds_since_last_input()
            if detector.feed(t, x, y, probe.button_down(), idle):
                triggered += 1
            index += 1
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            probe.warp(int(start_x), int(start_y))
        except Exception:  # noqa: BLE001
            pass
    print("检测到 %d 次活动事件。" % triggered)
    if triggered == 0:
        if not cursor_moved:
            print(
                "脚本没能移动光标（当前会话可能没有桌面输入权限，比如非交互服务/远程会话）。\n"
                "请在有桌面的终端里重跑，运行期间手动晃一下鼠标。"
            )
        else:
            print("光标动了但没触发活动：把 activity_move_px 调小一点再试。")
        return 1
    return 0


def run_agent(cfg: Config, dry_run: bool) -> int:
    probe = make_probe()
    controller = make_controller(cfg, dry_run=dry_run)
    link = TcpLink(cfg)
    engine = Engine(cfg, probe, controller, link)
    runner = EngineRunner(engine)
    runner.start()

    try:
        server = RequestServer(("0.0.0.0", int(cfg.port)), engine, runner, cfg.secret)
    except OSError as exc:
        log.error("监听 0.0.0.0:%d 失败: %s", cfg.port, exc)
        return 2
    server_thread = threading.Thread(target=server.serve_forever, name="kbsync-server", daemon=True)
    server_thread.start()

    detector = ActivityDetector(
        move_px=cfg.activity_move_px,
        window_s=cfg.activity_window_ms / 1000.0,
        use_input_recency=cfg.use_input_recency,
    )
    stop = threading.Event()

    def _shutdown(signum, _frame):  # type: ignore[no-untyped-def]
        log.info("收到信号 %s，退出", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _shutdown)
            except (OSError, ValueError):
                pass

    log.info("kbsync %s 启动: %s", APP_VERSION, cfg.summary())
    try:
        run_monitor(cfg, probe, detector, runner, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        server.shutdown()
        server.server_close()
        runner.stop()
        log.info("kbsync 已退出")
    return 0


def _configure_stdio() -> None:
    """输出被重定向时用 UTF-8，避免中文在管道/日志里乱码；真终端保持系统默认。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    try:
        cfg = load_config(args)
    except ConfigError as exc:
        print("配置错误: %s" % exc, file=sys.stderr)
        return 2

    setup_logging(cfg, verbose=args.verbose, console_only=args.probe or args.self_test)

    try:
        if args.probe:
            return cmd_probe(cfg)
        if args.self_test:
            return cmd_self_test(cfg)
        return run_agent(cfg, dry_run=args.dry_run)
    except ConfigError as exc:
        log.error("%s", exc)
        print("配置错误: %s" % exc, file=sys.stderr)
        return 2
    except RuntimeError as exc:
        log.error("%s", exc)
        print("运行失败: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
