/*
 * Mirics MSi2500/MSi001（SDRplay RSP1 / RSP1A / RSP2 及同芯片克隆板）的 rtl_tcp 设备层。
 * Copyright (C) 2026 Scorpio-yzy
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * 结构照搬 :rtlsdr 模块的 rtlsdrdevice.c（Signalware Ltd, GPL-2.0-or-later）：
 *   - 网络侧复用同一份 sdrtcp.c（设备无关的 rtl_tcp 服务器）
 *   - 这里只做"设备层"：打开 Mirics 设备、把命令映射到 libmirisdr、把 IQ 喂给服务器
 * 与 RTL 的差别：
 *   - 用 mirisdr_open_fd() 按 Java 层拿到的 USB fd 打开（Android 上没有 /dev/bus/usb 权限）
 *   - 采样率：MSi2500 的 ADC 分频比只能是 4..14 的偶数，所以硬件最低只到 1.3 MSps，
 *     【给不出 960 kS/s】（libmirisdr 会把 960k 悄悄夹到 1.3M）。做法是向硬件要
 *     960k×2 = 1.92 MSps，再在驱动里 2 倍抽取回 960 kS/s，客户端看到的仍是标准 960k。
 *   - ISOC / BULK 两种取数方式在 Android 上哪个能用不一定：自检会各跑一次真收数据，
 *     哪个收到了就用哪个（见 miri_stream_test）。
 *   - 增益走 libmirisdr 的 LNA/Mixer 档位（tenths of dB）
 *   - PPM 校正映射成 24MHz 晶振频率微调
 */
#include <jni.h>
#include <android/log.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>

#include "mirisdr.h"
#include "sdrtcp.h"
#include "tcp_commands.h"

#define LOG_TAG "MiriSdrDriver"
#define MIRI_LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define MIRI_LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

/* 由本项目新增的 libusb 兼容层（libmirisdr/src/libusb.h + libusb_compat.c）提供：
 * 把 Java 的 UsbDevice.getDeviceName() 交给它，作为"按 fd 反查设备路径失败"时的兜底。 */
void mirisdr_set_android_device_path(const char *path);

#define MIRI_XTAL_HZ 24000000.0
#define MIRI_DEFAULT_RATE 960000
/* libmirisdr 的硬件下限（hard.h: MIRISDR_SAMPLE_RATE_MIN）：低于它会被夹上来 */
#define MIRI_HW_RATE_MIN 1300000

typedef struct {
    mirisdr_dev_t *dev;
    sdrtcp_t tcp;
    pthread_mutex_t lock;
    int streaming;
    /* 客户端要的速率 -> 硬件速率 = 客户端速率 × decim。抽数在 miri_read_cb 里做。 */
    volatile int decim;
    uint32_t client_rate;
    unsigned char *out;      /* 抽数输出缓冲（避免每块都 malloc） */
    uint32_t out_cap;
    unsigned char carry[4];  /* 上一块的最后 4 个字节：跨块那一个样点的窗口要用 */
    int carry_n;
} miri_device_t;

/* 自检用：只收数据不干别的，用来判断 ISOC / BULK 哪个能出数 */
typedef struct {
    mirisdr_dev_t *dev;
    volatile long bytes;
    volatile int done;
    int ms;
    struct timeval t0;
} miri_stream_probe_t;

/*
 * 客户端速率 -> 硬件速率 + 抽取倍数。
 * 960000 -> k=2 -> 1920000（÷2 = 960 kS/s）。k 取"硬件速率不低于下限"的最小整数倍，
 * 这样抽取永远是整数倍、实现简单（2 倍用 4 抽头滑动平均就够）。
 */
static int miri_plan_rate(uint32_t client_rate, int *decim)
{
    int k = 1;
    if (client_rate == 0)
        client_rate = MIRI_DEFAULT_RATE;
    while ((uint64_t) client_rate * (uint64_t) k < MIRI_HW_RATE_MIN)
        k++;
    *decim = k;
    return (int) ((uint64_t) client_rate * (uint64_t) k);
}

/* 这条连接支持哪些 rtl_tcp 命令（其余命令服务器会忽略） */
static jint SUPPORTED_COMMANDS[] = {
        TCP_SET_FREQ,
        TCP_SET_SAMPLE_RATE,
        TCP_SET_GAIN_MODE,
        TCP_SET_GAIN,
        TCP_SET_FREQ_CORRECTION,
        TCP_SET_IF_TUNER_GAIN,
        TCP_ANDROID_EXIT,
        TCP_ANDROID_GAIN_BY_PERCENTAGE,
};

