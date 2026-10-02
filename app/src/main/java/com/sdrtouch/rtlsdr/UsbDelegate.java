/*
 * ★ 修改说明（LBJ Receiver 项目添加）
 *   本文件来自 Signalware Ltd 的 rtl_tcp_andro 模块，版权归其所有，适用下方原始许可。
 *   本项目对它的改动：
 *     · USB 插入时额外启动 com.railfan.lbj.MainActivity 并带 autostart extra
 *       （上游只发广播，本项目里没有接收者，导致"插上棒不会自动开始接收"）
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

import android.app.Activity;
import android.content.Intent;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbManager;
import android.os.Bundle;
import com.sdrtouch.tools.Log;

/**
 * 插上电视棒时的入口（无界面，manifest 里用 USB_DEVICE_ATTACHED + device_filter 触发）。
 *
 * 原来它只发一个 ACTION_SDR_DEVICE_ATTACHED 广播 —— 那是原驱动 App 给
 * DeviceOpenActivity 用的，本 App 里没有任何接收者，等于什么都没做：
 * 用户插上电视棒之后还得自己找到 App、点【开始接收】。
 * 现在改成直接把主界面拉起来，并带上 autostart 标记。
 * 要不要真的自动开始接收由 MainActivity 按设置决定：
 * 只有用户启用了「使用内置驱动」才自动 —— 否则不能抢外部驱动 App 的活。
 */
public class UsbDelegate extends Activity {

    private static final String TAG = UsbDelegate.class.getSimpleName();

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Intent intent = getIntent();

        if (UsbManager.ACTION_USB_DEVICE_ATTACHED.equals(intent.getAction())) {
            UsbDevice usbDevice = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
            if (usbDevice != null) {
                Log.appendLine(TAG + " USB attached: " + usbDevice.getDeviceName());
                try {
                    Intent open = new Intent();
                    open.setClassName(getPackageName(), "com.railfan.lbj.MainActivity");
                    open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                            | Intent.FLAG_ACTIVITY_CLEAR_TOP
                            | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                    open.putExtra("autostart", true);
                    startActivity(open);
                } catch (Throwable t) {
                    Log.appendLine(TAG + " 打不开主界面: " + t);
                }
            }
        }

        finish();
    }
}
