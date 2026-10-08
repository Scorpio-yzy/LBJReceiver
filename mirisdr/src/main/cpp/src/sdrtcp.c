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

#include <pthread.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <string.h>
#include <errno.h>


#include "sdrtcp.h"
#include "common.h"
#include "extbuffer.h"

#define FEED_SLEEP_IF_NOT_READY_MILLIS (500)

/* ★ 本项目改动：5 个缓冲太小了 —— 每个缓冲放一块 IQ（约 6KB），5 个也就 8 毫秒。
 * 客户端只要打一个嗝（音频 write 卡住、App 被系统降频）就会见底。改成 32 个，留出余量。 */
#define POOL_MAX_ELEMENTS (32)

#define STAGE_UNINITIALIZED (0)
#define STAGE_INITIALIZED (1)
#define STAGE_SOCKET_OPEN (2)
#define STAGE_CLIENT_OPEN (3)
#define STAGE_CLIENT_OPEN_STARTED_ASYNC (4)
#define STAGE_CLIENT_SERVING (5)
#define STAGE_NEEDS_STOPPING (6)

#define RETURN_FAILURE { sdrtcp_cleanup(obj); return 0; }
#define RETURN_SUCCESS { return 1; }
#define RETURN_AND_CLOSE { sdrtcp_cleanup(obj); return; }

typedef struct {
    char magic[4];
    uint32_t dongleType;
    uint32_t gainsCount;
} dongle_info_t;

static void sdrtcp_cleanup(sdrtcp_t * obj) {
    pthread_mutex_lock(&obj->state_locker);
    if (obj->state != STAGE_UNINITIALIZED) {
        obj->state = STAGE_UNINITIALIZED;

        LOGI("SdrTcp: Closing from state %d", obj->state);

        pool_free(&obj->workpool);
        if (obj->listen_socket != -1) {
            close(obj->listen_socket);
        }
        /* ★ 客户端 socket 以前只置 -1、从不 close —— 每服务完一个客户端就泄漏一个 fd。
         *   App 的设计是"客户端一走就重启服务"，反复连接最终会让 socket/accept 报 EMFILE，
         *   内置驱动再也起不来（现象是"驱动没起来"，而且怎么重试都不行）。 */
        if (obj->client_socket != -1) {
            shutdown(obj->client_socket, SHUT_RDWR);
            close(obj->client_socket);
        }

        obj->client_socket = -1;
        obj->listen_socket = -1;
    }
    pthread_mutex_unlock(&obj->state_locker);
}

static void commandListener(void *arg) {
    sdrtcp_t * obj = (sdrtcp_t *) arg;

    size_t left = 0;
    fd_set readfds;
    sdr_tcp_command_t cmd={0, 0};
    struct timeval tv= {1, 0};

    while(obj->state == STAGE_CLIENT_SERVING) {
        left = sizeof(cmd);
        while (left > 0 && obj->state == STAGE_CLIENT_SERVING) {
            FD_ZERO(&readfds);
            FD_SET(obj->client_socket, &readfds);
            tv.tv_sec = 1;
            tv.tv_usec = 0;
            int r = 0;

            if (obj->state == STAGE_CLIENT_SERVING) r = select(obj->client_socket + 1, &readfds, NULL, NULL, &tv);

            if (obj->state == STAGE_CLIENT_SERVING && r) {
                ssize_t received = recv(obj->client_socket, (char *) &cmd + (sizeof(cmd) - left), left, 0);

                /* ★ received == 0 是"对端关了写端"，不是错误也不是没数据：
                 *   以前只有 received == -1 才退出，于是 select 一直说可读、recv 一直返回 0、
                 *   left 永不减 —— 这条命令线程 100% CPU 空转（用户看到的是发热/掉电飞快）。
                 *   半关（shutdown(SHUT_WR)）在 rtl_tcp 客户端里很常见。 */
                if (received <= 0) {
                    LOGI("SdrTcp: commandListener 收到 EOF/错误（%d），收尾", (int) received);
                    obj->state = STAGE_NEEDS_STOPPING;
                    break;
                }
                left -= (size_t) received;
            }
        }

        if (obj->state == STAGE_CLIENT_SERVING && left == 0) {
            cmd.parameter = ntohl(cmd.parameter);
            obj->commandcb(obj, obj->ctx, &cmd);
            cmd.command = 0xff;
        }
    }

    LOGI("SdrTcp: Command listener thread exiting");
    pthread_exit(NULL);
}

