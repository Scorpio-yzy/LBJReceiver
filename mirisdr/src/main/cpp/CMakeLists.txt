cmake_minimum_required(VERSION 3.3.1)

# Mirics MSi2500/MSi001（SDRplay RSP1 / RSP1A / RSP2 及同芯片克隆板）的驱动，编译进 App。
# 上游：ericek111/libmirisdr-5（GPL-2.0-or-later，见 libmirisdr/COPYING）。
# 与 :rtlsdr 模块同一套做法：自己带的 libusb（LGPL）以 fd 方式打开 USB 设备。
PROJECT( "miriSdrAndroid" C )

ADD_SUBDIRECTORY(libusb)

# ★ libmirisdr 是【单编译单元】：libmirisdr.c 里 #include 了 reg.c / adc.c / async.c …
#   所以这里只编译它一个，不要去 glob src/*.c（会把 ref_tools 里的 CLI 工具也拉进来）。
SET ( SOURCES_LIB
    "${CMAKE_CURRENT_SOURCE_DIR}/libmirisdr/src/libmirisdr.c"
    "${CMAKE_CURRENT_SOURCE_DIR}/libmirisdr/src/libusb_compat.c" )

SET ( CMAKE_C_FLAGS         "${CMAKE_C_FLAGS} -Wall" )   # ★ 追加而不是覆盖：覆盖会丢掉 toolchain 默认的加固 flags（-fstack-protector 等）
SET ( CMAKE_C_FLAGS_DEBUG   "-g -DDEBUG" )
SET ( CMAKE_C_FLAGS_RELEASE "-O2 -DNDEBUG" )

FIND_LIBRARY( LOG_LIB log )

ADD_LIBRARY( mirisdr SHARED ${SOURCES_LIB} )
# ★ 只加 include，不加 libmirisdr/src：libmirisdr 的源码用 #include "libusb.h" 引号形式，
#   会先命中同目录下我们的兼容层 libusb.h，再由它去引真正的 <libusb.h>。
#   把 src 放进 -I 会让兼容层里的 <libusb.h> 又命中它自己，真头文件就永远进不来。
TARGET_INCLUDE_DIRECTORIES( mirisdr PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/libmirisdr/include" )

# ★ 打开 DETACH_KERNEL_DRIVER：有些手机的内核自带 MSi2500/MSi001 的 DVB 驱动
#   （msi2500 / dvb-usb-msi2500），会把接口先占住 —— 这时 libusb_claim_interface 必然失败，
#   表现就是"设备认得出来、但 mirisdr_open_fd 返回 -1"（真机反馈就是这样）。
#   打开之后 libmirisdr 会先 libusb_detach_kernel_driver() 把内核驱动摎掉再 claim。
TARGET_COMPILE_DEFINITIONS( mirisdr PRIVATE DETACH_KERNEL_DRIVER )
TARGET_LINK_LIBRARIES( mirisdr ${LOG_LIB} libusb_miri )

TARGET_LINK_OPTIONS( mirisdr PRIVATE
    "-Wl,-z,max-page-size=16384"
    "-Wl,-z,common-page-size=16384"
)

# ---------------------------------------------------------------------------
# 设备层 + rtl_tcp 服务器：src/ 下是"设备无关的服务器"（sdrtcp 等，来自 Signalware 的
# rtl_tcp_andro，GPL-2.0+）加上本项目的 mirisdrdevice.c（把命令映射到 libmirisdr、
# 把 IQ 喂给服务器）。App 只跟这一层打交道。
# ---------------------------------------------------------------------------
FILE ( GLOB_RECURSE SOURCES_DEVICE "src/*.c" )

ADD_LIBRARY( mirisdrdevice SHARED ${SOURCES_DEVICE} )
TARGET_INCLUDE_DIRECTORIES( mirisdrdevice PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/libmirisdr/include" )
TARGET_LINK_LIBRARIES( mirisdrdevice ${LOG_LIB} mirisdr libusb_miri )
TARGET_LINK_OPTIONS( mirisdrdevice PRIVATE
    "-Wl,-z,max-page-size=16384"
    "-Wl,-z,common-page-size=16384"
)
