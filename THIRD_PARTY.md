# 第三方组件与许可

本项目的**整体**以 **GPL-3.0-or-later** 发布（见 `LICENSE`）—— 因为它链接/包含了下列 GPL 组件，
整个 APK 属于 GPL 衍生作品，这不是可选项。

| 组件 | 位置 | 版权 | 许可 |
|---|---|---|---|
| **LBJ 解码核心**（DSP / POCSAG / BCH / 报文解析） | `app/src/main/python/lbj_ref.py` | Copyright (C) 2026 **Sdr-Is-Fun** | GPL-3.0-or-later |
| librtlsdr（含 Android/Signalware 分支） | `rtlsdr/src/main/cpp/librtlsdr/` | Steve Markgraf、Dimitri Stolnikov、Signalware Ltd | GPL-2.0-or-later |
| libusb | `rtlsdr/src/main/cpp/librtlsdr/libusb/` | libusb 项目 | LGPL-2.1 |
| rtlsdr / sdrdrivertools（Java/JNI 封装） | `rtlsdr/`、`sdrdrivertools/` | Copyright (C) 2022 **Signalware Ltd** `<driver@sdrtouch.com>` | GPL-2.0-or-later |
| Chaquopy（Android 上的 CPython 运行时） | Gradle 依赖 | Chaquo Ltd | MIT |
| CPython | Chaquopy 运行时 | Python Software Foundation | PSF-2.0 |
| numpy | Chaquopy 运行时 | numpy 开发者 | BSD-3-Clause |
| SciPy | 仅桌面测试用（Android 上是本项目写的兼容层） | SciPy 开发者 | BSD-3-Clause |
| AndroidX / Material Components / Kotlin 标准库 | Gradle 依赖 | Google / JetBrains | Apache-2.0 |

## 特别说明

### `lbj_ref.py`

**逐字节照搬**自 [Sdr-Is-Fun/RTL_SDR_LBJ_RECEIVER](https://github.com/Sdr-Is-Fun/RTL_SDR_LBJ_RECEIVER)
的 `rtl_sdr_lbj_receiver.py`（68,891 字节），仅**改了文件名**，正文一字未动（文件头版权声明原样保留）。
本仓库在它开头加了一段来源说明，除此之外与上游完全一致。

该项目自己又参考了 `FLN1021/SX1276_Receive_LBJ`，并声明同样以 GPL 发布 —— 再分发时请一并保留这些说明。

## libmirisdr（Mirics MSi2500/MSi001 —— SDRplay RSP1 类设备）

- 位置：`mirisdr/src/main/cpp/libmirisdr/`
- 来源：[ericek111/libmirisdr-5](https://github.com/ericek111/libmirisdr-5)（上游为 OSMOCOM/f4exb 的 libmirisdr）
- 许可：**GPL-2.0-or-later**（源码头："either version 2 of the License, or (at your option) any
  later version"）—— 与本项目的 GPL-3.0-or-later 兼容，可以随 APK 分发
- 用途：驱动 **Mirics MSi2500 + MSi001** 芯片的设备（SDRplay **RSP1** 及同芯片克隆板，
  设备表里还列了 1df7:3000/3010）。**当前状态：驱动库已能编译进工程，但还没接到界面上**
  （见 README「RSP1」一节）
- 该模块里的 `libusb/` 沿用 `:rtlsdr` 模块自带的那份 Android 移植（LGPL-2.1）
- `libmirisdr/src/libusb.h` + `libusb_compat.c` 是本项目新增的兼容层（把上游要的
  libusb 1.0.22 接口映射到移植版已有的 fd 接口上），同样是 GPL-3.0-or-later

### 署名与再分发

如果你基于本项目发布修改版，GPL 要求你：

1. 保留本文件的版权署名与 `LICENSE`；
2. 以相同许可（GPL-3.0-or-later）发布；
3. 在改过的文件上**注明改动**；
4. 提供完整对应的源代码（包括 `app/src/main/python/`、`rtlsdr/`、`mirisdr/` 里的源码）。
