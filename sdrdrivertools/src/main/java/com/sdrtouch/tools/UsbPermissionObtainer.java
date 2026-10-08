/*
 * rtl_tcp_andro is a library that uses libusb and librtlsdr to
 * turn your Realtek RTL2832 based DVB dongle into a SDR receiver.
 * It independently implements the rtl-tcp API protocol for native Android usage.
 * Copyright (C) 2022 by Signalware Ltd <driver@sdrtouch.com>
 *
 * This program is free software: you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License
 *  along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

package com.sdrtouch.tools;

import static android.app.PendingIntent.FLAG_ALLOW_UNSAFE_IMPLICIT_INTENT;
import static android.app.PendingIntent.FLAG_MUTABLE;
import static android.content.Context.RECEIVER_NOT_EXPORTED;

import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbManager;
import android.os.Build;

import java.util.concurrent.Future;

public class UsbPermissionObtainer {
    /**
     * ★ 动作名必须带包名。原来用的是安卓示例里的全局动作 "com.android.example.USB_PERMISSION"，
     *   而且接收器是 RECEIVER_EXPORTED —— 任何 App 都能伪造这个广播：
     *   轻则把 task 提前置成 null（权限流程错乱），重则引导我们 openDevice 到别的设备。
     *   带上包名 + NOT_EXPORTED 之后，只有系统发的（带着我们那个 PendingIntent）才收得到。
     */
    private static String actionUsbPermission(Context ctx) {
        return ctx.getPackageName() + ".USB_PERMISSION";
    }

    public static Future<UsbDeviceConnection> obtainFdFor(Context context, UsbDevice usbDevice) {
        int flags = 0;

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            flags = FLAG_MUTABLE | FLAG_ALLOW_UNSAFE_IMPLICIT_INTENT;
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            flags = FLAG_MUTABLE;
        }
        UsbManager manager = (UsbManager) context.getSystemService(Context.USB_SERVICE);
        if (!manager.hasPermission(usbDevice)) {
            AsyncFuture<UsbDeviceConnection> task = new AsyncFuture<>();
            String action = actionUsbPermission(context);
            registerNewBroadcastReceiver(context, usbDevice, task, action);
            manager.requestPermission(usbDevice, PendingIntent.getBroadcast(context, 0,
                    new Intent(action).setPackage(context.getPackageName()), flags));
            return task;
        } else {
            return new CompletedFuture<>(manager.openDevice(usbDevice));
        }
    }

    private static void registerNewBroadcastReceiver(final Context context, final UsbDevice usbDevice,
                                                     final AsyncFuture<UsbDeviceConnection> task,
                                                     final String expectedAction) {
        BroadcastReceiver broadcastReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                String action = intent.getAction();
                if (expectedAction.equals(action)) {
                    synchronized (this) {
                        if (task.isDone()) {
                            Log.appendLine("Permission already should be processed, ignoring.");
                            return;
                        }
                        UsbManager manager = (UsbManager) context.getSystemService(Context.USB_SERVICE);
                        UsbDevice device = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
                        // ★ device 可能为 null（广播里可以不带 EXTRA_DEVICE），
                        //   直接 device.equals(...) 会 NPE，异常又发生在广播线程上。
                        if (device == null) {
                            Log.appendLine("Permission broadcast without device");
                            task.setDone(null);
                            context.unregisterReceiver(this);
                            return;
                        }
                        if (device.equals(usbDevice)) {
                            if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)) {
                                if (!manager.hasPermission(device)) {
                                    Log.appendLine("Permissions were granted but can't access the device");
                                    task.setDone(null);
                                } else {
                                    Log.appendLine("Permissions granted and device is accessible");
                                    task.setDone(manager.openDevice(device));
                                }
                            } else {
                                Log.appendLine("Extra permission was not granted");
                                task.setDone(null);
                            }
                            context.unregisterReceiver(this);
                        } else {
                            Log.appendLine("Got a permission for an unexpected device");
                            task.setDone(null);
                        }
                    }
                } else {
                    Log.appendLine("Unexpected action");
                    task.setDone(null);
                }
            }
        };
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            // NOT_EXPORTED：系统发这个广播时会带上我们那个 PendingIntent，收得到；
            // 别的 App 伪造的发不进来。
            context.registerReceiver(broadcastReceiver, new IntentFilter(expectedAction), RECEIVER_NOT_EXPORTED);
        } else {
            context.registerReceiver(broadcastReceiver, new IntentFilter(expectedAction));
        }
    }

    private UsbPermissionObtainer() {}
}