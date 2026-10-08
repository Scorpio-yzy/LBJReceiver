/* libusb 兼容层的实现，见同目录 libusb.h 的说明。 */
#include "libusb.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

/* 由 libmirisdr.c 提供：把失败原因存成字符串，界面上显示用 */
extern void mirisdr_set_open_error(const char *fmt, ...);

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
        mirisdr_set_open_error("拿不到设备节点路径（/proc/self/fd 反查失败，且 App 也没传进来）");
        return LIBUSB_ERROR_NO_DEVICE;
    }

    libusb_device *dev = libusb_get_device2(ctx, path);
    if (!dev) {
        /* 这一步要读 /sys/bus/usb/devices 里的描述符：部分手机（SELinux）不让 App 读，于是设备对象建不出来 */
        mirisdr_set_open_error("从设备节点\"%s\"构造 libusb_device 失败（多半是 /sys/bus/usb 读不到）", path);
        return LIBUSB_ERROR_NO_DEVICE;
    }

    /*
     * ★ 本项目在 Android 上新增的改动：把 Java 交下来的 fd【复制一份】再给 libusb。
     *
     * libusb_close() 内部会 close(hpriv->fd)（linux_usbfs.c: op_close），而 Java 侧那个
     * UsbDeviceConnection 之后还会把自己的 fd 关一次 —— 直接用同一个 fd 就是双重关闭。
     * 两次 close 之间只要别的线程拿到同一个 fd 号，我们就会把【别人的】fd 关掉，
     * 现象是偶发的"这次驱动没起来、再点一次就好了"，极难查。
     *
     * dup 出来的 fd 指向同一个 open file description，UsbDeviceConnection 那边照旧有效，
     * 所有 USBDEVFS ioctl（claim / alt setting / 提交 URB）全都照常工作。
     */
    int myfd = dup((int) sys_dev);
    if (myfd < 0) {
        mirisdr_set_open_error("dup(fd=%d) 失败：fd 已失效", (int) sys_dev);
        return LIBUSB_ERROR_NO_DEVICE;
    }

    int rc = libusb_open2(dev, dev_handle, myfd);
    if (rc < 0) {
        /* ★ 失败时 libusb 的 op_open2 不会关 fd（成功路径才由 libusb_close 负责），
         *   这里也不关就漏一个 fd。反复点"启动驱动"最终 fd 表耗尽，
         *   之后连 socket/文件都开不了（连日志都写不出来，最难查）。 */
        close(myfd);
        mirisdr_set_open_error("libusb_open2(fd=%d) 失败：code %d (%s)",
                               (int) sys_dev, rc, libusb_error_name(rc));
    }
    return rc;
}
