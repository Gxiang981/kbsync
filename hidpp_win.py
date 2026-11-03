#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 原生 HID++ ChangeHost 控制器（ctypes，无第三方依赖）。

为什么不直接用 mxswitch：
    每次调 mxswitch.ps1 都要 ~6 秒（PowerShell 启动 + 全量扫描所有 HID
    collection / 设备槽位，设备休眠时每个探测都等超时）。这会直接变成
    切换延迟，还会把 kbsync 的网络线程堵死。

做法：
    启动时枚举一次罗技 vendor HID collection，找到能响应 ChangeHost
    (feature 0x1814) 的设备槽位，把 (path, report_id, device_index,
    feature_index) 缓存到 ~/.kbsync/hidpp-cache.json；之后每次查询/切换
    只发一帧，正常几十毫秒。
"""

from __future__ import annotations

import ctypes
import json
import logging
import time
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("kbsync")

VID_LOGITECH = 0x046D
SW_ID = 0x0A
FEAT_CHANGE_HOST = 0x1814
FRAME_LEN = {0x10: 7, 0x11: 20}
DEVICE_INDICES = (1, 2, 3, 0xFF, 4, 5, 6)
CACHE_VERSION = 1

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x03
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
HIDP_STATUS_SUCCESS = 0x00110000


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("InterfaceClassGuid", GUID),
        ("Flags", ctypes.c_uint32),
        ("Reserved", ctypes.c_void_p),
    ]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("DevicePath", ctypes.c_wchar * 512),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_uint32),
        ("VendorID", ctypes.c_uint16),
        ("ProductID", ctypes.c_uint16),
        ("VersionNumber", ctypes.c_uint16),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_uint16),
        ("UsagePage", ctypes.c_uint16),
        ("InputReportByteLength", ctypes.c_uint16),
        ("OutputReportByteLength", ctypes.c_uint16),
        ("FeatureReportByteLength", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16 * 17),
        ("NumberLinkCollectionNodes", ctypes.c_uint16),
        ("NumberInputButtonCaps", ctypes.c_uint16),
        ("NumberInputValueCaps", ctypes.c_uint16),
        ("NumberInputDataIndices", ctypes.c_uint16),
        ("NumberOutputButtonCaps", ctypes.c_uint16),
        ("NumberOutputValueCaps", ctypes.c_uint16),
        ("NumberOutputDataIndices", ctypes.c_uint16),
        ("NumberFeatureButtonCaps", ctypes.c_uint16),
        ("NumberFeatureValueCaps", ctypes.c_uint16),
        ("NumberFeatureDataIndices", ctypes.c_uint16),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


class _Win32:
    def __init__(self) -> None:
        self.hid = ctypes.WinDLL("hid", use_last_error=True)
        self.setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
        self.hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
        self.hid.HidD_GetAttributes.restype = wintypes.BOOL
        self.hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        self.hid.HidD_GetPreparsedData.restype = wintypes.BOOL
        self.hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        self.hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
        self.hid.HidP_GetCaps.restype = ctypes.c_int

        self.setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD
        ]
        self.setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        self.setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
            wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ]
        self.setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
            ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W),
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        self.setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        self.setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED),
        ]
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED),
        ]
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR
        ]
        self.kernel32.CreateEventW.restype = wintypes.HANDLE
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        ]
        self.kernel32.GetOverlappedResult.restype = wintypes.BOOL
        self.kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
        self.kernel32.CancelIoEx.restype = wintypes.BOOL


_win: Optional[_Win32] = None


def win() -> _Win32:
    global _win
    if _win is None:
        _win = _Win32()
    return _win


def _open(path: str, access: int) -> Optional[int]:
    w = win()
    handle = w.kernel32.CreateFileW(
        path, access, FILE_SHARE_RW, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None
    )
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    return int(handle)


def _close(handle: int) -> None:
    try:
        win().kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:  # noqa: BLE001
        pass


def _read(handle: int, length: int, timeout_ms: int) -> bytes:
    w = win()
    ov = OVERLAPPED()
    event = w.kernel32.CreateEventW(None, True, False, None)
    if not event:
        return b""
    ov.hEvent = event
    buf = ctypes.create_string_buffer(length)
    read = wintypes.DWORD(0)
    ok = w.kernel32.ReadFile(
        wintypes.HANDLE(handle), buf, length, ctypes.byref(read), ctypes.byref(ov)
    )
    try:
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                return b""
            if w.kernel32.WaitForSingleObject(event, timeout_ms) != WAIT_OBJECT_0:
                w.kernel32.CancelIoEx(wintypes.HANDLE(handle), ctypes.byref(ov))
                w.kernel32.WaitForSingleObject(event, 500)
                return b""
            if not w.kernel32.GetOverlappedResult(
                wintypes.HANDLE(handle), ctypes.byref(ov), ctypes.byref(read), False
            ):
                return b""
        return buf.raw[: read.value]
    finally:
        w.kernel32.CloseHandle(event)


def _write(handle: int, frame: bytes) -> bool:
    w = win()
    ov = OVERLAPPED()
    event = w.kernel32.CreateEventW(None, True, False, None)
    if not event:
        return False
    ov.hEvent = event
    written = wintypes.DWORD(0)
    try:
        ok = w.kernel32.WriteFile(
            wintypes.HANDLE(handle),
            ctypes.create_string_buffer(frame, len(frame)),
            len(frame),
            ctypes.byref(written),
            ctypes.byref(ov),
        )
        if not ok:
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                return False
            if w.kernel32.WaitForSingleObject(event, 500) != WAIT_OBJECT_0:
                w.kernel32.CancelIoEx(wintypes.HANDLE(handle), ctypes.byref(ov))
                return False
            if not w.kernel32.GetOverlappedResult(
                wintypes.HANDLE(handle), ctypes.byref(ov), ctypes.byref(written), False
            ):
                return False
        return True
    finally:
        w.kernel32.CloseHandle(event)


def _frame(report_id: int, dev_idx: int, feat_idx: int, function: int,
           params: bytes = b"") -> bytes:
    length = FRAME_LEN[report_id]
    frame = bytearray(length)
    frame[0] = report_id
    frame[1] = dev_idx
    frame[2] = feat_idx
    frame[3] = ((function & 0x0F) << 4) | SW_ID
    for i, value in enumerate(params[: length - 4]):
        frame[4 + i] = value
    return bytes(frame)


def _reply_matches(reply: bytes, dev_idx: int, feat_idx: int,
                   function: int) -> bool:
    if len(reply) < 5 or reply[0] not in (0x10, 0x11):
        return False
    if reply[1] != dev_idx and not (dev_idx == 0xFF and reply[1] == 0):
        return False
    header = ((function & 0x0F) << 4) | SW_ID
    if reply[2] == feat_idx and reply[3] == header:
        return True
    return False


class _Interface:
    """一个罗技 vendor HID collection。"""

    def __init__(self, path: str, usage_page: int, in_len: int, out_len: int) -> None:
        self.path = path
        self.usage_page = usage_page
        self.in_len = in_len
        self.out_len = out_len
        self.handle: Optional[int] = None

    def open(self) -> bool:
        if self.handle is None:
            self.handle = _open(self.path, GENERIC_READ | GENERIC_WRITE)
        return self.handle is not None

    def close(self) -> None:
        if self.handle is not None:
            _close(self.handle)
            self.handle = None

    def candidate_report_ids(self) -> List[int]:
        ids = [rid for rid, length in FRAME_LEN.items() if length <= self.out_len]
        ids.sort(key=lambda rid: 0 if FRAME_LEN[rid] == self.out_len else 1)
        return ids or [0x11]

    def transact(self, report_id: int, dev_idx: int, feat_idx: int,
                 function: int, params: bytes = b"",
                 timeout_ms: int = 120) -> Optional[bytes]:
        if not self.open():
            return None
        handle = self.handle
        assert handle is not None
        frame = _frame(report_id, dev_idx, feat_idx, function, params)
        if len(frame) > self.out_len:
            return None
        if not _write(handle, frame):
            return None
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            reply = _read(handle, self.in_len, min(remaining, 60))
            if not reply:
                continue
            if _reply_matches(reply, dev_idx, feat_idx, function):
                return reply
        return None


class WindowsHidppController:
    """查通道 / 切通道 / keepalive，全部走常驻 HID 句柄。"""

    def __init__(self, dry_run: bool = False,
                 cache_path: Optional[Path] = None) -> None:
        self.dry_run = dry_run
        self.cache_path = cache_path or (Path.home() / ".kbsync" / "hidpp-cache.json")
        self._interfaces: Dict[str, _Interface] = {}
        self._device: Optional[Dict[str, object]] = None
        self._last_scan = 0.0
        self._scan_cooldown_until = 0.0
        self._scan_failures = 0
        self._load_cache()

    # -- 缓存 -------------------------------------------------------------
    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return
        device = data.get("device")
        if isinstance(device, dict):
            self._device = device

    def _save_cache(self) -> None:
        if not self._device:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"version": CACHE_VERSION, "device": self._device},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    # -- 枚举 -------------------------------------------------------------
    def _enumerate_interfaces(self) -> List[_Interface]:
        w = win()
        guid = GUID()
        w.hid.HidD_GetHidGuid(ctypes.byref(guid))
        info_set = w.setupapi.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
        )
        if not info_set or info_set == wintypes.HANDLE(-1).value:
            return []
        result: List[_Interface] = []
        try:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            index = 0
            while w.setupapi.SetupDiEnumDeviceInterfaces(
                info_set, None, ctypes.byref(guid), index, ctypes.byref(did)
            ):
                index += 1
                detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
                # 文档规定：x64 固定 8，x86 固定 6，不能用 sizeof
                detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                if not w.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info_set, ctypes.byref(did), ctypes.byref(detail),
                    ctypes.sizeof(detail), None, None
                ):
                    continue
                path = detail.DevicePath
                attrs = HIDD_ATTRIBUTES()
                attrs.Size = ctypes.sizeof(attrs)
                handle = _open(path, 0)
                if handle is None:
                    continue
                try:
                    if not w.hid.HidD_GetAttributes(
                        wintypes.HANDLE(handle), ctypes.byref(attrs)
                    ) or attrs.VendorID != VID_LOGITECH:
                        continue
                    preparsed = ctypes.c_void_p()
                    if not w.hid.HidD_GetPreparsedData(
                        wintypes.HANDLE(handle), ctypes.byref(preparsed)
                    ):
                        continue
                    try:
                        caps = HIDP_CAPS()
                        if w.hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                            continue
                        if caps.UsagePage < 0xFF00:
                            continue
                        if caps.OutputReportByteLength <= 0 or caps.InputReportByteLength <= 0:
                            continue
                        result.append(_Interface(
                            path, caps.UsagePage,
                            caps.InputReportByteLength, caps.OutputReportByteLength,
                        ))
                    finally:
                        w.hid.HidD_FreePreparsedData(preparsed)
                finally:
                    _close(handle)
        finally:
            w.setupapi.SetupDiDestroyDeviceInfoList(info_set)
        return result

    def _scan(self) -> bool:
        log.debug("枚举罗技 HID collection…")
        interfaces = self._enumerate_interfaces()
        log.debug("找到 %d 个 vendor collection", len(interfaces))
        # 两轮扫描：先试最常见的槽位（receiver 槽 1 / 蓝牙直连 0xFF），
        # 再全量扫 1..6。这样键盘醒着的那几秒里更容易命中。
        passes = (((1, 0xFF), 120), ((2, 3, 4, 5, 6), 200))
        for dev_candidates, timeout_ms in passes:
            for iface in interfaces:
                for report_id in iface.candidate_report_ids():
                    for dev_idx in dev_candidates:
                        reply = iface.transact(
                            report_id, dev_idx, 0x00, 0,
                            bytes([(FEAT_CHANGE_HOST >> 8) & 0xFF, FEAT_CHANGE_HOST & 0xFF, 0]),
                            timeout_ms=timeout_ms,
                        )
                        if not (reply and len(reply) >= 5 and reply[4]):
                            continue
                        self._device = {
                            "path": iface.path,
                            "report_id": report_id,
                            "device_index": dev_idx,
                            "feature_index": int(reply[4]),
                        }
                        # 只保留这一个已打开的 collection，其余关掉
                        self._interfaces = {iface.path: iface}
                        for other in interfaces:
                            if other is not iface:
                                other.close()
                        self._save_cache()
                        log.info(
                            "HID++ 设备: path=%s report=0x%02x index=0x%02x feature=0x%02x",
                            iface.path, report_id, dev_idx, reply[4],
                        )
                        return True
        return False

    def _ensure_device(self, rescan: bool = True) -> bool:
        # 有缓存就直接用：设备可能只是休眠了，不要每 5 秒重新全量扫描
        if self._device:
            return True
        now = time.monotonic()
        if not rescan or now < self._scan_cooldown_until:
            return False
        self._last_scan = now
        self._scan_cooldown_until = now + 30.0   # 扫描失败后 30 秒内不再重扫
        return self._scan()

    def _interface_for(self, path: str) -> Optional[_Interface]:
        iface = self._interfaces.get(path)
        if iface is None:
            # 缓存里的 path 可能来自上一次进程；重新枚举一次补全参数
            for candidate in self._enumerate_interfaces():
                if candidate.path == path:
                    iface = candidate
                    break
            if iface is None:
                return None
            self._interfaces[path] = iface
        return iface

    def _call(self, function: int, params: bytes = b"",
              timeout_ms: int = 120) -> Optional[bytes]:
        if not self._device:
            return None
        iface = self._interface_for(str(self._device["path"]))
        if iface is None:
            return None
        feature_index = params if False else int(self._device["feature_index"])
        return iface.transact(
            int(self._device["report_id"]), int(self._device["device_index"]),
            feature_index, function, params, timeout_ms,
        )

    def _presence(self, timeout_ms: int = 200) -> bool:
        """用 getFeature(0x1814) 探测设备是否存在（这对键盘比 getHostInfo 更可靠）。"""
        if not self._device:
            return False
        iface = self._interface_for(str(self._device["path"]))
        if iface is None:
            return False
        reply = iface.transact(
            int(self._device["report_id"]), int(self._device["device_index"]),
            0x00, 0,
            bytes([(FEAT_CHANGE_HOST >> 8) & 0xFF, FEAT_CHANGE_HOST & 0xFF, 0]),
            timeout_ms,
        )
        return bool(reply and len(reply) >= 5 and reply[4])

    # -- 对外接口 ---------------------------------------------------------
    def info(self) -> Tuple[bool, Optional[int]]:
        """返回 (设备是否在本机, 当前通道 1..3)。"""
        if self.dry_run:
            return False, None
        if not self._ensure_device():
            return False, None
        # 只有 getHostInfo 返回了当前通道，才能证明键盘真的在本机。
        # getFeature 在「键盘已切到别的通道」时仍可能被接收器应答，不能用来判宿主。
        for _ in range(2):
            reply = self._call(0, timeout_ms=150)
            if reply and len(reply) >= 6:
                return True, int(reply[5]) + 1
            time.sleep(0.05)
        return False, None

    def switch_to(self, channel: int) -> bool:
        if not 1 <= int(channel) <= 3:
            return False
        if self.dry_run:
            log.info("[dry-run] 本应把键盘切到通道 %d", channel)
            return True
        if not self._ensure_device():
            return False
        iface = self._interface_for(str(self._device["path"]))
        if iface is None:
            return False

        # 关键：先确认设备「醒着」（能读到当前通道）。
        # 设备睡着时写入会被丢弃，如果只靠「切换后是否还应答」判断，
        # 会把「设备睡了、根本没切」误判成切换成功（假成功）。
        current = None
        for _ in range(4):
            found, ch = self.info()
            if found:
                current = ch
                break
            time.sleep(0.15)
        if current is None:
            log.debug("设备无应答（可能已休眠），放弃本次切换")
            return False
        if current == int(channel):
            return True

        frame = _frame(int(self._device["report_id"]), int(self._device["device_index"]),
                       int(self._device["feature_index"]), 1,
                       bytes([int(channel) - 1]))
        if not iface.open() or iface.handle is None:
            return False
        if not _write(iface.handle, frame):
            return False
        # setCurrentHost 不回复：用「设备是否已停止应答 / 通道是否已变」来复核。
        # 要求连续两次确认「设备已离开」才算成功，避免把设备偶发不应答误判成切换成功。
        absent = 0
        for _ in range(10):
            time.sleep(0.12)
            found, ch = self.info()
            if found and ch == int(channel):
                return True
            if found:
                absent = 0
                continue
            absent += 1
            if absent >= 2:
                return True      # 设备确实离开了本机 = 切换成功
        return False

    def keepalive(self) -> bool:
        """发一帧无害的 getHostInfo，尽量让键盘别睡。"""
        if self.dry_run:
            return False
        if not self._device:
            return False
        if not self._presence(200):
            return False
        self._call(0, timeout_ms=150)   # 顺便刷新一下通道（不顺带也无妨）
        return True

    def close(self) -> None:
        for iface in self._interfaces.values():
            iface.close()
        self._interfaces.clear()
