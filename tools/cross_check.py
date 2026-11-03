#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨机连通性自检：只验证 TCP 协议 / 线程 / Python 版本，不碰真实键盘。

在一台机器上跑服务端（用假键盘控制器，永远假装「键盘在本机」）：

    python3 tools/cross_check.py serve --port 52860 --secret s3cret

在另一台机器上跑客户端：

    python3 tools/cross_check.py call --peer 192.168.1.42 --port 52860 \
        --secret s3cret --channel 2

预期：ping 返回 pong，active 返回 switched（说明对端能收到并会「切键盘」），
错误 secret 返回 error。整个过程不会碰 Logitech 硬件。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kbsync  # noqa: E402


class FakeProbe(kbsync.InputProbe):
    def __init__(self, idle: float = 10.0) -> None:
        self.idle = idle

    def cursor(self):
        return 0.0, 0.0

    def seconds_since_last_input(self):
        return self.idle


class FakeController(kbsync.KeyboardController):
    def __init__(self) -> None:
        self.switched = []

    def info(self):
        return kbsync.ControllerInfo(ok=True, device_ok=True, channel=1, raw="fake")

    def switch_to(self, channel: int) -> bool:
        self.switched.append(int(channel))
        print("  [fake] 收到切换请求 -> 通道 %d" % channel, flush=True)
        return True


class FakeLink:
    def request(self, payload, timeout=None):  # noqa: ARG002
        return {"result": "not_host"}


def make_config(machine: str, port: int, peer: str, secret: str, channel: int) -> kbsync.Config:
    return kbsync.Config.from_dict(
        {
            "machine": machine,
            "my_channel": channel,
            "peer": peer,
            "port": port,
            "secret": secret,
            "mxswitch": "fake-mxswitch",
        }
    )


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = make_config(args.machine, args.port, "127.0.0.1", args.secret, 1)
    controller = FakeController()
    engine = kbsync.Engine(cfg, FakeProbe(), controller, FakeLink())
    runner = kbsync.EngineRunner(engine)
    runner.start()
    server = kbsync.RequestServer(("0.0.0.0", args.port), engine, runner, cfg.secret)
    print("serving on 0.0.0.0:%d (machine=%s) … Ctrl-C 退出" % (args.port, args.machine), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        runner.stop()
    print("switched channels:", controller.switched)
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    cfg = make_config(args.machine, args.port, args.peer, args.secret, args.channel)
    link = kbsync.TcpLink(cfg)
    ok, detail = link.ping()
    print("ping  -> %s (%s)" % ("ok" if ok else "FAIL", detail))
    reply = link.request(
        {
            "v": kbsync.PROTOCOL_VERSION,
            "type": "active",
            "machine": args.machine,
            "channel": args.channel,
            "secret": args.secret,
        }
    )
    print("active-> %s" % reply)
    bad = link.request(
        {
            "v": kbsync.PROTOCOL_VERSION,
            "type": "active",
            "machine": args.machine,
            "channel": args.channel,
            "secret": "wrong-secret",
        }
    )
    print("bad   -> %s" % bad)
    passed = ok and reply.get("result") == "switched" and bad.get("result") == "error"
    print("RESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    serve = sub.add_parser("serve", help="启动假键盘服务端")
    serve.add_argument("--machine", default="peer")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--secret", default="")
    serve.set_defaults(func=cmd_serve)

    call = sub.add_parser("call", help="向对端发起自检请求")
    call.add_argument("--machine", default="me")
    call.add_argument("--peer", required=True)
    call.add_argument("--port", type=int, required=True)
    call.add_argument("--secret", default="")
    call.add_argument("--channel", type=int, default=2)
    call.set_defaults(func=cmd_call)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