/*
 * libmirisdr 的增益档位表是 0..102，一共 103 个，而且它【不看调用方缓冲大小】一路写完
 * （gain.c: for (i = 0; i <= 102; i++) gains[i] = i;）。
 *
 * ★ 这里踩过一次大坑：照着上游参考工具开了 int gains[64]，结果 mirisdr_get_tuner_gains()
 *   往栈上多写了 39 个 int（156 字节）。Android 默认开 -fstack-protector，行为就是
 *   【点一下自检 App 直接闪退，logcat 都来不及看】—— 现象"闪退"就是这么来的。
 * 所以：缓冲一律 ≥ 103；并且先问个数（传 NULL 只问不写），个数超了宁可不显示也不越界写。
 */
#define MIRI_GAINS_MAX 128

static int miri_get_gains(mirisdr_dev_t *dev, int *buf, int cap)
{
    int n = mirisdr_get_tuner_gains(dev, NULL);   /* 只问个数，不写缓冲 */
    if (n <= 0)
        return 0;
    if (n > cap) {
        MIRI_LOGE("增益档位 %d 个，超过缓冲 %d —— 拒绝读取（宁可不显示也不越界）", n, cap);
        return 0;
    }
    mirisdr_get_tuner_gains(dev, buf);
    return n;
}

/*
 * 自检步骤落盘。native 里崩了的话 logcat 是拿不到的（用户手里没有 adb），
 * 每一步都 fsync 后才往下走，这样崩掉那一刻"最后一行"就一定是崩在哪一步。
 * 正常跑完会写一行"自检结束"，App 侧据此判断上次是崩了还是正常结束。
 */
static void miri_trace(const char *path, const char *fmt, ...)
{
    if (path == NULL || path[0] == 0)
        return;
    FILE *f = fopen(path, "a");
    if (f == NULL)
        return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fflush(f);
    fsync(fileno(f));
    fclose(f);
}

static miri_device_t *as_dev(jlong p)
{
    return (miri_device_t *) (intptr_t) p;
}

/*
 * IQ 数据回调：这里顺带做 2 倍抽取。
 *
 * ★ 数据是 I,Q,I,Q… 交错的，所以"抽 2 倍"是【每 8 个字节出 2 个字节】：
 *   I 只跟 I 平均、Q 只跟 Q 平均。第一版把连续 4 个字节一起平均（等于 I 和 Q 混着算），
 *   通带直接掉了 4 dB、直流只值一半 —— 桌面测试（tools/test_miri_decim.py）当场抓出来了。
 *
 * 每个分量是 4 抽头滑动平均：I[k] = (I[2k]+I[2k+1]+I[2k+2]+I[2k+3])/4，
 * 零点正好在输入 fs/4（= 输出奈奎斯特 480 kHz）和 fs/2 上 —— 折回带里最脏的两处被压得
 * 最狠，而 35 kHz 的 LBJ 信号几乎不受影响。
 *
 * 输入长度是 1008 的整数倍（4 的倍数），所以每块的相位一致；跨块只差最后一个样点
 * （它的窗口要伸到下一块），用 carry 存最后 4 个字节补上。
 * 这样每块输出正好是输入字节数的一半 —— 客户端量到的是 960.0 kS/s，不是 958。
 */
