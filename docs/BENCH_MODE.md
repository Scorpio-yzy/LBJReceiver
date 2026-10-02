# 台架模式（Bench Mode）接口说明

【台架模式】是给**测试**用的：App 不拉起任何驱动，而是直接去连一台 **rtl_tcp 兼容的服务器**。
服务器可以是真的电视棒（官方 `rtl_tcp`），也可以是**你自己写的假服务器** —— 用来在没有任何无线信号的地方，
喂确定性的 IQ 数据给 App，验证解码、界面、连接生命周期。

> ★ 本文就是那份"接口契约"。你自己写的假服务器只要满足下面几条，App 就能连上。

## 1. 连接

| 项目 | 值 |
|---|---|
| 传输 | TCP |
| 端口 | **固定 1234**（App 里写死，界面只能改"服务器地址"） |
| 默认地址 | `127.0.0.1`（不填服务器地址时） |
| 并发 | App 只用一条连接；建议服务器也一次只服务一个客户端 |

## 2. 握手

客户端连上后**先读 12 个字节**，**内容不解析**。发 12 个 \0 就行。

## 3. 命令（客户端 → 服务器）

每帧 **5 字节**：`cmd(1 字节) + param(4 字节，大端无符号)`。

| cmd | 名称 | 含义 |
|---|---|---|
| 1 | SET_FREQ | 硬件中心频率 Hz（**= 目标频率 − 50000**，见第 5 节） |
| 2 | SET_SAMPLERATE | 采样率 Hz，本 App 固定 960000 |
| 3 | SET_GAINMODE | 1 = 手动增益 |
| 4 | SET_GAIN | 增益 ×10（dB） |
| 5 | SET_FREQCORR | PPM 校正（有符号，按 u32 二进制补码发） |
| 8 | SET_AGC | 0（关掉 RTL2832U 的数字 AGC） |

建连后客户端会依次发：`SET_SAMPLERATE → SET_FREQ → SET_GAINMODE → SET_GAIN → SET_FREQCORR → SET_AGC`。

**这些命令是单向的，App 不等回复** —— 假服务器可以一条都不处理，全忽略也不影响连通。

## 4. 数据（服务器 → 客户端）

| 项目 | 值 |
|---|---|
| 格式 | **8 位无符号，I/Q 交织，I 在前**（和真实 rtl_tcp 一样） |
| 采样率 | 960 kS/s（App 按 `SET_SAMPLERATE` 处理，请按它发的值产生数据） |
| 每块字节数 | `block_size × 2 = 65536 × 2 = 131072` |
| 价值 | 0 = 负满量程，127/128 ≈ 零，255 = 正满量程 |

## 5. ★ 信号该摆在哪：目标频率在 **+50 kHz**

App 做了 **50 kHz 直流避让**：它要求硬件中心 = **目标频率 − 50 kHz**，
再由 DSP 在软件里把 +50 kHz 的信号搬回零频。

所以：

```
基带偏移 = 目标频率 − SET_FREQ 参数
```

用默认设置（目标 821.2375 MHz）时，App 发来的 `SET_FREQ` 是 `821187500`，
于是**你要把"信号"摆在基带 +50 kHz 处**。

> 偷懒但有效的做法：**完全忽略 `SET_FREQ`，永远把信号放在 +50 kHz**。
> 这样无论 App 怎么调频，它都正好落在解调带上。

## 6. 客户端行为（写服务器时会遇到）

* **服务器没起**：连接被拒 → App 会重试（先短试约 2.5 s，再长试，默认 25 s 内不断重连）
* **服务器中途断开**：界面报 `rtl_tcp 服务端已断开`，并尝试恢复
* **只发数据不发命令回复**：完全正常
* **数据流断了但连接还在**：App 侧有 3 秒级的连接检查与超时
* 台架模式下 App **不会**去拉起内置驱动，也不会碰 USB —— 所以手机可以完全不插电视棒

## 7. 最小可用假服务器（Python）

下面这段足够验证"连通 + 频谱动起来"（发的是噪声，解不出报文）：

```python
#!/usr/bin/env python3
"""最小台架模式服务器：发 12 字节握手头，然后不停发 8bit IQ 噪声。"""
import socket, struct, threading
import numpy as np

PORT, RATE, BLOCK = 1234, 960000, 65536

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('0.0.0.0', PORT))
srv.listen(1)
print('listening on', PORT)

def handle(c):
    c.sendall(b'\0' * 12)                 # 握手：12 字节，内容不看

    def cmd_loop():                        # 命令可以全忽略，这里只打印
        buf = b''
        while True:
            d = c.recv(1024)
            if not d:
                return
            buf += d
            while len(buf) >= 5:
                cmd, param = struct.unpack('>BI', buf[:5])
                buf = buf[5:]
                print('cmd', cmd, 'param', param)
    threading.Thread(target=cmd_loop, daemon=True).start()

    try:
        while True:
            iq = np.random.randint(0, 256, BLOCK * 2, dtype=np.uint8)
            c.sendall(iq.tobytes())
    except OSError:
        pass
    finally:
        c.close()

while True:
    conn, addr = srv.accept()
    print('client', addr)
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
```

要让它**真的解出报文**，就得按第 5 节把合成的 LBJ/POCSAG 信号放在 +50 kHz
（调制方式、BCD/BCH 编码规则见 `app/src/main/python/lbj_ref.py` 的解码逻辑反向推导）。

## 8. 更省事的办法：直接用官方 rtl_tcp

不写假服务器也行 —— 插一根真电视棒在电脑上：

```bash
rtl_tcp -a 0.0.0.0 -p 1234        # Windows/Linux 都行
```

然后手机端：设置 → 勾【台架模式】→ 服务器地址填电脑 IP → 保存并应用 → 开始接收。
实测 960 kS/s ≈ 1.8 MB/s，普通 Wi-Fi 完全够；手机不插棒也能收到**真实空口信号**。

## 9. 参考：本机实测

| 项目 | 值 |
|---|---|
| 手机端 | 台架模式 → `192.168.0.199:1234`，`ESTABLISHED` |
| 服务端 | 电脑端 `rtl_tcp`（Fitipower FC0013 电视棒） |
| 结果 | 手机频谱、RSSI、收音机（FM 95.9）全部正常；样本计数持续增长 |
| 常见坑 | 电脑上也同时跑着别的 1234 端口服务时会连错 —— 先确认只有一个在监听 |
