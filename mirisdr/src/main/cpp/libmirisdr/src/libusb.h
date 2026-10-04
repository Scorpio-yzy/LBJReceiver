/*
 * libusb 兼容层（Android 专用）
 * ---------------------------------------------------------------------------
 * libmirisdr-5 需要 libusb 1.0.22+ 的两个接口：
 *     libusb_set_option(ctx, LIBUSB_OPTION_NO_DEVICE_DISCOVERY, NULL)
 *     libusb_wrap_sys_device(ctx, fd, &handle)      ← Android 上【唯一】能打开 USB 设备的方式
 * 而本项目自带的 libusb（Signalware 的 Android 移植，1.0.13）没有它们，
 * 它用的是自己的两个接口：
 *     libusb_get_device2(ctx, "/dev/bus/usb/001/002")   ← 从设备路径直接造 handle
 *     libusb_open2(dev, &handle, fd)                    ← 用 Java 层拿到的 fd 打开
 * 所以这里补一层映射：拿 fd -> /proc/self/fd/N 反查出设备路径 -> 走上面那两个接口。
 * 不动上游 libmirisdr 一行代码，也不换 libusb。
 *
 * ★ 这个文件必须叫 libusb.h 并放在 libmirisdr/src/ 下：libmirisdr 的源码写的是
 *   #include "libusb.h"（引号形式先搜自己所在目录），于是会先命中这里，
 *   再由这里去引真正的 <libusb.h>。
 */
#ifndef MIRI_LIBUSB_COMPAT_H
#define MIRI_LIBUSB_COMPAT_H

#include <libusb.h>

#ifndef LIBUSB_OPTION_NO_DEVICE_DISCOVERY
#define LIBUSB_OPTION_NO_DEVICE_DISCOVERY 0x0002
#endif

/* 空实现：这个选项只是让 libusb 别去枚举设备（Android 上没有权限枚举）。
 * 我们用的是 libusb_get_device2（按路径造 handle），本来就不枚举。 */
int LIBUSB_CALL libusb_set_option(libusb_context *ctx, int option, ...);

/* 对应上游的 libusb_wrap_sys_device：用 fd 造出 device handle。 */
int LIBUSB_CALL libusb_wrap_sys_device(libusb_context *ctx, intptr_t sys_dev,
                                       libusb_device_handle **dev_handle);

/* JNI 层把 Java 的 UsbDevice.getDeviceName() 传进来，作为反查失败时的兜底。 */
void mirisdr_set_android_device_path(const char *path);

#endif
