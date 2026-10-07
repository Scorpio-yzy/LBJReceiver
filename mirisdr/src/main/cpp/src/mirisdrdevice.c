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
    /* libmirisdr 的 S8 是【有符号】的，rtl_tcp/App 的约定是【无符号、直流 127.5】。
     * 不转换的话 App 会把负数样本当成 128..255，正负样本各偏一个方向 —— 波形整个撕开。
     * 到底要不要 +128，用开头几 KB 的直流均值自动判（有符号 ≈ 0，无符号 ≈ 128）。 */
    int add128;
    int dc_known;
    long dc_sum;
    int dc_count;
} miri_device_t;

/* 把一个原始字节搬到 rtl_tcp 的无符号约定（直流 127.5）上 */
static inline int miri_conv(unsigned char b, int add128)
{
    return add128 ? ((int) (int8_t) b + 128) : (int) b;
}

/* 自检用：只收数据不干别的，用来判断 ISOC / BULK 哪个能出数 */
typedef struct {
    mirisdr_dev_t *dev;
    volatile long bytes;
    volatile int done;
    int ms;
    struct timeval t_first, t_last;   /* 速率只按"第一块到最后一块"算，不含收尾等待 */
    /* 直流估计：有符号数据 ≈ 0，无符号数据 ≈ 128。自检报出来，判错了一眼就能看出来。 */
    long dc_sum;
    int dc_n;
    volatile int dc_mean;
    /* 每块（一次回调）的长度：BULK 应该是 1024 的整数倍；ISOC 按微帧给，通常不是 */
    long cb_count;
    uint32_t cb_min, cb_max;
    long cb_unaligned;
    /* 字节直方图：最常见字节的占比能看出"是不是常量/空数据" */
    long hist[256];
    long hist_n;
    int  hex_n;
    char hex[3 * 32 + 1];
    /* 活动度：|样本| 的峰值与均值。前端真正接进来时噪声底就有好几个 LSB，
     * 波段表选错（前端开关没接通）则几乎全是 0 —— 这是"哪套波段表对"最直接的判据。 */
    long abs_sum;
    long abs_n;
    int  peak_abs;
} miri_stream_probe_t;

