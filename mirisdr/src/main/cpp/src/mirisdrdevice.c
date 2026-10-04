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
 *   - 采样率直接要 960000：MSi2500 自带分数重采样，能直接给出 960k；
 *     万一硬件给的不是 960k，App 那套"实测速率"自检会当场报出来
 *   - 增益走 libmirisdr 的 LNA/Mixer 档位（tenths of dB）
 *   - PPM 校正映射成 24MHz 晶振频率微调
 */
#include <jni.h>
#include <android/log.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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

typedef struct {
    mirisdr_dev_t *dev;
    sdrtcp_t tcp;
    pthread_mutex_t lock;
    int streaming;
} miri_device_t;

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

static miri_device_t *as_dev(jlong p)
{
    return (miri_device_t *) (intptr_t) p;
}

static void miri_read_cb(unsigned char *buf, uint32_t len, void *ctx)
{
    miri_device_t *d = (miri_device_t *) ctx;
    if (d == NULL || d->dev == NULL || buf == NULL || len == 0)
        return;
    sdrtcp_feed(&d->tcp, buf, len);
}

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
        case TCP_SET_SAMPLE_RATE:
            MIRI_LOGI("set sample rate %u", (unsigned) cmd->parameter);
            if (mirisdr_set_sample_rate(d->dev, cmd->parameter) != 0)
                MIRI_LOGE("set_sample_rate(%u) 失败", (unsigned) cmd->parameter);
            break;
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
            int gains[64];
            int n = mirisdr_get_tuner_gains(d->dev, gains);
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
 */
JNIEXPORT jstring JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_probe(JNIEnv *env, jobject thiz, jint fd, jstring devicePath_)
{
    (void) thiz;
    char msg[1024];
    int n;
    mirisdr_dev_t *dev = NULL;
    const char *devicePath = devicePath_ ? (*env)->GetStringUTFChars(env, devicePath_, 0) : NULL;

    msg[0] = 0;
    if (devicePath != NULL)
        mirisdr_set_android_device_path(devicePath);

    int r = mirisdr_open_fd(&dev, (int) fd);
    if (devicePath != NULL)
        (*env)->ReleaseStringUTFChars(env, devicePath_, devicePath);
    if (r != 0 || dev == NULL) {
        snprintf(msg, sizeof(msg), "打开设备失败（mirisdr_open_fd 返回 %d）", r);
        MIRI_LOGE("%s", msg);
        return (*env)->NewStringUTF(env, msg);
    }

    n = snprintf(msg, sizeof(msg), "已打开设备 ✓\n");
    r = mirisdr_set_hw_flavour(dev, MIRISDR_HW_SDRPLAY);
    n += snprintf(msg + n, sizeof(msg) - n, "硬件型号设为 SDRplay：%s（%d）\n", r == 0 ? "成功" : "失败", r);

    r = mirisdr_set_sample_format(dev, "504_S8");
    n += snprintf(msg + n, sizeof(msg) - n, "8 位 IQ 采样格式：%s（%d）\n", r == 0 ? "成功" : "失败", r);

    r = mirisdr_set_sample_rate(dev, MIRI_DEFAULT_RATE);
    n += snprintf(msg + n, sizeof(msg) - n, "设 960 kS/s：%s（%d），回读 %u\n",
                  r == 0 ? "成功" : "失败", r, (unsigned) mirisdr_get_sample_rate(dev));

    r = mirisdr_set_center_freq(dev, 821237500u);
    n += snprintf(msg + n, sizeof(msg) - n, "设 821.2375 MHz：%s（%d），回读 %u\n",
                  r == 0 ? "成功" : "失败", r, (unsigned) mirisdr_get_center_freq(dev));

    int gains[64];
    int ng = mirisdr_get_tuner_gains(dev, gains);
    if (ng > 0) {
        n += snprintf(msg + n, sizeof(msg) - n, "增益档位 %d 个：", ng);
        for (int i = 0; i < ng && i < 12 && n < (int) sizeof(msg) - 16; i++)
            n += snprintf(msg + n, sizeof(msg) - n, "%d ", gains[i]);
        n += snprintf(msg + n, sizeof(msg) - n, "…（单位 0.1dB）\n");
    } else {
        n += snprintf(msg + n, sizeof(msg) - n, "读增益档位失败（%d）\n", ng);
    }

    mirisdr_close(dev);
    n += snprintf(msg + n, sizeof(msg) - n, "设备已正常关闭。");
    MIRI_LOGI("probe 结果:\n%s", msg);
    return (*env)->NewStringUTF(env, msg);
}

JNIEXPORT jboolean JNICALL
Java_com_railfan_lbj_mirisdr_MiriSdrDevice_openAsync(
        JNIEnv *env, jobject thiz, jlong pointer, jint fd, jint gain, jlong samplingrate,
        jlong frequency, jint port, jint ppm, jint biast, jstring address_, jstring devicePath_)
{
    (void) thiz;
    (void) biast;            /* Mirics 芯片没有偏置供电（bias tee），忽略 */
    miri_device_t *d = as_dev(pointer);
    if (d == NULL)
        return JNI_FALSE;

    const char *devicePath = (*env)->GetStringUTFChars(env, devicePath_, 0);
    const char *address = (*env)->GetStringUTFChars(env, address_, 0);
    mirisdr_dev_t *dev = NULL;
    jboolean ok = JNI_FALSE;
    jclass clazz = (*env)->GetObjectClass(env, thiz);
    jmethodID announceOnOpen = (*env)->GetMethodID(env, clazz, "announceOnOpen", "()V");

    mirisdr_set_android_device_path(devicePath);

    int r = mirisdr_open_fd(&dev, (int) fd);
    if (r != 0 || dev == NULL) {
        MIRI_LOGE("mirisdr_open_fd 失败: %d", r);
        goto rel_jni;
    }
    MIRI_LOGI("设备已打开: %s", mirisdr_get_device_name(0) ? mirisdr_get_device_name(0) : "(未知)");

    mirisdr_set_hw_flavour(dev, MIRISDR_HW_SDRPLAY);
    mirisdr_set_sample_format(dev, "504_S8");

    if (samplingrate > 0 && mirisdr_set_sample_rate(dev, (uint32_t) samplingrate) != 0)
        MIRI_LOGE("设采样率 %ld 失败", (long) samplingrate);
    else
        MIRI_LOGI("采样率 %ld（回读 %u）", (long) samplingrate,
             (unsigned) mirisdr_get_sample_rate(dev));

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
    return ok;
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