static void miri_read_cb(unsigned char *buf, uint32_t len, void *ctx)
{
    miri_device_t *d = (miri_device_t *) ctx;
    if (d == NULL || d->dev == NULL || buf == NULL || len == 0)
        return;

    /* 只有 2 倍这一种。别的值原样转发：宁可速率不对让 App 报出来，也别静默丢一半。 */
    if (d->decim != 2 || len < 8) {
        sdrtcp_feed(&d->tcp, buf, len);
        return;
    }

    uint32_t need = len / 2 + 4;
    if (d->out_cap < need) {
        unsigned char *p = (unsigned char *) realloc(d->out, need);
        if (p == NULL) {
            MIRI_LOGE("抽取缓冲分配失败（%u 字节），本块原样转发", (unsigned) need);
            sdrtcp_feed(&d->tcp, buf, len);
            return;
        }
        d->out = p;
        d->out_cap = need;
    }

    uint32_t m = 0;
    /* 跨块的那一个样点：上一块最后 4 个字节 + 本块头 4 个字节 */
    if (d->carry_n == 4) {
        d->out[m++] = (unsigned char) ((d->carry[0] + d->carry[2] + buf[0] + buf[2] + 2) >> 2);
        d->out[m++] = (unsigned char) ((d->carry[1] + d->carry[3] + buf[1] + buf[3] + 2) >> 2);
    }
    for (uint32_t j = 0; (int) (j + 7) < (int) len; j += 4) {
        d->out[m++] = (unsigned char) ((buf[j] + buf[j + 2] + buf[j + 4] + buf[j + 6] + 2) >> 2);
        d->out[m++] = (unsigned char) ((buf[j + 1] + buf[j + 3] + buf[j + 5] + buf[j + 7] + 2) >> 2);
    }

    memcpy(d->carry, buf + len - 4, 4);
    d->carry_n = 4;

    if (m)
        sdrtcp_feed(&d->tcp, d->out, m);
}

/* 自检用：到点还没自己停就强行取消（设备一个字节都不给的时候只能靠它） */
static void *miri_stream_watchdog(void *arg)
{
    miri_stream_probe_t *s = (miri_stream_probe_t *) arg;
    int step = (s->ms + 500) / 50 + 2;
    for (int i = 0; i < step; i++) {
        usleep(50 * 1000);
        if (s->done)
            return NULL;
    }
    if (!s->done)
        mirisdr_cancel_async(s->dev);
    return NULL;
}

static void miri_stream_probe_cb(unsigned char *buf, uint32_t len, void *ctx)
{
    miri_stream_probe_t *s = (miri_stream_probe_t *) ctx;
    if (buf == NULL || len == 0)
        return;
    if (s->bytes == 0)
        gettimeofday(&s->t0, NULL);
    s->bytes += len;
    struct timeval now;
    gettimeofday(&now, NULL);
    double el = (double) (now.tv_sec - s->t0.tv_sec) + (double) (now.tv_usec - s->t0.tv_usec) / 1e6;
    if (el >= (double) s->ms / 1000.0)
        mirisdr_cancel_async(s->dev);   /* 收够了就停，不用等看门狗 */
}

/*
 * 真收一段数据，返回实测速率（S/s，按 I+Q 两字节一个样点算）。收不到就返回 0。
 * Android 上 ISOC / BULK 哪个能用没有定论，所以自检两种都试，答案跟着结果一起回去。
 * 返回 -1 表示起流都没起来（状态没复位或被占用），0 表示起了但没数据。
 */
static double miri_stream_test(mirisdr_dev_t *dev, const char *mode, int ms, long *bytes_out,
                               int *start_ret)
{
    miri_stream_probe_t s;
    memset(&s, 0, sizeof(s));
    s.dev = dev;
    s.ms = ms;

    if (mirisdr_set_transfer(dev, mode) != 0) {
        *bytes_out = 0;
        *start_ret = -100;
        return -1;
    }

    pthread_t wd;
    int has_wd = (pthread_create(&wd, NULL, miri_stream_watchdog, &s) == 0);
    int r = mirisdr_read_async(dev, miri_stream_probe_cb, &s, 0, 0);
    s.done = 1;
    if (has_wd)
        pthread_join(wd, NULL);

    *bytes_out = s.bytes;
    *start_ret = r;
    if (r != 0)
        return -1;
    if (s.bytes == 0)
        return 0;

    struct timeval end;
    gettimeofday(&end, NULL);
    double sec = (double) (end.tv_sec - s.t0.tv_sec) + (double) (end.tv_usec - s.t0.tv_usec) / 1e6;
    if (sec <= 0)
        return 0;
    return (double) (s.bytes / 2) / sec;
}

/* 自检时试出来的、能出数据的方式。openAsync 用它，省得再试一遍。 */
static char g_preferred_mode[8] = "ISOC";

static void miri_closed_cb(sdrtcp_t *tcp, void *ctx)
{
    miri_device_t *d = (miri_device_t *) ctx;
    (void) tcp;
    if (d != NULL && d->dev != NULL)
        mirisdr_cancel_async(d->dev);
}

