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

import static com.sdrtouch.tools.Check.isTrue;

import androidx.annotation.NonNull;

import java.util.concurrent.ExecutionException;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * This is a future task that will block until result has been returned
 */
public class AsyncFuture<V> implements Future<V> {
    private final Object locker = new Object();
    private V object;
    private boolean ready = false;

    @Override
    public boolean cancel(boolean mayInterruptIfRunning) {
        return false;
    }

    @Override
    public boolean isCancelled() {
        return false;
    }

    @Override
    public boolean isDone() {
        synchronized (locker) {
            return ready;
        }
    }

    @Override
    public V get() throws InterruptedException, ExecutionException {
        synchronized (locker) {
            // ★ 必须循环判 ready：USB 授权广播可能在调用方进入 get() 之前就已经 setDone()
            //   （用户点"允许"很快），那时 wait() 等的是下一次永远不会来的 notify ——
            //   打开线程永久卡住，权限接收器也一直挂着（现象：点启动后一直转圈）。
            while (!ready) locker.wait();
            return object;
        }
    }

    @Override
    public V get(long timeout, @NonNull TimeUnit unit) throws InterruptedException, ExecutionException, TimeoutException {
        long deadline = System.currentTimeMillis() + unit.toMillis(timeout);
        synchronized (locker) {
            while (!ready) {
                long left = deadline - System.currentTimeMillis();
                if (left <= 0) throw new TimeoutException("AsyncFuture 等待超时");
                locker.wait(left);
            }
            return object;
        }
    }

    public void setDone(V object) {
        synchronized (locker) {
            isTrue(!this.ready);
            this.object = object;
            this.ready = true;
            locker.notify();
        }
    }
}
