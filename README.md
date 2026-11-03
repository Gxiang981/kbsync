# kbsync — 让一把多主机键盘跟随「你正在用的那只鼠标」

> Windows + macOS，两台机器各接一只 G 鼠标，共用一把 MX 键盘（Logi Bolt / 蓝牙多通道）。
> 动了哪台机器的鼠标，键盘就自动跟到哪台 —— 不用再按键盘上的 F2 / F3。

```
        Windows                                   macOS
   ┌────────────────┐                       ┌────────────────┐
   │  G 鼠标          │                       │  G 鼠标          │
   │  kbsync         │◄──── TCP :52845 ────►│  kbsync         │
   │  (原生 HID++)    │      局域网协商        │  (原生 HID++)    │
   └───────┬────────┘                       └────────┬───────┘
           │      HID++  CHANGE_HOST (0x1814)        │
           └───────────────► MX 键盘 ◄───────────────┘
                    Easy-Switch 通道 F2 / F3
```

---

## 为什么需要它

罗技官方有 **Flow** 功能（键盘跟着鼠标跨电脑走），但它只认 **Flow 认证的鼠标**（MX 系列）。
G 系列（G Pro 等游戏鼠标）走 G HUB，**不能当 Flow 的触发源**。

于是有两条路：

1. 买一只 MX 鼠标来触发 Flow（很多人不接受那只鼠标的手感）；
2. **自己实现**：用 HID++ 的 `CHANGE_HOST` 指令直接切键盘的 Easy-Switch 通道 —— 就是本项目。

---

## 原理：为什么只能「推」不能「拉」

多主机键盘**同一时刻只对当前主机开放设备**：别的机器既看不到它，也发不出指令。
所以：

- 谁**持有**键盘，谁才有能力把它切走；
- 想从别人那里拿键盘，必须**请求**对方，由对方执行切换。

kbsync 的每台机器维护一个状态：

| 状态 | 含义 | 本机鼠标动了时 |
|---|---|---|
| `HOST` | 键盘在我这 | 什么都不做（只低频复核一次） |
| `NOT_HOST` | 键盘不在我这 | 向对端发请求；若对端是 `HOST`，它就把键盘推给我 |

协商报文是一行 JSON（见文末「协议」），跑在局域网 TCP 52845 上，带一个 `secret` 防止被同网段的人乱发。

---

## ⚠️ 实测结论（请先读这一节，省得白折腾）

本项目在 **MX Mechanical Mini + Logi Bolt 接收器 + 蓝牙双通道** 这套硬件上反复实测，得到几个很关键的事实：

| 事实 | 影响 |
|---|---|
| 键盘**空闲约 1 分钟就进入深度休眠**，休眠后**不再回应任何 HID++ 报文** | 休眠期间**任何机器都切不动它**，只能靠人敲一下键唤醒 |
| 每 500ms 发保活报文**并不能阻止它休眠** | 保活只保证「醒着的时候随时能切」，不保证「一直醒着」 |
| **只有键盘所在那台机器能推它走** | 两端都要实现原生 HID++ 才能双向切换，单端做不到 |
| macOS 上那个接收器**不回复 `getHostInfo`（读通道）**，只回 `getFeature`（探存在） | 判断「键盘在不在我這」必须用 `getFeature`；读通道失败 **≠** 键盘不在 |
| 键盘回包本身**间歇性**（同样 400ms 超时，有时有有时没有） | 发现设备要**记住上次成功的槽位**，一次全量扫描十几秒会错过「刚醒」的窗口 |
| macOS 的输入监控权限按**文件路径 + 二进制哈希**绑定 | 每次重新编译 `tools/mxhid.c` 都要**重新授权一次** |

**因此实际使用节奏是**：换到某台机器后**顺手敲一下键盘**，之后那台就能随时把它切走；
如果离开时那台机器上很久没敲过字（键盘已睡），就切不动，需要手动按 F2 / F3。

| 方向 | 实测结果 |
|---|---|
| Windows → Mac（动 Mac 鼠标） | ✅ 1~3 秒，键盘立刻到 Mac |
| Mac → Windows（动 Windows 鼠标） | ✅ 前提：Mac 上刚敲过字；否则失败 |

---

## 架构

```
kbsync.py            协议层 + 状态机 + 平台抽象（只依赖标准库）
hidpp_win.py         Windows 原生 HID++ 控制器（ctypes 直连 hid.dll，常驻句柄）
tools/mxhid.c        macOS 原生 HID++ 助手（常驻进程，毫秒级，需输入监控授权）
tools/cross_check.py 两台机器之间的连通性自检（不碰硬件）
tools/cursor_watch.py 光标采样，确认活动检测是否生效
tools/mac_probe_check.py macOS/Linux 端手动体检
```

| 后端 | 平台 | 单次操作 | 说明 |
|---|---|---|---|
| `hidpp`（原生） | Windows | **16~31 ms** | 默认，常驻句柄 + 500ms 保活 + 缓存发现结果 |
| `mxhid`（原生助手） | macOS | ~0.4 s | 更快，但要给二进制授「输入监控」；重编译后需重新授权 |
| `mxswitch` | 两端 | ~0.8 s | 通用兜底（MIT，Windows/macOS/Linux 都有） |

---

## 安装

### 0. 前置

- Python **3.9+**（只用标准库，无需 pip 依赖）
- 一把支持 Easy-Switch 的 MX 键盘（MX Keys / MX Mechanical / MX Keys Mini …）
- 两台机器在**同一局域网**，能互相访问

### 1. Windows

```powershell
git clone https://github.com/<你的账号>/kbsync.git
cd kbsync
copy config.example.json config.json
# 编辑 config.json（见下）
```

Windows 端用仓库自带的原生后端，**不需要装任何驱动或 SDK**。