static void serveClient(sdrtcp_t * obj) {
    LOGI("SdrTcp: Client has connected.");

    struct timeval tv= {1,0};
    fd_set writefds;

    while (obj->state == STAGE_CLIENT_SERVING) {
        extbuffer_t * buff;
        if (obj->state == STAGE_CLIENT_SERVING) {
            if ((buff = pool_get_wait_lock(&obj->workpool, 1, 1)) == NULL) {
                continue;
            }
        } else {
            break;
        }

        size_t index = 0;
        ssize_t bytessent = -1;
        size_t bytesleft = sizeof(uint16_t) * buff->size_valid_elements;
        uint8_t * data_to_send = (uint8_t *) buff->ushortbuffer;

        while(bytesleft > 0 && obj->state == STAGE_CLIENT_SERVING) {
            FD_ZERO(&writefds);
            FD_SET(obj->client_socket, &writefds);
            tv.tv_sec = 5;
            tv.tv_usec = 0;

            int r = 0;
            if (obj->state == STAGE_CLIENT_SERVING) {
                r = select(obj->client_socket + 1, NULL, &writefds, NULL, &tv);
                if (r == -1) {
                    int err = errno;
                    char *str_error = strerror(errno);
                    LOGI("SdrTcp: serveClient cannot select. Code %d, exception %s", err,
                         str_error);
                    obj->state = STAGE_NEEDS_STOPPING;
                }
                if (r && obj->state == STAGE_CLIENT_SERVING) {
                    bytessent = send(obj->client_socket, data_to_send + index, bytesleft, 0);
                    bytesleft -= bytessent;
                    index += bytessent;

                    if (bytessent == -1 && obj->state == STAGE_CLIENT_SERVING) {
                        int err = errno;
                        char *str_error = strerror(errno);
                        LOGI("SdrTcp: serveClient cannot send to client. Code %d, exception %s",
                             err, str_error);
                        obj->state = STAGE_NEEDS_STOPPING;
                    }
                }
            }
        }

        pool_get_unlock(&obj->workpool, 1, buff);
    }
}

static void sdrtcp_wait_for_client(sdrtcp_t * obj) {
    if (obj->state != STAGE_CLIENT_OPEN_STARTED_ASYNC) return;

    struct timeval tv = {1,0};
    fd_set readfds;
    struct sockaddr_in remote;

    obj->client_socket = -1;
    while(obj->state == STAGE_CLIENT_OPEN_STARTED_ASYNC) {

        int failure = 0;
        FD_ZERO(&readfds);
        FD_SET(obj->listen_socket, &readfds);
        tv.tv_sec = 1;
        tv.tv_usec = 0;

        int r = 0;
        if (obj->state == STAGE_CLIENT_OPEN_STARTED_ASYNC) {
            r = select(obj->listen_socket + 1, &readfds, NULL, NULL, &tv);
        }

        if (r && obj->state == STAGE_CLIENT_OPEN_STARTED_ASYNC) {
            socklen_t rlen = sizeof(remote);
            obj->client_socket = accept(obj->listen_socket, (struct sockaddr *) &remote, &rlen);
            if (obj->client_socket != -1) {
                obj->state = STAGE_CLIENT_OPEN;
            } else {
                LOGI("SdrTcp: Failed to talk to client");
                failure = 1;
            }
        }

        if (failure) return;
    }
}

static void tcp_server(void *arg) {
    sdrtcp_t * obj = (sdrtcp_t *) arg;

    LOGI("SdrTcp: Waiting for client...");
    sdrtcp_wait_for_client(obj);

    if (obj->state == STAGE_CLIENT_OPEN) {
        LOGI("SdrTcp: TCP server succesfully started and listening for clients!");
        pthread_t commandThread;

        pthread_attr_t attrs;
        pthread_attr_init(&attrs);
        pthread_attr_setdetachstate(&attrs, PTHREAD_CREATE_JOINABLE);
        pthread_create(&commandThread, &attrs, (void *) commandListener, (void *) obj);

        pthread_mutex_lock(&obj->state_locker);
        obj->state = STAGE_CLIENT_SERVING;
        pthread_mutex_unlock(&obj->state_locker);

        serveClient(obj);

        LOGI("SdrTcp: Waiting for command thread to die");
        void *status;
        pthread_join(commandThread, &status);
    }

    LOGI("SdrTcp: TCP server shutting down.");
    pthread_mutex_lock(&obj->state_locker);
    if (obj->state != STAGE_UNINITIALIZED) {
        pthread_mutex_unlock(&obj->state_locker);
        LOGI("SdrTcp: Closing sdrtcp due to main thread finishing");
        sdrtcp_cleanup(obj);
    } else {
        pthread_mutex_unlock(&obj->state_locker);
    }

    obj->closedcb(obj, obj->ctx);

    LOGI("SdrTcp: Server thread shut down");
    obj->worker_done = 1;      /* 必须在 closedcb 之后：调用方靠它判断"线程真的用完了" */
    pthread_exit(NULL);
}

