/*
 * Copyright (C) 2013 by Miroslav Slugen <thunder.m@email.cz
 * Copyright (C) 2025 by Peter Hackenberg <170885528+Peter3579@users.noreply.github.com>
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

#include "gain.h"

int mirisdr_set_gain(mirisdr_dev_t *p)
{
    uint32_t reg1 = 1, reg6 = 6;
#if MIRISDR_DEBUG >= 1
    fprintf(stderr,
#if MIRISDR_DEBUG >= 3
        "mirisdr_set_gain:\n"
#endif
        "mirisdr tuner gain: %d dB (band: %d, "
        "attenuations: baseband: %d, lna: %d, mixbuffer: %d,"
        " mixer: %d)\nmirisdr ",
        mirisdr_get_tuner_gain(p), p->band, p->gain_reduction_baseband,
        p->gain_reduction_lna, p->gain_reduction_mixbuffer,
        p->gain_reduction_mixer);
    if (p->dc_mode == MIRISDR_DC_STATIC)
        fprintf(stderr,"dc-cal: off");
    else if (p->dc_mode == MIRISDR_DC_CONTINUOUS)
        fprintf(stderr,"dc-cal: continuous");
    else if (p->dc_mode > MIRISDR_DC_CONTINUOUS)
        fprintf(stderr,"dc-cal: INVALID mode");
    else if (p->dc_mode == MIRISDR_DC_ONE_SHOT)
        fprintf(stderr,"dc-cal: %d µs once", 12 * p->dc_track);
        // Calculation is only valid for 24 MHz XTAL.
    else
        fprintf(stderr,"dc-cal: %d µs @ %.1f Hz",
             3 * p->dc_track * p->dc_mode,
             1e6 / (3. * p->dc_period * p->dc_mode));
    fprintf(stderr, " (mode: %d, speedup: %d, track: %d, period: %d)\n",
        p->dc_mode, p->dc_speedup, p->dc_track, p->dc_period);
#endif
// Reset to 0xf380 to enable gain control added Dec 5 2014 SM5BSZ
//    mirisdr_write_reg(p, 0x08, 0xf380);

    /* Receiver Gain Control */
    /* 0-3 => registr */
    /* 4-9 => baseband, 0 - 59, 60-63 je stejné jako 59 */
    /* 10-11 => mixer gain reduction pouze pro AM režim */
    /* 12 => mixer gain reduction -19dB */
    /* 13 => lna gain reduction -24dB */
    /* 14-16 => DC kalibrace */
    /* 17 => zrychlená DC kalibrace */
    reg1 |= p->gain_reduction_baseband << 4;

    // Mixbuffer is on AM1 and AM2 inputs only
    if (p->band == MIRISDR_BAND_AM1)
    {
        reg1 |= (p->gain_reduction_mixbuffer & 0x03) << 10;
    }
    else if (p->band == MIRISDR_BAND_AM2)
    {
        reg1 |= (p->gain_reduction_mixbuffer == 0 ? 0x0 : 0x03) << 10;
    }
    else
    {
        reg1 |= 0x0 << 10;
    }

    reg1 |= p->gain_reduction_mixer << 12;

    // LNA is not on AM1 nor AM2 inputs
    if ((p->band == MIRISDR_BAND_AM1) || (p->band == MIRISDR_BAND_AM2))
    {
        reg1 |= 0x0 << 13;
    }
    else
    {
        reg1 |= p->gain_reduction_lna << 13;
    }

    reg1 |= (p->dc_mode & 0x7) << 14;
    reg1 |= ((p->dc_speedup)? MIRISDR_DC_OFFSET_CALIBRATION_SPEEDUP_ON :
                              MIRISDR_DC_OFFSET_CALIBRATION_SPEEDUP_OFF) << 17;
    mirisdr_write_reg(p, 0x09, reg1);

    /* DC Offset Calibration setup */
    reg6 |= (p->dc_track & 0x3f) << 4;
    reg6 |= (p->dc_period & 0xfff) << 10;
    mirisdr_write_reg(p, 0x09, reg6);
//// set to 0xf300 to select AM input added Dec 5 2014 SM5BSZ
//    if (p->freq < 50000000)
//      {
//      mirisdr_write_reg(p, 0x08, 0xf300);
//      }
//    else
//      {
//      if (p->freq >= 108000000)
//        {
//// Nothing between 00 and 0xff helps to switch in signals above 108 MHz.
////        mirisdr_write_reg(p, 0x08, 0xf3ff);
//        }
//      }

    return 0;
}

/*
 * Provide list of available gain settings.
 * Used e.g. from gnuradio-osmosdr/lib/miri
 * The (first) call with *gains==NULL returns number of available gains.
 * The (second) call with *gains!=NULL fills the array and returns the count.
 *
 * The max. available gain depends on the selected band, but is always <= 102.
 * TODO: make this dependent on the band/frequency setting.
 */