### 2. macOS

```bash
git clone https://github.com/<你的账号>/kbsync.git
cd kbsync
cp config.example.json config.json

# 方案 A：直接用 mxswitch（最省事，只需一次授权）
git clone https://github.com/marcocosta97/mxswitch
cd mxswitch && make && cd ..
#   给 mxswitch 授「输入监控」：系统设置 → 隐私与安全性 → 输入监控

# 方案 B：编译本仓库的原生助手（更快）
cd tools
clang -O2 -Wall -Wextra -o mxhid mxhid.c \
    -framework IOKit -framework CoreFoundation -framework CoreGraphics
codesign -s - mxhid
./mxhid --setup        # 打开「输入监控」，把 mxhid 加进去并打开开关
cd ..
```

> **重新编译 `mxhid.c` 后必须重新授权**：macOS 的输入监控绑定「路径 + 二进制哈希」，
> 换了文件就失效（表现是助手能打开设备却收不到任何回包）。

---

## 配置

`config.json`（两台各一份，字段对称）：

```jsonc
{
  "machine": "win",            // 两端不能重名
  "my_channel": 2,             // 本机对应的键盘通道（Windows=F2 → 2，Mac=F3 → 3）
  "peer": "192.168.31.122",    // 对端 IP（建议固定 IP 或 DHCP 保留）
  "port": 52845,               // 两端一致
  "secret": "换成只有你知道的一串字符",   // 两端一致
  "backend": "auto",           // auto | hidpp | mxhid | mxswitch
  "mxhid_path": "/Users/you/kbsync/tools/mxhid",   // macOS 原生助手路径
  "mxswitch": "/Users/you/mxswitch/mxswitch",      // mxswitch 后端路径
  "keepalive_ms": 2000,        // 定期探测/保活的间隔
  "release_idle_ms": 0,        // >0 启用打字保护（会被鼠标抖动误触发，默认关）
  "switch_cooldown_ms": 2500,  // 一次切换后的冷却，防两边来回抢
  "activity_move_px": 16,      // 350ms 内位移超过多少像素算「在用这只鼠标」
  "host_recheck_ms": 15000,    // HOST 状态多久复核一次
  "log_level": "INFO"
}
```

`my_channel` 对应键盘上的 Easy-Switch 键（F1/F2/F3）。

---

## 运行

```bash
python kbsync.py --config config.json            # 前台运行
python kbsync.py --config config.json --probe    # 体检：设备/通道/对端连通性
python kbsync.py --config config.json --self-test # 自检鼠标活动检测
python kbsync.py --config config.json --dry-run  # 只记日志，不真的切键盘
```

### 开机自启

**Windows**（任务计划程序，隐藏窗口）：

```powershell
schtasks /Create /TN "kbsync" /SC ONLOGON /RL LIMITED ^
  /TR "\"C:\path\pythonw.exe\" \"C:\path\kbsync.py\" --config \"C:\path\config.json\""
```

或者直接双击 `run-windows.bat`（先改好里面的路径）。

**macOS**（launchd）：把 `com.kbsync.agent.plist` 里的路径改成你自己的，然后

```bash
cp com.kbsync.agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kbsync.agent.plist
```

日志默认在 `~/.kbsync/kbsync.log`（自动轮转）。

---

## 故障排查

| 现象 | 排查 |
|---|---|
| 完全没反应 | 先跑 `--probe`：看「判定键盘在本机」和对端连通性 |
| `--probe` 显示键盘不在本机 | 键盘在另一台机器上，或已休眠 → 敲一下键盘唤醒 |
| 切不动（一直 busy） | 键盘在**当前这台**机器上且已休眠 → 在那台机器敲一下键 |
| 刚切过去就跳回来 | 两边鼠标都在动；调大 `switch_cooldown_ms`、`activity_move_px` |
| 一动鼠标就切，太灵敏 | 调大 `activity_move_px`（24~32）、`switch_cooldown_ms` |
| macOS 助手收不到任何回包 | 输入监控没授权，或二进制重编译后没重新授权 → `./mxhid --setup` |
| Windows 端探测不到 | 键盘休眠；或被对端切走；或接收器未插好 |

跨机连通性自检（不碰硬件）：

```bash
# A 机
python3 tools/cross_check.py serve --port 52860 --secret s3cret
# B 机
python3 tools/cross_check.py call --peer <A的IP> --port 52860 --secret s3cret --channel 2
# 期望：ping -> ok / active -> {'result': 'switched'} / RESULT: PASS
```

---

## 开发与测试

```bash
python -m unittest discover -s tests -v    # 29 个用例：检测器 / 状态机 / 回环 TCP
python -m py_compile kbsync.py hidpp_win.py
```

协议（TCP，一行一个 JSON）：

```jsonc
// 请求
{"v":1,"type":"active","machine":"win","channel":2,"secret":"..."}
// 响应
{"result":"switched"}   // 对端持有键盘，已推过来
{"result":"not_host"}   // 对端没有键盘
{"result":"busy"}       // 对端正在使用（打字保护）/ 冷却中，稍后重试
{"result":"error"}      // 校验失败 / 对端异常
{"result":"pong"}       // ping
```

---

## 致谢与许可

- HID++ 协议细节参考 Logitech 公开的 HID++ 规范；
- [mxswitch](https://github.com/marcocosta97/mxswitch)（MIT，作者 marcocosta97）——跨平台切换键盘通道的实现思路与报文格式，本项目在 macOS 上也直接使用它；
- `tools/mxhid.c` 与 `hidpp_win.py` 是本项目为「常驻句柄 + 毫秒级响应」重写的实现。

本项目采用 MIT 许可，详见 [LICENSE](LICENSE)。使用前请确认你的键盘支持 Easy-Switch（多通道）。
