/*
 * Mirics MSi2500/MSi001（SDRplay RSP1 / RSP1A / RSP2）设备的 Java 侧入口。
 * Copyright (C) 2026 Scorpio-yzy
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * 与 :rtlsdr 的 RtlSdrDevice 同一个套路：USB 权限与 fd 由 App 层拿到，
 * 这里只把它交给 native（libmirisdr + 内置的 rtl_tcp 服务器）。
 */
package com.railfan.lbj.mirisdr;

public class MiriSdrDevice {
    static {
        System.loadLibrary("mirisdrdevice");
    }

    private final long handle;

    public MiriSdrDevice() {
        handle = initialize();
    }

    public long handle() {
        return handle;
    }

    /**
     * 自检：打开设备 -> 设 960 kS/s -> 设频率 -> 读增益档 -> 关掉，返回可读的结果文本。
     * 不串流，插上设备跑一次就知道能不能用。
     *
     * @param hwFlavour 0 = 通用 MSi2500 板，1 = SDRplay（RSP1/RSP1A/RSP2）。
     *                  两者的前端波段切换表不同，选错会"能打开但收不到信号"，
     *                  所以由 Java 侧按 USB PID 判定后传进来。
     */
    public native String probe(int fd, String devicePath, int hwFlavour);

    /** 打开设备并在 port 上起 rtl_tcp 服务（地址一般传 127.0.0.1）。 */
    public native boolean openAsync(long handle, int fd, int gain, long samplerate, long frequency,
                                    int port, int ppm, int biasT, String address, String devicePath,
                                    int hwFlavour);

    /**
     * native 侧在开流成功后回调一次（与 :rtlsdr 模块的 SdrDevice 保持同名）。
     * 这里没有上层抽象要通知，留个空实现 —— 但方法必须【存在】：
     * JNI 的 GetMethodID 找不到方法会抛 NoSuchMethodError 并且一直挂着，等 native
     * 返回 Java 时就把这次成功的 openAsync 变成一次异常（等于白开）。
     */
    public void announceOnOpen() {
    }

    /** 停流（保留设备句柄）。 */
    public native void stop(long handle);

    /** 关设备、释放句柄。 */
    public native void close(long handle);

    /** rtl_tcp 命令号里这条连接支持哪些。 */
    public native String[] getSupportedCommands();

    private native long initialize();
}
