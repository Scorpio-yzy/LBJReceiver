
/*
 * 8 bitový formát / 8-bit format
 * 1024 bajtů odpovídá 1008 hodnotám / 1024 bytes carry 1008 payload bytes:
 *    4 bajty  little-endian čítač vzorků, +504 na rámec
 *   12 bajtů  ostatní hlavička
 * 1008 bajtů  data (I,Q proloženě)
 *
 * ★ 本项目（LBJ Receiver）在 Android 上重写了这个转换器。
 *
 * 上游实现的前提是"src 从 1024 字节帧边界开始，且一次回调正好是若干整帧"。
 * 桌面 Linux 上勉强成立，Android 上不成立：
 *   · 走 ISOC 时每次回调只有"一个微帧有多少"（1.92 MS/s 下才几百字节），
 *     一帧必然被切开 —— 上游会把切开的帧丢掉，下一块又从错位处按 16 字节跳着解析；
 *   · 于是解析器把帧头当成数据、越界读 URB 里没被填过的内存。
 * 真机现象就是"自检说收到了数据、但 App 里收不到任何信号"。
 *
 * 这里改成带"半帧暂存 + 帧地址重对齐"的连续解析：
 *   · 不满一帧就留在 frame_buf 里等下一块；
 *   · 帧地址（每帧 +504）对不上 = 中间真丢了数据，在缓冲里搜到对得上的位置再对齐；
 *   · 搜不到（设备重启导致计数归零之类）就地重新对齐，绝不在原地打转。
 * 重组算法有桌面测试：tools/test_miri_framer.py。
 */
static int mirisdr_samples_convert_504_s8 (mirisdr_dev_t *p, unsigned char* src, uint8_t *dst, int cnt) {
    int ret = 0, off = 0;

    if (cnt <= 0) return 0;

    while (off < cnt) {
        /* 1) 先往暂存里灌数据（灌满缓冲为止；留出 1024 字节以上是为了能搜对齐） */
        int space = (int) sizeof(p->frame_buf) - p->frame_have;
        int take = cnt - off;
        if (take > space) take = space;
        if (take > 0) {
            memcpy(p->frame_buf + p->frame_have, src + off, take);
            p->frame_have += take;
            off += take;
        }

        /* 2) 缓冲里有整帧就往外吐，直到不足一帧 */
        while (p->frame_have >= 1024) {
            uint32_t addr = (uint32_t) p->frame_buf[0] | ((uint32_t) p->frame_buf[1] << 8) |
                            ((uint32_t) p->frame_buf[2] << 16) | ((uint32_t) p->frame_buf[3] << 24);

            if (p->frame_expected_known && addr != p->frame_expected) {
                int found = -1;
                int limit = p->frame_have - 1024;
                if (limit > 2048) limit = 2048;
                for (int o = 1; o <= limit; o++) {
                    uint32_t a2 = (uint32_t) p->frame_buf[o] | ((uint32_t) p->frame_buf[o + 1] << 8) |
                                  ((uint32_t) p->frame_buf[o + 2] << 16) | ((uint32_t) p->frame_buf[o + 3] << 24);
                    if (a2 == p->frame_expected) {
                        found = o;
                        break;
                    }
                }
                if (found > 0) {
                    memmove(p->frame_buf, p->frame_buf + found, p->frame_have - found);
                    p->frame_have -= found;
                    p->frame_lost += found / 1024;
                    p->sync_loss_cnt++;
                    continue;                       /* 对齐了，重新判这一帧 */
                }
                /* 对不上又搜不到：认了（计数可能重置），就地重新对齐 */
                p->sync_loss_cnt++;
                p->frame_expected_known = 0;
            }
            if (!p->frame_expected_known) {
                p->frame_expected = addr;
                p->frame_expected_known = 1;
            }

            if (!p->dbg_header_valid) {
                memcpy(p->dbg_header, p->frame_buf, 16);
                p->dbg_header_valid = 1;
            }

            memcpy(dst + ret, p->frame_buf + 16, 1008);
            ret += 1008;
            p->frame_expected += 504;

            memmove(p->frame_buf, p->frame_buf + 1024, p->frame_have - 1024);
            p->frame_have -= 1024;
        }

        /* 灌满了却一帧都吐不出来（缓冲被占死）——防死循环 */
        if (take == 0)
            break;
    }

    return ret;
}