static void miri_command_cb(sdrtcp_t *tcp, void *ctx, sdr_tcp_command_t *cmd)
{
    miri_device_t *d = (miri_device_t *) ctx;
    if (d == NULL || d->dev == NULL)
        return;
    pthread_mutex_lock(&d->lock);
    switch (cmd->command) {
        case TCP_SET_FREQ:
            MIRI_LOGI("set freq %u", (unsigned) cmd->parameter);
            if (mirisdr_set_center_freq(d->dev, cmd->parameter) != 0)
                MIRI_LOGE("set_center_freq(%u) 失败", (unsigned) cmd->parameter);
            break;
        case TCP_SET_SAMPLE_RATE: {
            /* 客户端要的是 960k，硬件给不出（下限 1.3M）—— 换成 960k×2 再抽回来。
             * 速率没变就【什么都不做】：改速率要在流里停/起，能不折腾就不折腾。 */
            int decim = 1;
            int hw = miri_plan_rate(cmd->parameter, &decim);
            if ((uint32_t) cmd->parameter == d->client_rate && decim == d->decim) {
                MIRI_LOGI("采样率没变（客户端 %u），跳过", (unsigned) cmd->parameter);
                break;
            }
            MIRI_LOGI("客户端要 %u S/s -> 硬件 %d S/s，抽取 %d 倍",
                      (unsigned) cmd->parameter, hw, decim);
            if (mirisdr_set_sample_rate(d->dev, (uint32_t) hw) != 0) {
                MIRI_LOGE("set_sample_rate(%d) 失败", hw);
            } else {
                d->client_rate = (uint32_t) cmd->parameter;
                d->decim = (decim == 2) ? 2 : 1;
            }
            break;
        }
        case TCP_SET_GAIN_MODE:
            mirisdr_set_tuner_gain_mode(d->dev, (int) cmd->parameter);
            break;
        case TCP_SET_GAIN:
            MIRI_LOGI("set gain %d (tenths dB)", (int) cmd->parameter);
            if (mirisdr_set_tuner_gain(d->dev, (int) cmd->parameter) != 0)
                MIRI_LOGE("set_tuner_gain(%d) 失败", (int) cmd->parameter);
            break;
        case TCP_ANDROID_GAIN_BY_PERCENTAGE: {
            /* 按百分比映射到 LNA 增益档（0~100 -> min..max） */
            int gains[MIRI_GAINS_MAX];
            int n = miri_get_gains(d->dev, gains, MIRI_GAINS_MAX);
            if (n > 0) {
                int idx = (int) (cmd->parameter * (uint32_t) (n - 1) / 100u);
                if (idx < 0) idx = 0;
                if (idx > n - 1) idx = n - 1;
                mirisdr_set_tuner_gain(d->dev, gains[idx]);
                MIRI_LOGI("gain by %%%u -> index %d (%d)", (unsigned) cmd->parameter, idx, gains[idx]);
            }
            break;
        }
        case TCP_SET_FREQ_CORRECTION: {
            /* ppm -> 24MHz 晶振频率（与 librtlsdr 的 ppm 语义一致） */
            double xtal = MIRI_XTAL_HZ * (1.0 + ((double) (int32_t) cmd->parameter) / 1e6);
            MIRI_LOGI("set ppm %d -> xtal %.0f", (int) (int32_t) cmd->parameter, xtal);
            if (mirisdr_set_xtal_freq(d->dev, (uint32_t) (xtal + 0.5)) != 0)
                MIRI_LOGE("set_xtal_freq 失败");
            break;
        }
        case TCP_ANDROID_EXIT:
            MIRI_LOGI("客户端要求关闭");
            sdrtcp_stop_serving_client(tcp);
            break;
        default:
            break;
    }
    pthread_mutex_unlock(&d->lock);
}

/* common.c 里的 JNI_OnLoad 会调用这个名字做一次性初始化
 *（RTL 模块是在它自己的 rtlsdrdevice.c 里定义的，这里必须补上，否则链接不过）。 */
void initialize(JNIEnv *env)
{
    (void) env;
    MIRI_LOGI("JNI_OnLoad：Mirics(MSi2500/MSi001) 驱动已加载");
}

