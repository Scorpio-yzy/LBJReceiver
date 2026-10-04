LibMiriSDR-5
============

This is (yet) another flavour of libmirisdr initiated with original libmirisdr-2 from Miroslav Slugeň and additions of Leif Åsbrink SM5BSZ in libmirisdr-3-bsz and further additions from Edouard Griffiths (f4exb) in libmirisdr-4.

Note these improvements and bug fixes:

<h2>Improvements</h2>

  - Better support of SDRPlay through a "flavour" option in the open function. This indicator can be used throughout the code if necessary. At present it drives the frequency plan that drives the choice between the different receiving paths of the MSi001 depending on frequency.
  - Remove useless auto gain feature that is just fixed gain in fact. The setter/getter still exists for compatibility but effectively does nothing.
  - Set the RPATH on the executables so you don't have to set LD_LIBRARY_PATH with the binaries installed by cmake.
  - Use Unix framework when compiling under Windows witn MinGW. This may fix possible bugs.
  - Use more meaningful variable names for what is actually gain reductions and not gains.
  - Some comments in the code were translated from Czech to English (Google translated) to ease understanding by the masses.
  
<h2>Bug fixes</h2>

  - Stop using a deprecated version of libusb.h (1.0.13) and rely on the one installed in the system or specified in the cmake command line.
  - Restore gain settings after a frequency, bandwidth or IF change as this affects the gain settings.
  - Corrected baseband gain setting.
  - Corrected LNA gains for 45 and L bands.

<h2>Interface</h2>

The interface description is reverse engineered from the code base and datasheed info and is far from being complete. In case of issues (unexpected behaviour of the SDR receiver) or just curiosity, it is recommended to `#define MIRISDR_DEBUG 1` in `include/mirisdr.h` and recompile the library to enable debug output on `stderr`.


<h3>Gain</h3>

The MSi001 uses attenuators. That gain reduction is translated into the more common *gain* approach. The maximal possible attenuation is mapped to gain = 0 dB.  
Depending on the selected band, a maximum gain from 83 dB (L) to 102 dB (AM2, VHF, III) is possible.

* **`int mirisdr_set_tuner_gain (mirisdr_dev_t *p, int gain)`**

sets the tuner total gain to the requested gain value. Requests with negative gain values are silently ignored. If a positive value exceeds the tuner capability, the max. possible gain is set. The function always returns 0 to indicate succuess.

* **`int mirisdr_get_tuner_gain (mirisdr_dev_t *p)`**

reports the actual total gain in dB. Expect some inaccuracy due to chip tolerances.

* **`int mirisdr_set_mixer_gain (mirisdr_dev_t *p, int gain)`**
* **`int mirisdr_set_mixbuffer_gain (mirisdr_dev_t *p, int gain)`**
* **`int mirisdr_set_lna_gain (mirisdr_dev_t *p, int gain)i`**
* **`int mirisdr_set_baseband_gain (mirisdr_dev_t *p, int gain)`**

individually set the gain of the different stages. Note, that these are overwritten by a call to `mirisdr_set_tuner_gain()`. Note, that *IQ mixer* (`mixer_gain`) with 19 dB and *LNA* (`lna_gain`) with band-depended gain from 5 to 24 dB can only be switched on or off. The *upmixer* (`mixbuffer_gain`) takes discrete values of 0, 6, 12, 18 dB (AM1-band) or 0, 24 dB (AM2-band). The *baseband* gain ranges from 0 dB to 59 dB.

* **`int mirisdr_get_mixer_gain (mirisdr_dev_t *p)`**
* **`int mirisdr_get_mixbuffer_gain (mirisdr_dev_t *p)`**
* **`int mirisdr_get_lna_gain (mirisdr_dev_t *p)`**
* **`int mirisdr_get_baseband_gain(mirisdr_dev_t *p)`**

return the gain in dB of the queried stage. If this stage is not in the actual signal path, the returned value is inaccurate since a non-current band must be assumed for the calculation. Would it be better return then zero?

* **`	int mirisdr_set_tuner_gain_mode (mirisdr_dev_t *p, int mode)`**

sets the tuner gain mode to either *automatic* (mode==0) or *manual* (mode!=0) and returns 0 if successful (-1 on error). Since no automatic mode is supported, this function will return an error on automatic mode request.

* **`	int mirisdr_get_tuner_gain_mode (mirisdr_dev_t *p)`**

returns the tuner gain mode. This function always returns *manual* (mode=1).


<h3>DC Offset</h3>

* **`int mirisdr_set_dc_raw (mirisdr_dev_t *p, uint32_t raw)`**
* **`uint32_t mirisdr_get_dc_raw (mirisdr_dev_t *p)`**

sets respectively gets the value in the dc mode control register. No sanity checks are performed. The 32-bit unsigned int value is encoded as (bit length in parentheses):

`[ unused (4) | period (12) | unused (6) | track (6) | speedup (1) | mode (3) ]`

The default dc register setting is
0x080001f2 (period = 0x800, track = 0x1f, speedup = 0, mode = 2).

