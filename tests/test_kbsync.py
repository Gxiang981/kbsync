#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kbsync 的单元测试（只用标准库）：

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kbsync  # noqa: E402


class FakeProbe(kbsync.InputProbe):
    def __init__(self, idle: Optional[float] = 10.0) -> None:
        self.idle = idle

    def cursor(self):
        return 0.0, 0.0

    def seconds_since_last_input(self) -> Optional[float]:
        return self.idle


class FakeController(kbsync.KeyboardController):
    def __init__(self, device_ok: bool = True, switch_ok: bool = True) -> None:
        self.device_ok = device_ok
        self.switch_ok = switch_ok
        self.switched_to: List[int] = []
        self.info_calls = 0

    def info(self) -> kbsync.ControllerInfo:
        self.info_calls += 1
        return kbsync.ControllerInfo(
            ok=self.device_ok,
            device_ok=self.device_ok,
            channel=1 if self.device_ok else None,
            raw="MX Keys\nCurrent channel: 1" if self.device_ok else "",
        )

    def switch_to(self, channel: int) -> bool:
        self.switched_to.append(int(channel))
        return self.switch_ok


class FakeLink:
    def __init__(self, replies: Optional[List[Dict[str, object]]] = None) -> None:
        self.replies = list(replies or [])
        self.requests: List[Dict[str, object]] = []

    def request(self, payload, timeout=None):
        self.requests.append(payload)
        if self.replies:
            return self.replies.pop(0)
        return {"result": "error"}

    def ping(self):
        return True, "ok"


def make_config(**overrides):
    data = {
        "machine": "win",
        "my_channel": 1,
        "peer": "10.0.0.2",
        "port": 52845,
        "secret": "s3cret",
        "mxswitch": "C:/tools/mxswitch/windows/mxswitch.ps1",
        "device_name": "Keys",
    }
    data.update(overrides)
    return kbsync.Config.from_dict(data)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class ConfigTests(unittest.TestCase):
    def test_valid(self):
        cfg = make_config()
        self.assertEqual(cfg.my_channel, 1)
        self.assertEqual(cfg.port, 52845)

    def test_unknown_field_rejected(self):
        with self.assertRaises(kbsync.ConfigError):
            kbsync.Config.from_dict({"machine": "x", "nope": 1})

    def test_bad_channel_rejected(self):
        with self.assertRaises(kbsync.ConfigError):
            make_config(my_channel=4)

    def test_missing_peer_rejected(self):
        with self.assertRaises(kbsync.ConfigError):
            make_config(peer="")


class ActivityDetectorTests(unittest.TestCase):
    def test_tiny_move_does_not_trigger(self):
        det = kbsync.ActivityDetector(move_px=16, window_s=0.35, use_input_recency=False)
        self.assertFalse(det.feed(0.0, 100, 100))
        self.assertFalse(det.feed(0.05, 104, 100))
        self.assertFalse(det.feed(0.10, 108, 100))

    def test_real_move_triggers(self):
        det = kbsync.ActivityDetector(move_px=16, window_s=0.35, use_input_recency=False)
        fired = [det.feed(i * 0.03, 100 + i * 12, 100) for i in range(5)]
        self.assertTrue(any(fired))

    def test_click_triggers(self):
        det = kbsync.ActivityDetector(move_px=16, window_s=0.35, use_input_recency=False)
        self.assertFalse(det.feed(0.0, 100, 100, button_down=False))
        self.assertTrue(det.feed(0.03, 100, 100, button_down=True))

    def test_debounce(self):
        det = kbsync.ActivityDetector(move_px=16, window_s=0.35, use_input_recency=False)
        first = det.feed(0.0, 100, 100, button_down=True)
        second = det.feed(0.05, 100, 100, button_down=True)
        self.assertTrue(first)
        self.assertFalse(second)

    def test_input_recency_fallback(self):
        # recency 只作为「确实有位移」时的补充确认，不能单独触发（防抖动误切）
        det = kbsync.ActivityDetector(move_px=1000, window_s=0.35, use_input_recency=True)
        self.assertFalse(det.feed(0.0, 0, 0, seconds_since_input=0.01))
        det2 = kbsync.ActivityDetector(move_px=1000, window_s=0.35, use_input_recency=True)
        det2.feed(0.0, 0, 0, seconds_since_input=1.0)
        self.assertTrue(det2.feed(0.05, 3, 0, seconds_since_input=0.01))
        self.assertFalse(det2.feed(0.3, 3, 0, seconds_since_input=5.0))