JNIEXPORT jlong JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_initialize(JNIEnv *env, jobject thiz)
{
    (void) env;
    (void) thiz;
    miri_device_t *d = (miri_device_t *) calloc(1, sizeof(miri_device_t));
    if (d == NULL)
        return 0;
    pthread_mutex_init(&d->lock, NULL);
    sdrtcp_init(&d->tcp);
    MIRI_LOGI("initialize() -> %p", (void *) d);
    return (jlong) (intptr_t) d;
}

/*
 * 自检：只"打开 -> 设 960k -> 设频率 -> 读增益档 -> 关掉"，不串流。
 * 朋友的机器上跑一次，就能知道设备能不能用、960k 认不认。
 *
 * tracePath：自检步骤落盘文件（App 私有目录）。用户手里没有 adb，native 崩了只能靠
 * 这个文件把"崩在哪一步"带回来；正常跑完会写"自检结束"。
 */
JNIEXPORT jstring JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_probe(JNIEnv *env, jobject thiz, jint fd,
        jstring devicePath_, jstring tracePath_, jint hwFlavour)
{
    (void) thiz;
    char msg[1024];
    int n;
    mirisdr_dev_t *dev = NULL;
    const char *devicePath = devicePath_ ? (*env)->GetStringUTFChars(env, devicePath_, 0) : NULL;
    const char *tracePath = tracePath_ ? (*env)->GetStringUTFChars(env, tracePath_, 0) : NULL;

    msg[0] = 0;
    if (devicePath != NULL)
        mirisdr_set_android_device_path(devicePath);

    /* 前端波段表：0 = 通用 MSi2500 板，1 = SDRplay 三兄弟。由 Java 侧按 USB PID 判定。 */
    int hw = (hwFlavour == 1) ? MIRISDR_HW_SDRPLAY : MIRISDR_HW_DEFAULT;

    miri_trace(tracePath, "1 打开设备（fd=%d 波段表=%s 路径=%s）", (int) fd,
               hw == MIRISDR_HW_SDRPLAY ? "SDRplay" : "通用 MSi2500",
               devicePath != NULL ? devicePath : "(空)");

    int r = mirisdr_open_fd(&dev, (int) fd);
    if (devicePath != NULL)
        (*env)->ReleaseStringUTFChars(env, devicePath_, devicePath);
    if (r != 0 || dev == NULL) {
        snprintf(msg, sizeof(msg), "打开设备失败（mirisdr_open_fd 返回 %d）", r);
        MIRI_LOGE("%s", msg);
        miri_trace(tracePath, "1 打开设备失败：%d（自检结束）", r);
        if (tracePath != NULL) (*env)->ReleaseStringUTFChars(env, tracePath_, tracePath);
        return (*env)->NewStringUTF(env, msg);
    }
    miri_trace(tracePath, "2 设备已打开（claim 成功）");

    n = snprintf(msg, sizeof(msg), "已打开设备 ✓\n");
    r = mirisdr_set_hw_flavour(dev, (mirisdr_hw_flavour_t) hw);
    n += snprintf(msg + n, sizeof(msg) - n, "前端波段表设为 %s：%s（%d）\n",
                  hw == MIRISDR_HW_SDRPLAY ? "SDRplay" : "通用 MSi2500",
                  r == 0 ? "成功" : "失败", r);
    miri_trace(tracePath, "3 波段表已设：%s", hw == MIRISDR_HW_SDRPLAY ? "SDRplay" : "通用 MSi2500");

    r = mirisdr_set_sample_format(dev, "504_S8");
    n += snprintf(msg + n, sizeof(msg) - n, "8 位 IQ 采样格式：%s（%d）\n", r == 0 ? "成功" : "失败", r);
    miri_trace(tracePath, "4 采样格式已设");

    /* MSi2500 硬件下限 1.3 MSps：要 960k 会被 libmirisdr 悄悄夹到 1.3M。
     * 所以向硬件要 1.92M，再在驱动里 2 倍抽回 960k（客户端看到的仍是 960k）。 */
    int decim = 1;
    int hw_rate = miri_plan_rate(MIRI_DEFAULT_RATE, &decim);
    r = mirisdr_set_sample_rate(dev, (uint32_t) hw_rate);
    n += snprintf(msg + n, sizeof(msg) - n,
                  "硬件采样率 %d S/s（= 960k × %d）：%s（%d），回读 %u\n",
                  hw_rate, decim, r == 0 ? "成功" : "失败", r, (unsigned) mirisdr_get_sample_rate(dev));
    miri_trace(tracePath, "5 硬件采样率 %d 已设，回读 %u（客户端 960k = ÷%d）",
               hw_rate, (unsigned) mirisdr_get_sample_rate(dev), decim);

    r = mirisdr_set_center_freq(dev, 821237500u);
    n += snprintf(msg + n, sizeof(msg) - n, "设 821.2375 MHz：%s（%d），回读 %u\n",
                  r == 0 ? "成功" : "失败", r, (unsigned) mirisdr_get_center_freq(dev));
    miri_trace(tracePath, "6 频率已设，回读 %u", (unsigned) mirisdr_get_center_freq(dev));

    miri_trace(tracePath, "7 读增益档位…");
    int gains[MIRI_GAINS_MAX];
    int ng = miri_get_gains(dev, gains, MIRI_GAINS_MAX);
    miri_trace(tracePath, "8 增益档位 %d 个", ng);
    if (ng > 0) {
        n += snprintf(msg + n, sizeof(msg) - n, "增益档位 %d 个：", ng);
        for (int i = 0; i < ng && i < 12 && n < (int) sizeof(msg) - 16; i++)
            n += snprintf(msg + n, sizeof(msg) - n, "%d ", gains[i]);
        n += snprintf(msg + n, sizeof(msg) - n, "…（单位 0.1dB）\n");
    } else {
        n += snprintf(msg + n, sizeof(msg) - n, "读增益档位失败（%d）\n", ng);
    }

    /* ---------- 真收一段数据：ISOC 和 BULK 哪个能出数 ---------- */
    miri_trace(tracePath, "9 试收数据（ISOC）…");
    long b_iso = 0;
    int sr_iso = 0;
    double rate_iso = miri_stream_test(dev, "ISOC", 700, &b_iso, &sr_iso);
    miri_trace(tracePath, "10 ISOC：起流返回 %d，收到 %ld 字节，实测 %.0f S/s", sr_iso, b_iso,
               rate_iso);
    MIRI_LOGI("ISOC: start=%d bytes=%ld rate=%.0f", sr_iso, b_iso, rate_iso);

    miri_trace(tracePath, "11 试收数据（BULK）…");
    long b_bulk = 0;
    int sr_bulk = 0;
    double rate_bulk = miri_stream_test(dev, "BULK", 700, &b_bulk, &sr_bulk);
    miri_trace(tracePath, "12 BULK：起流返回 %d，收到 %ld 字节，实测 %.0f S/s", sr_bulk, b_bulk,
               rate_bulk);
    MIRI_LOGI("BULK: start=%d bytes=%ld rate=%.0f", sr_bulk, b_bulk, rate_bulk);

    const char *mode_pick;
    if (rate_iso > 0.0 && rate_iso >= rate_bulk)
        mode_pick = "ISOC";
    else if (rate_bulk > 0.0)
        mode_pick = "BULK";
    else
        mode_pick = "ISOC";      /* 两个都没数：按默认值起，失败原因看下面的说明 */
    snprintf(g_preferred_mode, sizeof(g_preferred_mode), "%s", mode_pick);

    n += snprintf(msg + n, sizeof(msg) - n, "\n真收一段数据（各 0.7 秒）：\n");
    n += snprintf(msg + n, sizeof(msg) - n, "  ISOC：%s\n",
                  rate_iso > 0 ? "收到数据" : (sr_iso == 0 ? "起流了但没数据" : "起流失败"));
    n += snprintf(msg + n, sizeof(msg) - n, "  BULK：%s\n",
                  rate_bulk > 0 ? "收到数据" : (sr_bulk == 0 ? "起流了但没数据" : "起流失败"));
    n += snprintf(msg + n, sizeof(msg) - n, "  采用的取数方式：%s\n", mode_pick);

    miri_trace(tracePath, "13 关闭设备…");
    mirisdr_close(dev);
    n += snprintf(msg + n, sizeof(msg) - n, "设备已正常关闭。");
    miri_trace(tracePath, "自检结束（全程没有崩溃）");
    MIRI_LOGI("probe 结果:\n%s", msg);
    if (tracePath != NULL) (*env)->ReleaseStringUTFChars(env, tracePath_, tracePath);
    return (*env)->NewStringUTF(env, msg);
}

