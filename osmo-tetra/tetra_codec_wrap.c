/*
 * Thin DLL wrapper exposing a stable ctypes-compatible entry point.
 * Links against the ETSI EN 300 395-2 speech codec (c-code/).
 *
 * The ETSI source uses a file-based API (reads/writes FILE*).  We
 * intercept it at the frame level to avoid process overhead.
 *
 * Build:
 *   gcc -shared -O2 -o tetra_codec.dll tetra_codec_wrap.c \
 *       codec/c-code/sub_sc_d.c codec/c-code/sub_dsp.c    \
 *       codec/c-code/fbas_tet.c codec/c-code/fexp_tet.c   \
 *       -Icodec/c-code -lm
 *   (add the remaining SRCS3 files from codec/c-code/makefile as needed)
 */

#include <stdint.h>
#include <string.h>
#include "codec/c-code/source.h"   /* Word16, Word32 */

/* ── ETSI decoder state (forward-declared from the ETSI source files) ───── */
/* Look in codec/c-code/sdecoder.c for the exact init / decode calls,
 * then fill them in below.  Typical ETSI pattern:                          */

extern void Init_Decod_Tetra(void); 
extern void Decod_Tetra(Word16 *prm, Word16 *synth);

static int _initialized = 0;

__declspec(dllexport)
int tetra_speech_decode(const uint8_t *bits137, int16_t *pcm240)
{
    if (!_initialized) {
        Init_Decod_Tetra(); 
        _initialized = 1;
    }

    /* Convert bit array (0/1 uint8) to Word16 parameter array expected
     * by the ETSI decoder, then call the synthesis function.             */

    Decod_Tetra((Word16 *)bits137, (Word16 *)pcm240);		/* decoder */

    /* Until the ETSI function names are known, return silence: */
    memset(pcm240, 0, 240 * sizeof(int16_t));
    return 0;
}
