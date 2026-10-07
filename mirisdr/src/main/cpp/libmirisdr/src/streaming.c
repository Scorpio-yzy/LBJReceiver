/*
 * Copyright (C) 2013 by Miroslav Slugen <thunder.m@email.cz
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

int mirisdr_streaming_start (mirisdr_dev_t *p) {
    if (!p) goto failed;
    if (!p->dh) goto failed;

    libusb_control_transfer(p->dh, 0x42, 0x43, 0x0, 0x0, NULL, 0, CTRL_TIMEOUT);

    return 0;

failed:
    return -1;
}

int mirisdr_streaming_stop (mirisdr_dev_t *p) {
    if (!p) goto failed;
    if (!p->dh) goto failed;

    /*
     * ★ 本项目改动：发"停止串流"命令之前必须等一会儿。
     *
     * 这一条是 Linux 内核的 msi2500 驱动里明确写出来的经验
     * （drivers/media/usb/msi2500/msi2500.c，msi2500_stop_streaming）：
     *     "according to tests, at least 700us delay is required"  → 它 msleep(20) 再发命令。
     *
     * 上游 libmirisdr 是【立刻】发 0x45 —— 设备还没把当前那一帧发完就被叫停，
     * 状态容易留在半路。我们的自检要连做好几轮启停（ISOC/BULK/两套波段表），
     * 正好是这条最容易暴露的用法。这里给 2ms（比 700us 宽裕，又不影响手感）。
     */
#if defined (_WIN32) && !defined(__MINGW32__)
    Sleep(2);
#else
    usleep(2000);
#endif

    libusb_control_transfer(p->dh, 0x42, 0x45, 0x0, 0x0, NULL, 0, CTRL_TIMEOUT);

    return 0;

failed:
    return -1;
}