JNIEXPORT jboolean JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_openAsync(
        JNIEnv *env, jobject thiz, jlong pointer, jint fd, jint gain, jlong samplingrate,
        jlong frequency, jint port, jint ppm, jint biast, jstring address_, jstring devicePath_,
        jint hwFlavour, jstring mode_)
{
    (void) thiz;
    (void) biast;            /* Mirics 芯片没有偏置供电（bias tee），忽略 */
    miri_device_t *d = as_dev(pointer);
    if (d == NULL)
        return JNI_FALSE;

    const char *devicePath = (*env)->GetStringUTFChars(env, devicePath_, 0);
    const char *address = (*env)->GetStringUTFChars(env, address_, 0);
    /* 取数方式用自检试出来的那个；没跑过自检就用默认 ISOC */
    const char *mode = NULL;
    int mode_from_jni = 0;
    if (mode_ != NULL) {
        mode = (*env)->GetStringUTFChars(env, mode_, 0);
        mode_from_jni = 1;
    }
    if (mode == NULL || (strcmp(mode, "BULK") != 0 && strcmp(mode, "ISOC") != 0))
        mode = g_preferred_mode;
    mirisdr_dev_t *dev = NULL;
    jboolean ok = JNI_FALSE;
    jclass clazz = (*env)->GetObjectClass(env, thiz);
    jmethodID announceOnOpen = (*env)->GetMethodID(env, clazz, "announceOnOpen", "()V");
    /* GetMethodID 失败会留下一个 pending 的 NoSuchMethodError；不清掉的话它会在 native
     * 返回 Java 时抛出，把这次"其实已经开流成功"的调用变成异常。清掉即可，功能不受影响。 */
    if ((*env)->ExceptionCheck(env))
        (*env)->ExceptionClear(env);

    mirisdr_set_android_device_path(devicePath);

    int r = mirisdr_open_fd(&dev, (int) fd);
    if (r != 0 || dev == NULL) {
        MIRI_LOGE("mirisdr_open_fd 失败: %d", r);
        goto rel_jni;
    }
    /* 这里原来调 mirisdr_get_device_name()：它在 Android 上要重新 libusb_init + 枚举整条
     * USB 总线（还需要设备在它那张表里），纯为打一行日志不值得 —— 换成直接打波段表。 */
    MIRI_LOGI("设备已打开（前端波段表 %s）", hwFlavour == 1 ? "SDRplay" : "通用 MSi2500");

    mirisdr_set_hw_flavour(dev, (mirisdr_hw_flavour_t)
            (hwFlavour == 1 ? MIRISDR_HW_SDRPLAY : MIRISDR_HW_DEFAULT));
    mirisdr_set_sample_format(dev, "504_S8");

    /* 客户端速率 -> 硬件速率：硬件给不出 960k（下限 1.3 MSps），要 960k×2 再抽回一半 */
    int decim = 1;
    uint32_t client_rate = (samplingrate > 0) ? (uint32_t) samplingrate
                                              : (uint32_t) MIRI_DEFAULT_RATE;
    int hw_rate = miri_plan_rate(client_rate, &decim);
    if (mirisdr_set_sample_rate(dev, (uint32_t) hw_rate) != 0)
        MIRI_LOGE("设采样率 %d 失败", hw_rate);
    else
        MIRI_LOGI("采样率：硬件 %d S/s（回读 %u），抽取 %d 倍 -> 客户端 %u S/s",
                  hw_rate, (unsigned) mirisdr_get_sample_rate(dev), decim, client_rate);

    if (mirisdr_set_transfer(dev, mode) != 0)
        MIRI_LOGE("设置取数方式 %s 失败", mode);
    MIRI_LOGI("取数方式：%s", mode);

    if (frequency > 0 && mirisdr_set_center_freq(dev, (uint32_t) frequency) != 0)
        MIRI_LOGE("设中心频率 %ld 失败", (long) frequency);

    if (ppm != 0) {
        double xtal = MIRI_XTAL_HZ * (1.0 + ((double) ppm) / 1e6);
        mirisdr_set_xtal_freq(dev, (uint32_t) (xtal + 0.5));
    }

    if (gain == 0) {
        mirisdr_set_tuner_gain_mode(dev, 0);      /* 自动 */
    } else {
        mirisdr_set_tuner_gain_mode(dev, 1);
        if (mirisdr_set_tuner_gain(dev, gain) != 0)
            MIRI_LOGE("设增益 %d 失败", gain);
        else
            MIRI_LOGI("增益设为 %.1f dB", gain / 10.0);
    }

    if (mirisdr_reset_buffer(dev) != 0)
        MIRI_LOGE("reset_buffer 失败");

    /* 网络侧：与 RTL 同一条服务器代码。dummy 的 magic/type/gains 只是握手信息。 */
    if (!sdrtcp_open_socket(&d->tcp, address, port, "MIRI", 5, 0)) {
        MIRI_LOGE("监听 %s:%d 失败", address, port);
        goto err;
    }

    pthread_mutex_lock(&d->lock);
    d->dev = dev;
    /* 抽取参数必须在 mirisdr_read_async 之前设好：回调立刻就会开始跑 */
    d->decim = (decim == 2) ? 2 : 1;
    d->client_rate = client_rate;
    d->carry_n = 0;
    pthread_mutex_unlock(&d->lock);

    sdrtcp_serve_client_async(&d->tcp, (void *) d, miri_command_cb, miri_closed_cb);

    if (mirisdr_read_async(dev, miri_read_cb, (void *) d, 0, 0) != 0) {
        MIRI_LOGE("启动异步读失败");
        goto err;
    }
    d->streaming = 1;
    MIRI_LOGI("开始串流");

    ok = JNI_TRUE;
    if (announceOnOpen != NULL)
        (*env)->CallVoidMethod(env, thiz, announceOnOpen);
    goto rel_jni;

err:
    if (dev != NULL) {
        mirisdr_close(dev);
        dev = NULL;
    }
    pthread_mutex_lock(&d->lock);
    d->dev = NULL;
    pthread_mutex_unlock(&d->lock);

rel_jni:
    (*env)->ReleaseStringUTFChars(env, devicePath_, devicePath);
    (*env)->ReleaseStringUTFChars(env, address_, address);
    if (mode_from_jni)
        (*env)->ReleaseStringUTFChars(env, mode_, mode);
    return ok;
}