int mirisdr_get_tuner_gains(mirisdr_dev_t *p, int *gains)
{
    int i;
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_tuner_gains: %p (band: %d)\n", gains, p->band);
#endif
    i = 103;
    if (gains)
    {
        for (i = 0; i <= 102; i++)
        {
            gains[i] = i;
        }
    }

    return i;
}

int mirisdr_set_tuner_gain(mirisdr_dev_t *p, int gain)
{
    p->gain = gain;
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_set_tuner_gain: %d dB (band: %d)\n", gain, p->band);
#endif
    if (!p)
    {
        fprintf(stderr, "mirisdr_set_tuner_gain: error: nil device pointer!\n");
        return -1;
    }
    /*
     * Pro VHF režim je lna zapnutý +24dB, mixer +19dB a baseband
     * je možné nastavovat plynule od 0 - 59 dB, z toho je maximální
     * zesílení 102 dB
     *
     * For VHF mode LNA is turned on to + 24 db, mixer to + 19 dB and baseband
     * can be adjusted continuously from 0 to 59 db, of which the maximum gain of 102 db
     */
    if (p->gain > 102)
    {
        p->gain = 102;
    }
    else if (p->gain < 0)
    {
        goto gain_auto;
    }

    /* Nejvyšší citlivost vždy bez redukce mixeru a lna */
    /* Always the highest sensitivity without reducing the mixer and LNA */
    if (p->gain >= 43)
    {
        p->gain_reduction_lna = 0;
        p->gain_reduction_mixbuffer = 0; // LNA equivalent for AM inputs
        p->gain_reduction_mixer = 0;
        p->gain_reduction_baseband = 59 - (p->gain - 43);
    }
    else if (p->gain >= 19)
    {
        p->gain_reduction_lna = 1;
        p->gain_reduction_mixbuffer = 3; // LNA equivalent for AM inputs (AM1: 18dB / AM2: 24 dB)
        p->gain_reduction_mixer = 0;
        p->gain_reduction_baseband = 59 - (p->gain - 19);
    }
    else
    {
        p->gain_reduction_lna = 1;
        p->gain_reduction_mixbuffer = 3; // LNA equivalent for AM inputs (AM1: 18dB / AM2: 24 dB)
        p->gain_reduction_mixer = 1;
        p->gain_reduction_baseband = 59 - p->gain;
    }

    return mirisdr_set_gain(p);

    gain_auto: return mirisdr_set_tuner_gain_mode(p, 0);
}

/* gain 0 corresponds to the maximal attenuated RF input */
int mirisdr_get_tuner_gain(mirisdr_dev_t *p)
{
    int gain = 0;

    if (p->gain < 0)
        goto gain_auto;

    gain += 59 - p->gain_reduction_baseband;

    if ((p->band == MIRISDR_BAND_AM1) || (p->band == MIRISDR_BAND_AM2))
    {
        gain += mirisdr_get_mixbuffer_gain(p);
    }
    else
    {
        gain += mirisdr_get_lna_gain(p);
    }

    if (!p->gain_reduction_mixer) {
        gain += 19;
    }

#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_tuner_gain: %d dB (band: %d)\n", gain, p->band);
#endif
    return gain;

    gain_auto:
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_tuner_gain: -1 (no automatic gain)\n");
#endif
        return -1;
}

/*
 * Used e.g. from gnuradio-osmosdr/lib/miri
 * mode==false is "automatic", mode==true is "manual".
 * Returns 0 on success, -1 on error.
 * Returns error (-1) if automatic mode is requested.
 */
int mirisdr_set_tuner_gain_mode(mirisdr_dev_t *p, int mode)
{
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_set_tuner_gain_mode: %d (%s)\n", mode, (mode)?"manual":"automatic -> rejected");
#endif
//    if (!mode) {
//        p->gain = -1;
//#if MIRISDR_DEBUG >= 1
//        fprintf( stderr, "gain mode: auto\n");
//#endif
//        mirisdr_write_reg(p, 0x09, 0x014281);
//        mirisdr_write_reg(p, 0x09, 0x3FFFF6);
//    } else if (p->gain < 0) {
//#if MIRISDR_DEBUG >= 1
//        fprintf( stderr, "gain mode: manual\n");
//#endif
//        p->gain = DEFAULT_GAIN;
//    }

    return (mode) ? 0 : -1;
}

int mirisdr_get_tuner_gain_mode(mirisdr_dev_t *p)
{
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_tuner_gain_mode: %d\n", 1);
#endif
//    return (p->gain < 0) ? 0 : 1;
    return 1;  // manual mode
}

/*
 * Gain reduction is an index that depends on the AM mode (only applies to AM inputs)
 *          AM1     AM2
 * 0x00    0 dB    0 dB
 * 0x01    6 dB   24 dB
 * 0x10   12 dB   24 dB
 * 0x11   18 dB   24 dB
 */
