/*
 * ★ 修改说明（LBJ Receiver 项目添加）
 *   本文件来自 Signalware Ltd 的 rtl_tcp_andro 模块，版权归其所有，适用下方原始许可。
 *   本项目对它的改动：
 *     · 新增带 SdrDevice/SdrTcpArguments 的构造函数
 *     · isBound() 改为 public（本项目的 BuiltinDriver 需要查询绑定状态）
 *   改动之外与上游一致。本项目整体以 GPL-3.0-or-later 发布，详见 LICENSE 与 THIRD_PARTY.md。
 */
/*
 * rtl_tcp_andro is a library that uses libusb and librtlsdr to
 * turn your Realtek RTL2832 based DVB dongle into a SDR receiver.
 * It independently implements the rtl-tcp API protocol for native Android usage.
 * Copyright (C) 2022 by Signalware Ltd <driver@sdrtouch.com>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

package com.sdrtouch.rtlsdr;

import android.content.ComponentName;
import android.content.ServiceConnection;
import android.os.IBinder;

import com.sdrtouch.core.SdrTcpArguments;
import com.sdrtouch.core.devices.SdrDevice;

/**
 * 原样搬自 rtl_tcp_andro 的 DeviceOpenActivity 所在包。
 *
 * 与原版唯一的差别：构造器和 isBound() 从包内可见改成 public
 * —— 现在由 {@link BuiltinDriver}（Kotlin）在同一包里创建它，
 * 逻辑一行没动。
 */
public class SdrServiceConnection implements ServiceConnection {
    private final SdrDevice sdrDevice;
    private final SdrTcpArguments sdrTcpArguments;
    private final Runnable onDisconnected;
    private volatile boolean isBound;

    public SdrServiceConnection(SdrDevice sdrDevice, SdrTcpArguments sdrTcpArguments, Runnable onDisconnected) {
        this.sdrDevice = sdrDevice;
        this.sdrTcpArguments = sdrTcpArguments;
        this.onDisconnected = onDisconnected;
        this.isBound = false;
    }

    @Override
    public void onServiceConnected(ComponentName name, IBinder ibinder) {
        isBound = true;
        BinaryRunnerService.LocalBinder binder = (BinaryRunnerService.LocalBinder) ibinder;
        binder.startWithDevice(sdrDevice, sdrTcpArguments);
    }

    @Override
    public void onServiceDisconnected(ComponentName name) {
        isBound = false;
        onDisconnected.run();
    }

    public boolean isBound() {
        return isBound;
    }
}