/* 自检试出来的、能出数据的取数方式（"ISOC" / "BULK"）。App 启动驱动时照这个来。 */
JNIEXPORT jstring JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_preferredMode(JNIEnv *env, jobject thiz)
{
    (void) thiz;
    return (*env)->NewStringUTF(env, g_preferred_mode);
}

JNIEXPORT void JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_stop(JNIEnv *env, jobject thiz, jlong pointer)
{
    (void) env;
    (void) thiz;
    miri_device_t *d = as_dev(pointer);
    if (d == NULL)
        return;
    if (d->dev != NULL) {
        mirisdr_cancel_async(d->dev);
        d->streaming = 0;
    }
    sdrtcp_stop_serving_client(&d->tcp);
}

JNIEXPORT void JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_close(JNIEnv *env, jobject thiz, jlong pointer)
{
    (void) env;
    (void) thiz;
    miri_device_t *d = as_dev(pointer);
    if (d == NULL)
        return;
    if (d->dev != NULL) {
        mirisdr_cancel_async(d->dev);
        mirisdr_close(d->dev);
        d->dev = NULL;
    }
    if (d->out != NULL) {
        free(d->out);
        d->out = NULL;
        d->out_cap = 0;
    }
    sdrtcp_free(&d->tcp);
    pthread_mutex_destroy(&d->lock);
    free(d);
    MIRI_LOGI("设备已关闭并释放");
}

JNIEXPORT jobjectArray JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_getSupportedCommands(JNIEnv *env, jobject thiz)
{
    (void) thiz;
    int len = (int) (sizeof(SUPPORTED_COMMANDS) / sizeof(SUPPORTED_COMMANDS[0]));
    jclass stringClass = (*env)->FindClass(env, "java/lang/String");
    jobjectArray arr = (*env)->NewObjectArray(env, len, stringClass, NULL);
    for (int i = 0; i < len; i++) {
        char buf[16];
        snprintf(buf, sizeof(buf), "%d", (int) SUPPORTED_COMMANDS[i]);
        jstring s = (*env)->NewStringUTF(env, buf);
        (*env)->SetObjectArrayElement(env, arr, i, s);
        (*env)->DeleteLocalRef(env, s);
    }
    return arr;
}