int mirisdr_set_mixer_gain(mirisdr_dev_t *p, int gain)
{
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_set_mixer_gain: %d (-> %d dB)\n", gain, gain ? 19 : 0);
    fprintf(stderr, "_set_mixer_gain -> ");
#endif
    if (!p)
    {
        fprintf(stderr, "mirisdr_set_mixer_gain: error: nil device pointer!\n");
        return -1;
    }
    p->gain_reduction_mixer = gain ? 0 : 1;

    return mirisdr_set_gain(p);
}

int mirisdr_set_mixbuffer_gain(mirisdr_dev_t *p, int gain)
{
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_set_mixbuffer_gain: %d dB (band: %d)\n", gain, p->band);
#endif
    if (!p)
    {
        fprintf(stderr, "mirisdr_set_mixbuffer_gain: error: nil device pointer!\n");
        return -1;
    }
    if (gain < 0) {
        fprintf(stderr, "ERROR: mirisdr_set_mixbuffer_gain: negative number provided: %d\n", gain);
        return -1;
    }

    if (gain > 18) {
        gain = 18; // clamp
    }

    p->gain_reduction_mixbuffer = (3 - gain / 6) & 0x03;

    return mirisdr_set_gain(p);
}

int mirisdr_set_lna_gain(mirisdr_dev_t *p, int gain)
{
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_set_lna_gain: %d (band: %d)\n", gain, p->band);
#endif
    if (!p)
    {
        fprintf(stderr, "mirisdr_set_lna_gain: error: nil device pointer!\n");
        return -1;
    }
    p->gain_reduction_lna = gain ? 0 : 1;

    return mirisdr_set_gain(p);
}

int mirisdr_set_baseband_gain(mirisdr_dev_t *p, int gain)
{
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_set_baseband_gain: %d dB\n", gain);
#endif
    if (!p)
    {
        fprintf(stderr, "mirisdr_set_baseband_gain: error: nil device pointer!\n");
        return -1;
    }
    if (gain < 0) gain = 0;
    if (gain > 59) gain = 59;
    p->gain_reduction_baseband = 59 - gain;

    return mirisdr_set_gain(p);
}

int mirisdr_get_mixer_gain(mirisdr_dev_t *p)
{
    int gain = p->gain_reduction_mixer ? 0 : 19;
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_mixer_gain: %d dB\n", gain);
#endif
    return gain;
}

int mirisdr_get_mixbuffer_gain(mirisdr_dev_t *p)
{
    int gain = 18 - 6*p->gain_reduction_mixbuffer;
    if (p->band == MIRISDR_BAND_AM2)
    {
        gain = p->gain_reduction_mixbuffer ? 0 : 24;
    }
    // report mixbuffer gain even if it's not really used on other bands to not confuse clients
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_mixbuffer_gain: %d dB (band: %d)\n", gain, p->band);
#endif
    return gain;
}

int mirisdr_get_lna_gain(mirisdr_dev_t *p)
{
    int gain = 24;
    if (p->gain_reduction_lna) gain = 0;
    else if (p->band == MIRISDR_BAND_45) gain = 7;
    else if (p->band == MIRISDR_BAND_L) gain = 5; /* rounded 4.5 dB */
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_lna_gain: %d dB (band: %d)\n", gain, p->band);
#endif
    return gain;
}

int mirisdr_get_baseband_gain(mirisdr_dev_t *p)
{
    int gain = 59 - p->gain_reduction_baseband;
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_baseband_gain: %d dB\n", gain);
#endif
    return gain;
}

int mirisdr_set_dc_raw (mirisdr_dev_t *p, uint32_t raw)
{
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_set_dc_raw: %u\n", raw);
#endif
    if (!p)
    {
        fprintf(stderr, "mirisdr_set_dc_raw: error: nil device pointer!\n");
        return -1;
    }
    p->dc_mode = raw & 0x7;
    raw >>= 3;
    p->dc_speedup = raw & 0x1;
    raw >>= 1;
    p->dc_track = raw & 0x3f;
    raw >>= 12;
    p->dc_period = raw & 0xfff;

    return mirisdr_set_gain(p);
}

uint32_t mirisdr_get_dc_raw (mirisdr_dev_t *p)
{
    uint32_t rv;
    if (!p)
    {
        fprintf(stderr, "mirisdr_get_dc_raw: error: nil device pointer!\n");
        rv = 0;
    }
    else
    {
        rv  = p->dc_mode;
        rv |= p->dc_speedup << 3;
        rv |= p->dc_track << 4;
        rv |= p->dc_period<< 16;
    }
#if MIRISDR_DEBUG >= 3
    fprintf(stderr, "mirisdr_get_dc_raw: %u\n", rv);
#endif
    return rv;
}