class EngineTests(unittest.TestCase):
    def build(self, *, device_ok=True, switch_ok=True, idle=10.0, replies=None):
        self.clock = Clock()
        self.cfg = make_config()
        self.probe = FakeProbe(idle=idle)
        self.controller = FakeController(device_ok=device_ok, switch_ok=switch_ok)
        self.link = FakeLink(replies)
        engine = kbsync.Engine(
            self.cfg, self.probe, self.controller, self.link, now=lambda: self.clock.now
        )
        return engine

    def test_startup_state_host_when_keyboard_found(self):
        engine = self.build(device_ok=True)
        self.assertIs(engine.state, kbsync.State.HOST)

    def test_startup_state_not_host(self):
        engine = self.build(device_ok=False)
        self.assertIs(engine.state, kbsync.State.NOT_HOST)

    def test_request_when_not_host(self):
        engine = self.build(device_ok=False)
        reply = engine.handle_request(
            {"v": 1, "type": "active", "machine": "mac", "channel": 2, "secret": "s3cret"}
        )
        self.assertEqual(reply["result"], "not_host")
        self.assertEqual(self.controller.switched_to, [])

    def test_request_when_host_switches(self):
        engine = self.build(device_ok=True)
        reply = engine.handle_request(
            {"v": 1, "type": "active", "machine": "mac", "channel": 2, "secret": "s3cret"}
        )
        self.assertEqual(reply["result"], "switched")
        self.assertEqual(self.controller.switched_to, [2])
        self.assertIs(engine.state, kbsync.State.NOT_HOST)

    def test_request_busy_when_local_user_active(self):
        # 默认关闭打字保护（release_idle_ms=0）；这里显式打开来测它
        self.clock = Clock()
        cfg = make_config(release_idle_ms=450)
        self.controller = FakeController(device_ok=True)
        self.probe = FakeProbe(idle=0.05)
        self.link = FakeLink()
        engine = kbsync.Engine(
            cfg, self.probe, self.controller, self.link, now=lambda: self.clock.now,
        )
        reply = engine.handle_request(
            {"v": 1, "type": "active", "machine": "mac", "channel": 2, "secret": "s3cret"}
        )
        self.assertEqual(reply["result"], "busy")
        self.assertEqual(self.controller.switched_to, [])

    def test_request_switch_failure_asks_retry(self):
        # 设备还在本机、但切换失败（多半是键盘休眠）：不要断言 not_host，让对端重试
        engine = self.build(device_ok=True, switch_ok=False)
        reply = engine.handle_request(
            {"v": 1, "type": "active", "machine": "mac", "channel": 2, "secret": "s3cret"}
        )
        self.assertEqual(reply["result"], "busy")
        self.assertIs(engine.state, kbsync.State.HOST)

    def test_request_bad_secret(self):
        engine = self.build(device_ok=True)
        reply = engine.handle_request(
            {"v": 1, "type": "active", "machine": "mac", "channel": 2, "secret": "wrong"}
        )
        self.assertEqual(reply["result"], "error")
        self.assertEqual(self.controller.switched_to, [])

    def test_request_bad_channel(self):
        engine = self.build(device_ok=True)
        reply = engine.handle_request(
            {"v": 1, "type": "active", "machine": "mac", "channel": 9, "secret": "s3cret"}
        )
        self.assertEqual(reply["result"], "error")

    def test_activity_when_host_does_not_ask(self):
        engine = self.build(device_ok=True)
        engine.on_local_activity()
        self.assertEqual(self.link.requests, [])

    def test_activity_when_not_host_asks_and_becomes_host(self):
        engine = self.build(device_ok=False, replies=[{"result": "switched"}])
        engine.on_local_activity()
        self.assertEqual(len(self.link.requests), 1)
        self.assertEqual(self.link.requests[0]["channel"], 1)
        self.assertIs(engine.state, kbsync.State.HOST)

    def test_not_host_self_check_recovers(self):
        engine = self.build(device_ok=False)
        self.assertIs(engine.state, kbsync.State.NOT_HOST)
        self.clock.advance(6.0)
        self.controller.device_ok = True   # 键盘其实就在本机（启动时它睡着了）
        engine.on_local_activity()
        self.assertIs(engine.state, kbsync.State.HOST)
        self.assertEqual(self.link.requests, [])

    def test_activity_not_host_reply_uses_local_probe(self):
        engine = self.build(device_ok=False, replies=[{"result": "not_host"}])
        self.controller.device_ok = True   # 对端没键盘，本机探测确实有
        engine.on_local_activity()
        self.assertIs(engine.state, kbsync.State.HOST)

    def test_activity_busy_keeps_asking_later(self):
        engine = self.build(device_ok=False, replies=[{"result": "busy"}])
        engine.on_local_activity()
        self.assertIs(engine.state, kbsync.State.NOT_HOST)
        # 未到最小间隔，不会立刻重发
        engine.on_local_activity()
        self.assertEqual(len(self.link.requests), 1)
        self.clock.advance(1.5)
        engine.on_local_activity()
        self.assertEqual(len(self.link.requests), 2)

    def test_host_recheck_detects_keyboard_left(self):
        engine = self.build(device_ok=True, replies=[{"result": "switched"}])
        # 复核还没到期：不动作
        engine.on_local_activity()
        self.assertEqual(self.link.requests, [])
        self.clock.advance(20.0)
        self.controller.device_ok = False  # 键盘其实已经被手动切走
        engine.on_local_activity()         # 第 1 次探测失败：保持 HOST
        self.assertIs(engine.state, kbsync.State.HOST)
        self.clock.advance(20.0)
        engine.on_local_activity()         # 连续第 2 次失败：翻转 NOT_HOST 并请求
        self.assertEqual(len(self.link.requests), 1)