int sdrtcp_open_socket(sdrtcp_t * obj, const char * address, int port, const char * dongleMagic, uint32_t dongleType, uint32_t gainsCount) {
    if (obj->state != STAGE_UNINITIALIZED) {
        LOGI("SdrTcp: Called sdrtcp_open_socket with unexpected state %d", obj->state);
        RETURN_FAILURE;
    }

    obj->listen_socket = -1;

    pthread_mutex_lock(&obj->state_locker);
    pool_init(&obj->workpool, POOL_MAX_ELEMENTS, EXTBUFF_TYPE_USHORT);
    pool_set_threads(&obj->workpool, 2);

    dongle_info_t dongle_info;
    memset(&dongle_info, 0, sizeof(dongle_info));
    memcpy(&dongle_info.magic, dongleMagic, 4);
    dongle_info.dongleType = htonl(dongleType);
    dongle_info.gainsCount = htonl(gainsCount);

    // Send the dongle info as the first thing
    extbuffer_t * buff = NULL;
    if ((buff = pool_get_wait_lock(&obj->workpool, 0, 1)) != NULL) {
        extbuffer_preparetohandle(buff, sizeof(dongle_info) / sizeof(uint16_t));
        memcpy((void *) buff->ushortbuffer, (void *) &dongle_info, sizeof(dongle_info));
        pool_get_unlock(&obj->workpool, 0, buff);
    }

    obj->state = STAGE_INITIALIZED;
    pthread_mutex_unlock(&obj->state_locker);

    struct sockaddr_in local;
    memset(&local,0,sizeof(local));

    local.sin_family = AF_INET;
    local.sin_port = htons(port);
    local.sin_addr.s_addr = inet_addr(address);

    if (obj->state == STAGE_INITIALIZED) obj->listen_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

    pthread_mutex_lock(&obj->state_locker);
    if (obj->listen_socket != -1) obj->state = STAGE_SOCKET_OPEN;
    pthread_mutex_unlock(&obj->state_locker);

    int r = 1;
    int success = 0;

    if (obj->state == STAGE_SOCKET_OPEN) {
        setsockopt(obj->listen_socket, SOL_SOCKET, SO_REUSEADDR, (char *) &r, sizeof(int));
        struct linger ling = {1, 0};
        setsockopt(obj->listen_socket, SOL_SOCKET, SO_LINGER, (char *) &ling, sizeof(ling));
        if (bind(obj->listen_socket, (struct sockaddr *) &local, sizeof(local)) == 0)  {
            r = fcntl(obj->listen_socket, F_GETFL, 0);
            r = fcntl(obj->listen_socket, F_SETFL, r | O_NONBLOCK);

            if (listen(obj->listen_socket, 1) == 0) {
                LOGI("SdrTcp: Listening on %s:%d", address, port);
                success = 1;
            }
        }
    }

    if (obj->state == STAGE_SOCKET_OPEN && success) RETURN_SUCCESS else {
        LOGI("SdrTcp: Closing sdrtcp due to sdrtcp_open_socket seeing state %d and success %d", obj->state, success);
        RETURN_FAILURE;
    }
}

void sdrtcp_serve_client_async(sdrtcp_t * obj, void * ctx, sdrtcp_command_callback commandcb, sdrtcp_closed_callback closedcb) {
    if (obj->state != STAGE_SOCKET_OPEN) {
        LOGI("SdrTcp: Wrong state when calling sdrtcp_serve_client_async. State is %d", obj->state);
        closedcb(obj, ctx);
        RETURN_AND_CLOSE;
    }

    // async start is imminent
    obj->state = STAGE_CLIENT_OPEN_STARTED_ASYNC;

    pthread_t worker_thread;

    obj->closedcb = closedcb;
    obj->commandcb = commandcb;
    obj->ctx = ctx;

    pthread_attr_t attrs;
    pthread_attr_init(&attrs);
    /* ★ 以前是 JOINABLE 但句柄是局部变量、永远没人 join —— 每轮连接泄漏一个线程栈/TCB。
     *   改 DETACHED 让它退出即回收；配套新增 worker_done 标志，
     *   让 sdrtcp_free() 能等到"线程真的走完（含 closedcb）"再销毁结构，
     *   而不是像以前那样直接 free —— 那是对 worker 仍在使用的内存动手（use-after-free）。 */
    pthread_attr_setdetachstate(&attrs, PTHREAD_CREATE_DETACHED);
    obj->worker_done = 0;
    pthread_create(&worker_thread, &attrs, (void *) tcp_server, (void *) obj);
}