/* 一次"真收数据"测试的结果，直接拿去拼自检报告 */
typedef struct {
    int started;              /* mirisdr_read_async 的返回值 */
    long bytes;
    double rate;              /* 复数样点/秒 */
    int dc_mean;
    long cb_count;
    uint32_t cb_min, cb_max;
    long cb_unaligned;
    int top_permille;         /* 最常见字节占比（千分比） */
    int sync_loss;            /* 504 帧解析丢帧计数 */
    /* 诊断用：手机上没法看 logcat，把关键字节摆到屏幕上 */
    int top_val[3];           /* 出现最多的前三个字节值 */
    int top_share[3];         /* 对应的千分比 */
    char hex_pay[3 * 32 + 1]; /* 载荷前 32 字节 */
    char hex_hdr[3 * 16 + 1]; /* 帧头 16 字节 */
    int hdr_valid;
    int peak_abs;             /* |样本| 峰值（有符号域） */
    int mean_abs_milli;       /* |样本| 均值 ×1000 */
} miri_stream_result_t;

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

    /* 开头几 KB 先量直流，判"有符号 / 无符号"（有符号数据均值 ≈ 0，无符号 ≈ 128）。
     * 量够之前按最可能的情况（有符号）先跑，判错了也只是开头几毫秒的事。 */
    if (!d->dc_known) {
        for (uint32_t i = 0; i < len; i++)
            d->dc_sum += buf[i];
        d->dc_count += (int) len;
        if (d->dc_count >= 8192) {
            int mean = (int) (d->dc_sum / d->dc_count);
            d->add128 = (mean >= 64) ? 0 : 1;
            d->dc_known = 1;
            MIRI_LOGI("实测直流均值 %d -> 按%s数据搬到 rtl_tcp 无符号约定", mean,
                      d->add128 ? "有符号（+128）" : "本来就是无符号");
        }
    }

    /*
     * ★ sdrtcp_feed() 的长度单位是【16 位元素个数】，不是字节！
     * 它的实现是 memcpy(..., sizeof(uint16_t) * len) 再按同样字节数发出去，
     * 上游 RTL 那条路写的是 sdrtcp_feed(..., len / 2)。传字节数会干两件坏事：
     *   ① 从样本缓冲里多读一倍（越界读）；
     *   ② 发给 App 的字节数翻倍，其中一半是内存垃圾 ——
     *      现象是"频谱在动、声音只有咔咔声，速率检测却刚好不报警"（因为字节速率恰好对）。
     */
    uint32_t need = len + 4;
    if (d->out_cap < need) {
        unsigned char *p = (unsigned char *) realloc(d->out, need);
        if (p == NULL) {
            MIRI_LOGE("抽取缓冲分配失败（%u 字节），本块原样转发", (unsigned) need);
            sdrtcp_feed(&d->tcp, buf, len / 2);
            return;
        }
        d->out = p;
        d->out_cap = need;
    }

    int add = d->add128;

    /* 不做抽取（只有非 960k 的客户端才会走到）时也要搬成无符号 */
    if (d->decim != 2 || len < 8) {
        for (uint32_t i = 0; i < len; i++)
            d->out[i] = (unsigned char) miri_conv(buf[i], add);
        sdrtcp_feed(&d->tcp, d->out, len / 2);
        return;
    }

    uint32_t m = 0;
    /* 跨块的那一个样点：上一块最后 4 个字节 + 本块头 4 个字节。
     * 平均在"无符号约定"那一侧做：有符号数据先 +128 再平均，
     * 因为 +128 是线性的，等价于"先平均再进行有符号→无符号"。 */
    if (d->carry_n == 4) {
        int si = miri_conv(d->carry[0], add) + miri_conv(d->carry[2], add)
                 + miri_conv(buf[0], add) + miri_conv(buf[2], add);
        int sq = miri_conv(d->carry[1], add) + miri_conv(d->carry[3], add)
                 + miri_conv(buf[1], add) + miri_conv(buf[3], add);
        d->out[m++] = (unsigned char) ((si + 2) >> 2);
        d->out[m++] = (unsigned char) ((sq + 2) >> 2);
    }
    for (uint32_t j = 0; (int) (j + 7) < (int) len; j += 4) {
        int si = miri_conv(buf[j], add) + miri_conv(buf[j + 2], add)
                 + miri_conv(buf[j + 4], add) + miri_conv(buf[j + 6], add);
        int sq = miri_conv(buf[j + 1], add) + miri_conv(buf[j + 3], add)
                 + miri_conv(buf[j + 5], add) + miri_conv(buf[j + 7], add);
        d->out[m++] = (unsigned char) ((si + 2) >> 2);
        d->out[m++] = (unsigned char) ((sq + 2) >> 2);
    }

    memcpy(d->carry, buf + len - 4, 4);
    d->carry_n = 4;

    /* m 恒为偶数（上面两处都是成对写的），所以 m/2 正好把 m 个字节发出去 */
    if (m >= 2)
        sdrtcp_feed(&d->tcp, d->out, m / 2);
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

    struct timeval now;
    gettimeofday(&now, NULL);
    if (s->bytes == 0)
        s->t_first = now;
    s->t_last = now;
    s->bytes += len;

    /* 每块长度统计：BULK 每块是整 16 KB（若干整帧），ISOC 是"一微帧多少给多少" */
    s->cb_count++;
    if (s->cb_min == 0 || len < s->cb_min)
        s->cb_min = len;
    if (len > s->cb_max)
        s->cb_max = len;
    if ((len % 1024) != 0)
        s->cb_unaligned++;

    if (s->dc_n < 32768) {
        for (uint32_t i = 0; i < len && s->dc_n < 32768; i++, s->dc_n++)
            s->dc_sum += buf[i];
        if (s->dc_n >= 16384)
            s->dc_mean = (int) (s->dc_sum / s->dc_n);
    }
    if (s->hist_n < 262144) {
        for (uint32_t i = 0; i < len && s->hist_n < 262144; i++, s->hist_n++)
            s->hist[buf[i]]++;
    }
    if (s->hex_n < 32) {
        for (uint32_t i = 0; i < len && s->hex_n < 32; i++, s->hex_n++) {
            snprintf(s->hex + s->hex_n * 3, 4, "%02x ", buf[i]);
        }
    }
    if (s->abs_n < 262144) {
        for (uint32_t i = 0; i < len && s->abs_n < 262144; i++, s->abs_n++) {
            int v = (int) (int8_t) buf[i];
            if (v < 0) v = -v;
            s->abs_sum += v;
            if (v > s->peak_abs) s->peak_abs = v;
        }
    }

    double el = (double) (now.tv_sec - s->t_first.tv_sec)
                + (double) (now.tv_usec - s->t_first.tv_usec) / 1e6;
    if (el >= (double) s->ms / 1000.0)
        mirisdr_cancel_async(s->dev);   /* 收够了就停，不用等看门狗 */
}

