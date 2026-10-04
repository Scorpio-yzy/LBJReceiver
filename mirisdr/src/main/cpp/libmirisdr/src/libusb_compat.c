/* libusb 兼容层的实现，见同目录 libusb.h 的说明。 */
#include "libusb.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static char g_dev_path[256];

void mirisdr_set_android_device_path(const char *path)
{
    if (!path) {
        g_dev_path[0] = '\0';
        return;
    }
    strncpy(g_dev_path, path, sizeof(g_dev_path) - 1);
    g_dev_path[sizeof(g_dev_path) - 1] = '\0';
}

int LIBUSB_CALL libusb_set_option(libusb_context *ctx, int option, ...)
{
    (void)ctx;
    (void)option;
    return 0;
}

int LIBUSB_CALL libusb_wrap_sys_device(libusb_context *ctx, intptr_t sys_dev,
                                       libusb_device_handle **dev_handle)
{
    char link[64];
    char path[256];
    ssize_t n;

    if (!dev_handle)
        return LIBUSB_ERROR_INVALID_PARAM;
    *dev_handle = NULL;

    /* 先按上游的做法：从 /proc/self/fd/N 反查设备节点（通常是 /dev/bus/usb/BBB/DDD） */
    path[0] = '\0';
    snprintf(link, sizeof(link), "/proc/self/fd/%d", (int)sys_dev);
    n = readlink(link, path, sizeof(path) - 1);
    if (n > 0) {
        path[n] = '\0';
    } else {
        path[0] = '\0';
    }

    /* 反查不到就用 JNI 传进来的设备路径（Java 的 UsbDevice.getDeviceName()） */
    if (path[0] != '/') {
        strncpy(path, g_dev_path, sizeof(path) - 1);
        path[sizeof(path) - 1] = '\0';
    }
    if (path[0] != '/') {
        /* 两条路都没有：明确报"没有这个设备"，让上层给出可读的提示 */
        return LIBUSB_ERROR_NO_DEVICE;
    }

    libusb_device *dev = libusb_get_device2(ctx, path);
    if (!dev)
        return LIBUSB_ERROR_NO_DEVICE;

    return libusb_open2(dev, dev_handle, (int)sys_dev);
}