class ConfigFileTests(unittest.TestCase):
    def test_example_config_is_valid(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        cfg = kbsync.Config.load(example)
        self.assertEqual(cfg.my_channel, 1)

    def test_config_with_utf8_bom(self):
        # Windows 记事本 / PowerShell 5 写出来的 JSON 常带 BOM，必须能读
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        content = example.read_text(encoding="utf-8")
        path = Path(__file__).resolve().parent / "_bom_config.json"
        try:
            path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
            cfg = kbsync.Config.load(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(cfg.my_channel, 1)


class LoopbackTests(unittest.TestCase):
    """真起 TCP 服务端 + 客户端，验证「请求 -> 切键盘 -> 状态回写」整条链路。"""

    def setUp(self):
        self.server_cfg = make_config(machine="mac", my_channel=2, peer="127.0.0.1")
        self.server_controller = FakeController(device_ok=True)
        self.server_engine = kbsync.Engine(
            self.server_cfg, FakeProbe(idle=10.0), self.server_controller, FakeLink(),
            now=lambda: 1000.0,
        )
        self.runner = kbsync.EngineRunner(self.server_engine)
        self.runner.start()
        self.server = kbsync.RequestServer(
            ("127.0.0.1", 0), self.server_engine, self.runner, self.server_cfg.secret
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runner.stop()

    def client_engine(self, device_ok=False):
        cfg = make_config(machine="win", my_channel=1, peer="127.0.0.1", port=self.port)
        return kbsync.Engine(
            cfg, FakeProbe(idle=10.0), FakeController(device_ok=device_ok),
            kbsync.TcpLink(cfg), now=lambda: 1000.0,
        )

    def test_handover_over_tcp(self):
        engine = self.client_engine()
        engine.on_local_activity()
        self.assertEqual(self.server_controller.switched_to, [1])
        self.assertIs(engine.state, kbsync.State.HOST)

    def test_handover_back(self):
        engine = self.client_engine(device_ok=True)  # 客户端本来持有键盘
        self.assertIs(engine.state, kbsync.State.HOST)
        # 对端（server）反过来请求
        reply = self.server_engine.handle_request(
            {"v": 1, "type": "active", "machine": "win", "channel": 1, "secret": "s3cret"}
        )
        self.assertEqual(reply["result"], "switched")
        self.assertEqual(self.server_controller.switched_to, [1])

    def test_ping_ok(self):
        cfg = make_config(machine="win", my_channel=1, peer="127.0.0.1", port=self.port)
        ok, detail = kbsync.TcpLink(cfg).ping()
        self.assertTrue(ok, detail)

    def test_ping_bad_secret(self):
        cfg = make_config(
            machine="win", my_channel=1, peer="127.0.0.1", port=self.port, secret="other"
        )
        ok, _ = kbsync.TcpLink(cfg).ping()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