/*
 * 真收一段数据，把"速率 / 每块长度 / 是否对齐 / 直流 / 字节分布 / 丢帧"全量出来。
 *
 * 为什么要这么多：用户手里没有 adb，只有手机上一屏弹窗。这几个量合起来能一次说清
 *   · 速率不对      -> 传输方式或采样率的问题
 *   · 每块不是 1024 整数倍 -> libmirisdr 的 504 解析会错位（ISOC 就是这样）
 *   · 最常见字节占比很高   -> 数据是常量（设备没在采，或者帧内容为空）
 *   · 丢帧计数很大         -> 流里真丢数据
 */
static void miri_stream_test(mirisdr_dev_t *dev, const char *mode, int ms,
                             miri_stream_result_t *out)
{
    miri_stream_probe_t s;
    memset(&s, 0, sizeof(s));
    memset(out, 0, sizeof(*out));
    s.dev = dev;
    s.ms = ms;
    out->dc_mean = -1;

    if (mirisdr_set_transfer(dev, mode) != 0) {
        out->started = -100;
        return;
    }

    pthread_t wd;
    int has_wd = (pthread_create(&wd, NULL, miri_stream_watchdog, &s) == 0);
    int r = mirisdr_read_async(dev, miri_stream_probe_cb, &s, 0, 0);
    s.done = 1;
    if (has_wd)
        pthread_join(wd, NULL);

    out->started = r;
    out->bytes = s.bytes;
    out->cb_count = s.cb_count;
    out->cb_min = s.cb_min;
    out->cb_max = s.cb_max;
    out->cb_unaligned = s.cb_unaligned;
    if (s.dc_n > 0)
        out->dc_mean = (int) (s.dc_sum / s.dc_n);
    if (s.hist_n > 0) {
        for (int k = 0; k < 3; k++) {
            int best = -1;
            long bestv = -1;
            for (int v = 0; v < 256; v++) {
                int already = 0;
                for (int j = 0; j < k; j++)
                    if (out->top_val[j] == v) already = 1;
                if (already) continue;
                if (s.hist[v] > bestv) { bestv = s.hist[v]; best = v; }
            }
            out->top_val[k] = best;
            out->top_share[k] = (best >= 0) ? (int) (bestv * 1000 / s.hist_n) : 0;
        }
        out->top_permille = out->top_share[0];
    }
    out->peak_abs = s.peak_abs;
    out->mean_abs_milli = (s.abs_n > 0) ? (int) (s.abs_sum * 1000 / s.abs_n) : 0;
    memcpy(out->hex_pay, s.hex, sizeof(out->hex_pay));
    out->hex_pay[sizeof(out->hex_pay) - 1] = 0;
    out->sync_loss = mirisdr_get_sync_loss(dev);
    {
        unsigned char hdr[16];
        out->hdr_valid = mirisdr_get_last_frame_header(dev, hdr);
        if (out->hdr_valid) {
            for (int i = 0; i < 16; i++)
                snprintf(out->hex_hdr + i * 3, 4, "%02x ", hdr[i]);
            out->hex_hdr[3 * 16] = 0;
        } else {
            out->hex_hdr[0] = 0;
        }
    }

    if (r == 0 && s.bytes > 0) {
        double sec = (double) (s.t_last.tv_sec - s.t_first.tv_sec)
                     + (double) (s.t_last.tv_usec - s.t_first.tv_usec) / 1e6;
        if (sec > 0)
            out->rate = (double) (s.bytes / 2) / sec;   /* I+Q 两个字节一个复样点 */
    }
}