void sdrtcp_stop_serving_client(sdrtcp_t * obj) {
    /* ★ state 的读改写必须持 state_locker：tcp_server 那边也是持锁把 state 置成
     *   STAGE_CLIENT_SERVING 的。不加锁时存在这种时序：
     *   stop 读到旧 state → 走 cleanup（pool_free、state 打回 UNINITIALIZED），
     *   紧接着 tcp_server 又把 state 写成 SERVING 并进 serveClient ——
     *   此时 workpool 已 free，pool_get 永远返回 NULL，serveClient 变成 100% CPU 死循环，
     *   而且这条服务线程再也退不出来（下一次启动就绑不上端口）。 */
    pthread_mutex_lock(&obj->state_locker);
    int st = obj->state;
    if (st == STAGE_UNINITIALIZED) {
        pthread_mutex_unlock(&obj->state_locker);
        LOGI("SdrTcp: Requested sdrtcp stop but already stopped");
        return;
    }
    if (st < STAGE_CLIENT_OPEN_STARTED_ASYNC) {
        pthread_mutex_unlock(&obj->state_locker);
        LOGI("SdrTcp: Requested sdrtcp stop and stopping now");
        sdrtcp_cleanup(obj);        /* 它自己会加锁 */
    } else {
        obj->state = STAGE_NEEDS_STOPPING;
        pthread_mutex_unlock(&obj->state_locker);
        LOGI("SdrTcp: Requested sdrtcp stop asynchroneously");
    }
}

// queue up data to send over the connection
int sdrtcp_feed(sdrtcp_t * obj, unsigned char  * buf, uint32_t len) {
    extbuffer_t * buff = NULL;
    int succesful = 0;

    if (obj->state == STAGE_CLIENT_SERVING) {
        pthread_mutex_lock(&obj->state_locker);
        if (obj->state == STAGE_CLIENT_SERVING) {
            /* ★ 本项目改动：这里【绝对不能等】。
             *
             * sdrtcp_feed() 是从 libusb 的完成回调里调用的，用 block=1 等空缓冲的话，
             * 客户端只要停顿十几毫秒，回调就原地阻塞 → USB 事件循环停摆 →
             * 整条流永久死掉（真机现象：手台贴近发射后"频谱卡住、App 还能点、但不再接收"，
             * 而且不会自愈，只能重开）。
             *
             * 宁可丢掉这一块（听起来就是一声轻微的咔），也必须让回调立刻返回。 */
            if ((buff = pool_get_wait_lock(&obj->workpool, 0, 0)) != NULL) {
                extbuffer_preparetohandle(buff, len);
                memcpy((void *) buff->ushortbuffer, (void *) buf,
                       sizeof(uint16_t) * len);
                pool_get_unlock(&obj->workpool, 0, buff);
                succesful = 1;
            } else {
                obj->dropped++;
                if ((obj->dropped % 200) == 1)
                    LOGI("SdrTcp: 客户端来不及取，已丢 %lu 块", obj->dropped);
                succesful = 2;   /* 2 = 没送出去（呼叫方本来就忽略返回值） */
            }
        } else if (obj->state == STAGE_SOCKET_OPEN || obj->state == STAGE_CLIENT_OPEN || obj->state == STAGE_CLIENT_OPEN_STARTED_ASYNC) {
            /* ★ 这里同样【不能等】：这是"客户端还没连上/刚断开"的那种状态。
             *   以前这里 usleep(500ms)，等于每个 IQ 块都把 libusb 事件循环按住半秒 ——
             *   现象是"点启动后要等好几秒才开始出数据、取消响应也慢"。
             *   与上面那段注释（绝不能等）本来就矛盾。直接丢弃。 */
            succesful = 2; // no client to send data to
        }
        pthread_mutex_unlock(&obj->state_locker);
    } else if (obj->state == STAGE_SOCKET_OPEN || obj->state == STAGE_CLIENT_OPEN || obj->state == STAGE_CLIENT_OPEN_STARTED_ASYNC) {
        succesful = 2; // no client to send data to（同样不能等，见上）
    }

    return succesful;
}

void sdrtcp_init(sdrtcp_t * obj) {
    obj->state = 0;
    pthread_mutex_init(&obj->state_locker, NULL);
    obj->client_socket = -1;
    obj->listen_socket = -1;
    obj->dropped = 0;
    obj->worker_done = 1;   /* 还没起服务线程 = 不需要等 */
}

void sdrtcp_free(sdrtcp_t * obj) {
    /* ★ 先请服务线程收尾，并等它真的退出（最多 2.5 秒）再销毁互斥量。
     *   以前这里只有 pthread_mutex_destroy：tcp_server / commandListener 可能还在跑，
     *   随后它们 pthread_mutex_lock 一个已销毁的互斥量、访问已 free 的结构 —— 
     *   退出/重连时随机崩溃或 pthread 断言。 */
    sdrtcp_stop_serving_client(obj);
    for (int i = 0; i < 250 && !obj->worker_done; i++)
        usleep(10 * 1000);
    if (!obj->worker_done)
        LOGI("SdrTcp: 服务线程 2.5 秒内没退出，继续释放（可能留下一个悬空线程）");
    pthread_mutex_destroy(&obj->state_locker);
}