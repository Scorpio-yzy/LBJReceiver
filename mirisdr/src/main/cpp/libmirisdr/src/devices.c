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

static mirisdr_device_t mirisdr_devices[] = {
    { 0x1df7, 0x2500, "Mirics MSi2500 default (e.g. VTX3D card)", "Mirics", "MSi2500"},
    { 0x1df7, 0x3000, "SDRplay RSP1A", "SDRPlay", "RSP1A"},
    { 0x1df7, 0x3010, "SDRplay RSP1A", "SDRPlay", "RSP2"},
    { 0x2040, 0xd300, "Hauppauge WinTV 133559 LF", "Hauppauge", "WinTV 133559 LF"},
    { 0x07ca, 0x8591, "AverMedia A859 Pure DVBT", "AverTV", "A859 Pure DVBT"},
    { 0x04bb, 0x0537, "IO-DATA GV-TV100 stick", "IO-DATA", "GV-TV100"},
    { 0x0511, 0x0037, "Logitec LDT-1S310U/J", "Logitec", "LDT-1S310U/J"}
};

static mirisdr_device_t *mirisdr_device_get (uint16_t vid, uint16_t pid) {
    size_t i;

    for (i = 0; i < sizeof(mirisdr_devices) / sizeof(mirisdr_device_t); i++) {
        if ((mirisdr_devices[i].vid == vid) && (mirisdr_devices[i].pid == pid)) return &mirisdr_devices[i];
    }

    return NULL;
}

/* počet dostupných zařízení */
uint32_t mirisdr_get_device_count (void) {
    ssize_t i, i_max;
    uint32_t ret = 0;
    libusb_context *ctx;
    libusb_device **list;
    struct libusb_device_descriptor dd;

#ifdef __ANDROID__
    /* LibUSB does not support device discovery on android */
    libusb_set_option(NULL, LIBUSB_OPTION_NO_DEVICE_DISCOVERY, NULL);
#endif

    libusb_init(&ctx);

    i_max = libusb_get_device_list(ctx, &list);

    for (i = 0; i < i_max; i++) {
        libusb_get_device_descriptor(list[i], &dd);

        if (mirisdr_device_get(dd.idVendor, dd.idProduct)) ret++;
    }

    libusb_free_device_list(list, 1);

    libusb_exit(ctx);

    return ret;
}

/* název zařízení */
const char *mirisdr_get_device_name (uint32_t index) {
    ssize_t i, i_max;
    size_t j = 0;
    libusb_context *ctx;
    libusb_device **list;
    struct libusb_device_descriptor dd;
    mirisdr_device_t *device = NULL;

#ifdef __ANDROID__
    /* LibUSB does not support device discovery on android */
    libusb_set_option(NULL, LIBUSB_OPTION_NO_DEVICE_DISCOVERY, NULL);
#endif

    libusb_init(&ctx);
    i_max = libusb_get_device_list(ctx, &list);

    for (i = 0; i < i_max; i++) {
        libusb_get_device_descriptor(list[i], &dd);

        if ((device = mirisdr_device_get(dd.idVendor, dd.idProduct)) &&
            (j++ == index)) {
            libusb_free_device_list(list, 1);
            libusb_exit(ctx);
            return device->name;
        }
    }

    libusb_free_device_list(list, 1);
    libusb_exit(ctx);

    return "";
}

/* vlastní implementace */
int mirisdr_get_device_usb_strings (uint32_t index, char *manufact, char *product, char *serial) {
    ssize_t i, i_max;
    size_t j = 0;
    libusb_context *ctx;
    libusb_device **list;
    struct libusb_device_descriptor dd;
    mirisdr_device_t *device = NULL;

#ifdef __ANDROID__
    /* LibUSB does not support device discovery on android */
    libusb_set_option(NULL, LIBUSB_OPTION_NO_DEVICE_DISCOVERY, NULL);
#endif

    libusb_init(&ctx);
    i_max = libusb_get_device_list(ctx, &list);

    for (i = 0; i < i_max; i++) {
        libusb_get_device_descriptor(list[i], &dd);

        if ((device = mirisdr_device_get(dd.idVendor, dd.idProduct)) &&
            (j++ == index)) {
            strcpy(manufact, device->manufacturer);
            strcpy(product, device->product);

            // because all known SDRplay clones do not have an EEPROM chip with the serial number,
            // use the physical location of the USB port

            char* serial_cursor = serial;
            serial_cursor += sprintf(serial_cursor, "%d:", libusb_get_bus_number(list[i]));

#if LIBUSBX_API_VERSION >= 0x01000102 
            uint8_t usb_path[16];
            int path_len = libusb_get_port_numbers(list[i], usb_path, sizeof(usb_path));
            if (path_len == LIBUSB_ERROR_OVERFLOW) { // array too small
                path_len = sizeof(usb_path);
            }

            for (int u = 0; u < path_len; u++) {
                serial_cursor += sprintf(serial_cursor, "%d.", usb_path[u]);
            }
            
#endif
            *(serial_cursor - 1) = '\0'; // remove last dot or :

            libusb_free_device_list(list, 1);
            libusb_exit(ctx);
            return 0;
        }
    }

    memset(manufact, 0, 256);
    memset(product, 0, 256);
    memset(serial, 0, 256);

    libusb_free_device_list(list, 1);
    libusb_exit(ctx);

    return -1;
}