/* 自检时试出来的、能出数据的方式。openAsync 用它，省得再试一遍。 */
static char g_preferred_mode[8] = "ISOC";
/* 自检时比出来"前端接得进来"的那套波段表（0 = SDRplay，1 = 通用；-1 = 还没比过） */
static int g_hw_pick = -1;

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
        case TCP_SET_GAIN: {
            /* ★ rtl_tcp 协议里的增益是【0.1 dB】，而 libmirisdr 要的是【整数 dB 0..102】。
             * 直接下发（比如 App 的 18.0 dB -> 180）会被 libmirisdr 当成 180 dB 夹到 102，
             * 也就是永远最大增益 —— 强信号直接过载，频谱在动、声音只有咔咔声。 */
            int db = (int) (((int32_t) cmd->parameter + 5) / 10);
            MIRI_LOGI("set gain %d (0.1dB) -> %d dB", (int) cmd->parameter, db);
            if (mirisdr_set_tuner_gain(d->dev, db) != 0)
                MIRI_LOGE("set_tuner_gain(%d dB) 失败", db);
            break;
        }
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
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_probe(JNIEnv *env, jobject thiz, jlong pointer,
        jint fd, jstring devicePath_, jstring tracePath_, jint hwFlavour, jlong freqHz)
{
    (void) thiz;
    /* 这个 handle 是 App 那个长期存在的设备对象 —— sdrtcp 的"客户端来不及取就丢"计数
     * 就记在它身上，正好借自检把"手机处理不过来"这个数带出来。 */
    miri_device_t *dh = as_dev(pointer);
    char msg[2048];
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
        /* 把具体哪一步、什么错误码一并写出来 —— 用户只能给截图，
         * 只写"返回 -1"等于什么都没说。 */
        const char *why = mirisdr_last_open_error();
        snprintf(msg, sizeof(msg), "打开设备失败（mirisdr_open_fd 返回 %d）\n原因：%s",
                 r, (why && why[0]) ? why : "（无详情）");
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

    /* 用【界面上当前设的频率】而不是写死 821.2375：
     * 这样把频率调到一个本地强台（FM 广播常发）再点自检，就能靠"|样本| 峰值"看出
     * 前端通不通 —— 这是"只能靠截图"时最有价值的一个数。 */
    uint32_t probe_freq = (freqHz > 0) ? (uint32_t) freqHz : 821237500u;
    r = mirisdr_set_center_freq(dev, probe_freq);
    n += snprintf(msg + n, sizeof(msg) - n, "设 %.4f MHz：%s（%d），回读 %u\n",
                  probe_freq / 1e6, r == 0 ? "成功" : "失败", r,
                  (unsigned) mirisdr_get_center_freq(dev));
    miri_trace(tracePath, "6 频率 %u 已设，回读 %u", probe_freq,
               (unsigned) mirisdr_get_center_freq(dev));

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

    /*
     * ---------- 真收一段数据 ----------
     *
     * ★ 先试 BULK，不是 ISOC。原因：
     *   504 格式是"每 1024 字节一帧（16 字节帧头 + 1008 字节数据）"，libmirisdr 的解析器
     *   要求【每次回调拿到的正好是若干整帧】。BULK 是整块（16 KB）给，天然对齐；
     *   ISOC 是"每个微帧有多少给多少"（1.92 MS/s 时一微帧才几百字节），
     *   解析器会把 16 字节帧头当成数据、还会越界读下一包 —— 出来的是错位的垃圾。
     *   而且 ISOC 跑过之后接口往往还挂在 ISO 那档上，紧接着切 BULK 会失败，
     *   这就是上一版"ISOC 有数据、BULK 起流失败"的由来。
     */
    miri_stream_result_t res_iso, res_bulk;
    miri_trace(tracePath, "9 试收数据（ISOC）…");
    miri_stream_test(dev, "ISOC", 900, &res_iso);
    miri_trace(tracePath, "10 ISOC：起流=%d 字节=%ld 速率=%.0f 块数=%ld 块长 %u~%u 非1024倍数=%ld "
                          "直流=%d 最常见字节=%d‰ 丢帧=%d",
               res_iso.started, res_iso.bytes, res_iso.rate, res_iso.cb_count,
               res_iso.cb_min, res_iso.cb_max, res_iso.cb_unaligned, res_iso.dc_mean,
               res_iso.top_permille, res_iso.sync_loss);

    if (res_iso.rate > 0) {
        snprintf(g_preferred_mode, sizeof(g_preferred_mode), "ISOC");
        res_bulk = res_iso;
        res_bulk.rate = -1;      /* BULK 没试过，报告里标一下 */
    } else {
        /* ISOC 不行才试 BULK。注意这台设备上 BULK 的 alt 档切不过去，
         * 而且失败过的尝试会污染接口（这条已在 read_async 里修掉）——
         * 所以顺序是 ISOC 优先，BULK 只当兜底。 */
        miri_trace(tracePath, "11 试收数据（BULK，ISOC 没出数据才试）…");
        miri_stream_test(dev, "BULK", 900, &res_bulk);
        miri_trace(tracePath, "12 BULK：起流=%d 字节=%ld 速率=%.0f 块数=%ld 块长 %u~%u 非1024倍数=%ld "
                              "直流=%d 最常见字节=%d‰ 丢帧=%d",
                   res_bulk.started, res_bulk.bytes, res_bulk.rate, res_bulk.cb_count,
                   res_bulk.cb_min, res_bulk.cb_max, res_bulk.cb_unaligned, res_bulk.dc_mean,
                   res_bulk.top_permille, res_bulk.sync_loss);
        snprintf(g_preferred_mode, sizeof(g_preferred_mode), "%s",
                 res_bulk.rate > 0 ? "BULK" : "ISOC");
    }
    MIRI_LOGI("取数方式选定：%s", g_preferred_mode);

    /*
     * ---------- 两套前端波段表比一比 ----------
     *
     * libmirisdr 里 SDRplay 三兄弟和通用 MSi2500 板的前端开关/滤波寄存器是两套值
     * （soft.c 的 hw_switch_freq_plan_sdrplay / _default）。选错的那套会把前端通路
     * 断掉 —— 现象是"USB 上有数据、但 ADC 几乎什么都收不到"（载荷里绝大多数字节是 0）。
     *
     * 判据用【|样本| 峰值】：前端真接通时，光噪声底就有好几个 LSB；
     * 断开时基本全程是 0。自检两台都真收一次，谁峰值高用谁，并把结论记下来给 openAsync 用。
     */
    miri_stream_result_t fl[2];
    const int flav[2] = { MIRISDR_HW_SDRPLAY, MIRISDR_HW_DEFAULT };
    const char *flav_names[2] = { "SDRplay", "通用 MSi2500" };

    for (int i = 0; i < 2; i++) {
        mirisdr_set_hw_flavour(dev, (mirisdr_hw_flavour_t) flav[i]);
        mirisdr_set_sample_rate(dev, (uint32_t) hw_rate);
        mirisdr_set_center_freq(dev, probe_freq);
        /* 用 90 dB 来比：Mirics 的增益是"数值越大增益越高"，45 dB 时前端其实还没被推动，
         * 两套波段表的峰值会都贴在噪声底上（之前两台都报"峰值 4"就是这么来的）。 */
        mirisdr_set_tuner_gain(dev, 90);
        miri_stream_test(dev, g_preferred_mode, 700, &fl[i]);
        miri_trace(tracePath, "14 波段表 %s：速率=%.0f 峰值=%d 平均|x|=%d‰ 最常见=%d‰ 丢帧=%d 字节=%ld",
                   flav_names[i], fl[i].rate, fl[i].peak_abs, fl[i].mean_abs_milli,
                   fl[i].top_permille, fl[i].sync_loss, fl[i].bytes);
    }
    {
        /* 哪套波段表"看得见东西"就用哪套。
         * 要有 1.5 倍以上的差距才敢改判 —— 峰值本身有噪声，差不多的时侯按 USB PID 判就行。 */
        int pick;
        if (fl[1].peak_abs > fl[0].peak_abs * 3 / 2)
            pick = 1;                                   /* 通用板明显更好 */
        else if (fl[0].peak_abs > fl[1].peak_abs * 3 / 2)
            pick = 0;                                   /* SDRplay 明显更好 */
        else
            pick = (hw == MIRISDR_HW_SDRPLAY) ? 0 : 1;  /* 差不多，按 PID */
        g_hw_pick = pick;
        mirisdr_set_hw_flavour(dev, (mirisdr_hw_flavour_t) flav[pick]);
        MIRI_LOGI("波段表选定：%s（峰值 %d vs %d）", flav_names[pick], fl[pick].peak_abs,
                  fl[1 - pick].peak_abs);
    }

    /*
     * ---------- 带宽对比（只报告，不自动改）----------
     *
     * Linux 内核 msi2500 的 set_usb_adc() 会把调谐器带宽设成采样率（bandwidth = f_adc），
     * 而 libmirisdr 固定 8MHz。我们跑 1.92MSps + 8MHz，带外噪声偏多。
     * 但"收窄更好"不能靠猜：带宽窄了噪声底低、|样本| 峰值也跟着低，单看峰值反而会选宽的。
     * 所以这里只把两个带宽下的数摆出来，由人来判（有强信号时峰值由信号主导，噪声底差异
     * 会自己显形）。
     */
    miri_stream_result_t bw_wide, bw_narrow;
    /* 带宽枚举值：200K=0 300K=1 600K=2 1536K=3 5M=4 6M=5 7M=6 8M=7（见 libmirisdr 的 structs.h） */
    mirisdr_set_bandwidth(dev, 7);
    mirisdr_set_sample_rate(dev, (uint32_t) hw_rate);
    mirisdr_set_center_freq(dev, probe_freq);
    mirisdr_set_tuner_gain(dev, 90);
    miri_stream_test(dev, g_preferred_mode, 600, &bw_wide);
    mirisdr_set_bandwidth(dev, 3);
    mirisdr_set_sample_rate(dev, (uint32_t) hw_rate);
    mirisdr_set_center_freq(dev, probe_freq);
    mirisdr_set_tuner_gain(dev, 90);
    miri_stream_test(dev, g_preferred_mode, 600, &bw_narrow);
    mirisdr_set_bandwidth(dev, 7);                    /* 恢复默认，不改用户行为 */
    miri_trace(tracePath, "15 带宽对比：8MHz 峰值=%d 平均|x|=%d‰ / 1.5MHz 峰值=%d 平均|x|=%d‰",
               bw_wide.peak_abs, bw_wide.mean_abs_milli, bw_narrow.peak_abs, bw_narrow.mean_abs_milli);

    /* 写寄存器返回码：0 = 控制传输正常。上游把这些返回值全丢了，
     * 所以"自检全成功"并不等于设备真的在听我们说话。 */
    int ct = mirisdr_test_control_transfer(dev);
    int cs = mirisdr_test_streaming_start(dev);
    int drops = (dh != NULL) ? (int) dh->tcp.dropped : -1;
    miri_trace(tracePath, "13 控制传输：写寄存器 %d，开始串流 %d；驱动丢块 %d", ct, cs, drops);

    n += snprintf(msg + n, sizeof(msg) - n,
                  "\n控制传输：写寄存器 %d，开始串流 %d（0 = 正常，负数 = 设备没在听）\n", ct, cs);
    n += snprintf(msg + n, sizeof(msg) - n, "带宽对比（增益都用 90 dB）：8MHz 峰值 %d / 1.5MHz 峰值 %d\n",
                  bw_wide.peak_abs, bw_narrow.peak_abs);
    n += snprintf(msg + n, sizeof(msg) - n, "运行期间手机来不及取、驱动丢掉的块数：%d\n", drops);
    n += snprintf(msg + n, sizeof(msg) - n, "真收一段数据（应约 1.92 MS/s）：\n");

    const char *names[2] = {"ISOC", "BULK"};
    const miri_stream_result_t *rs[2] = {&res_iso, &res_bulk};
    const miri_stream_result_t *pick = NULL;
    for (int k = 0; k < 2; k++) {
        const miri_stream_result_t *x = rs[k];
        if (x->rate < 0)
            continue;                                    /* 这次没试 */
        if (x->rate > 0) {
            n += snprintf(msg + n, sizeof(msg) - n,
                          "  %s：%.2f MS/s，块 %ld 个（%u~%u 字节），丢帧 %d，直流均值 %d\n",
                          names[k], x->rate / 1e6, x->cb_count, x->cb_min, x->cb_max,
                          x->sync_loss, x->dc_mean);
            if (pick == NULL) pick = x;
        } else if (x->started == 0) {
            n += snprintf(msg + n, sizeof(msg) - n, "  %s：起流了但一个字节都没收到\n", names[k]);
        } else {
            n += snprintf(msg + n, sizeof(msg) - n, "  %s：起流失败（%d）\n", names[k], x->started);
        }
    }
    n += snprintf(msg + n, sizeof(msg) - n,
                  "  波段表对比（峰值越大=前端越接得进来）：SDRplay 峰值 %d，通用 峰值 %d\n",
                  fl[0].peak_abs, fl[1].peak_abs);
    n += snprintf(msg + n, sizeof(msg) - n, "  选用的波段表：%s\n", flav_names[g_hw_pick]);

    if (pick != NULL) {
        n += snprintf(msg + n, sizeof(msg) - n,
                      "  字节分布：%02x 占 %d‰，%02x 占 %d‰，%02x 占 %d‰\n",
                      pick->top_val[0], pick->top_share[0],
                      pick->top_val[1], pick->top_share[1],
                      pick->top_val[2], pick->top_share[2]);
        if (pick->hdr_valid)
            n += snprintf(msg + n, sizeof(msg) - n, "  帧头 16 字节：%s\n", pick->hex_hdr);
        n += snprintf(msg + n, sizeof(msg) - n, "  载荷前 32 字节：%s\n", pick->hex_pay);
    } else {
        n += snprintf(msg + n, sizeof(msg) - n,
                      "  两种取数方式都没收到数据 —— 把这一屏发我\n");
    }

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
        MIRI_LOGE("mirisdr_open_fd 失败: %d（%s）", r, mirisdr_last_open_error());
        goto rel_jni;
    }
    /* 这里原来调 mirisdr_get_device_name()：它在 Android 上要重新 libusb_init + 枚举整条
     * USB 总线（还需要设备在它那张表里），纯为打一行日志不值得 —— 换成直接打波段表。 */
    MIRI_LOGI("设备已打开（前端波段表 %s）", hwFlavour == 1 ? "SDRplay" : "通用 MSi2500");

    /* 波段表：自检如果比出哪套好就用哪套，否则按 USB PID 判定 */
    int use_flav = (g_hw_pick >= 0) ? g_hw_pick : (hwFlavour == 1 ? 1 : 0);
    mirisdr_set_hw_flavour(dev, (mirisdr_hw_flavour_t)
            (use_flav == 1 ? MIRISDR_HW_SDRPLAY : MIRISDR_HW_DEFAULT));
    MIRI_LOGI("前端波段表：%s（自检结论 %d）", use_flav == 1 ? "SDRplay" : "通用 MSi2500", g_hw_pick);
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

    /* gain 是从 rtl_tcp 语义来的【0.1 dB】，libmirisdr 要整数 dB（0..102） */
    if (gain == 0) {
        mirisdr_set_tuner_gain_mode(dev, 0);      /* 自动 */
    } else {
        int gain_db = (gain + 5) / 10;
        mirisdr_set_tuner_gain_mode(dev, 1);
        if (mirisdr_set_tuner_gain(dev, gain_db) != 0)
            MIRI_LOGE("设增益 %d dB 失败", gain_db);
        else
            MIRI_LOGI("增益设为 %d dB（收到 %d，单位 0.1dB）", gain_db, gain);
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
    /* 方向默认按"有符号"（libmirisdr 的 S8 就是有符号），第一块数据会自己纠正 */
    d->add128 = 1;
    d->dc_known = 0;
    d->dc_sum = 0;
    d->dc_count = 0;
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
    /* 失败也要把监听 socket 收掉：sdrtcp 的 listen socket 只有 stop/free 会关，
     * 不收的话端口一直占着，用户再点一次【启动驱动】必然绑不上（"驱动没起来"）。 */
    sdrtcp_stop_serving_client(&d->tcp);
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

/* 最后一次打开失败的确切原因（给界面显示）。 */
JNIEXPORT jstring JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_lastOpenError(JNIEnv *env, jobject thiz)
{
    (void) thiz;
    return (*env)->NewStringUTF(env, mirisdr_last_open_error());
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

/* 放开 USB 与监听端口，但【保留】这个句柄，之后还能再 openAsync 复用。
 *
 * 为什么不用 close()：close() 会把 d 整个 free 掉，而 sdrtcp 的 worker 线程
 * (tcp_server / commandListener) 还拿着 d（cleanup 之后还要调 closedcb(obj, ctx)），
 * 另外 sdrtcp_stop_serving_client() 在"已经在服务"时只是置 STAGE_NEEDS_STOPPING，
 * 真正收尾是异步的 —— 这时候 free 就是 use-after-free。
 * 自检只要把【接口】让出来（mirisdr_close -> libusb_release_interface + libusb_close）
 * 就够了，句柄留着最省事也最安全。 */
JNIEXPORT void JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_releaseUsb(JNIEnv *env, jobject thiz, jlong pointer)
{
    (void) env;
    (void) thiz;
    miri_device_t *d = as_dev(pointer);
    if (d == NULL)
        return;
    if (d->dev != NULL) {
        mirisdr_cancel_async(d->dev);
        mirisdr_close(d->dev);          /* 内核接口在这里被放掉 */
        pthread_mutex_lock(&d->lock);
        d->dev = NULL;
        d->streaming = 0;
        d->carry_n = 0;
        pthread_mutex_unlock(&d->lock);
    }
    /* 监听端口也一起收掉：不收的话自检完再点【启动驱动】会绑不上 1234。
     * 这一步在"正在服务"时是异步收尾，所以下面绝不能紧接着 free(d)。 */
    sdrtcp_stop_serving_client(&d->tcp);
    /* 等监听 socket 真的关掉（最多 1.2 秒）。不等的话用户紧接着点【启动驱动】，
     * sdrtcp_open_socket 会撞上"还在收尾"的状态直接失败（= 又冒出一句"驱动没起来"）。
     * 这里只等待、不释放任何东西，worker 线程还在跑也安全。 */
    for (int i = 0; i < 120 && d->tcp.listen_socket != -1; i++)
        usleep(10 * 1000);
    MIRI_LOGI("已释放 USB 与监听端口（句柄保留，可再 openAsync）");
}

/* 彻底销毁（App 退出进程前才该用）。注意：正在服务时 sdrtcp 的 worker 线程
 * 仍持有 d，别在流还没停干净时调它 —— 自检/换设备请用上面的 releaseUsb()。 */
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
