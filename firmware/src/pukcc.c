/*
 * pukcc.c - brings the PUKCC up and hands wolfSSL's RSA to it.
 * See pukcc.h for why this exists at all.
 *
 * What this module deliberately does NOT do, after a discovery worth
 * recording: implement the RSA-CRT sequence itself. Microchip's own PUKCL
 * driver (third_party/wolfssl/.../wolfcrypt/src/port/pic32/crypt_rsa_pukcl.c
 * plus crypt_wolfcryptcb.c) is already vendored in this project and was
 * already being compiled into every image - it was simply switched off, gated
 * behind WOLFSSL_HAVE_MCHP_HW_RSA and WOLFSSL_HAVE_MCHP_HW_CRYPTO_RSA_HW_PUKCC
 * (now defined in the project's compiler macros, nbproject/configurations.xml,
 * rather than in MCC-generated configuration.h - that way an MCC regeneration
 * cannot quietly switch the accelerator back off).
 *
 * The first version of this file drove the CRT service by hand. It ran - 51.8
 * ms against software's 745 ms, measured on the bench - but returned a WRONG
 * result, because the CRT service does not compute the R value itself: the
 * driver first runs a GCD service for R and two Div services to derive EP/EQ
 * from d. Re-deriving all of that here would have been duplicated, unreviewed
 * crypto code sitting next to a vendor implementation of the same thing.
 *
 * So what is left here is exactly what the vendored driver does not do,
 * because it is board-level rather than algorithm-level:
 *
 *   - enable the PUKCC clock and wait out the automatic Crypto RAM clear. MCC
 *     never generated this for us: the peripheral was unused, so nothing in
 *     initialization.c touches MCLK_AHBMASK's PUKCC bit. Without it the
 *     library reads a dead peripheral.
 *   - run the mandatory self-test with a BOUNDED wait. The vendored
 *     crypt_pukcl_SelfTest() spins on `while (u2Status != PUKCL_OK);` with no
 *     escape, which is not something to reach on a board whose only remote
 *     access is the TLS console this very accelerator serves.
 *   - register the driver's crypto callback with wolfSSL.
 *   - give the console a way to prove on real hardware that the result is
 *     correct and how much faster it actually is.
 */

#include "definitions.h"
#include "pukcc.h"
#include "cmd_print.h"

#include <stdlib.h>
#include <string.h>

/* The vendored PUKCL headers. They include their siblings by bare name, which
 * resolves because they all sit in one directory - do not "tidy" these into
 * some other include form. */
#include "wolfssl/wolfcrypt/port/pic32/CryptoLib_Headers_pb.h"
/* Not pulled in by Headers_pb.h: the Crypto RAM window (MSB_EXTENT_CRYPTORAM)
 * and the PUKCC status register (PUKCCSR, BIT_PUKCCSR_CLRRAM_BUSY). */
#include "wolfssl/wolfcrypt/port/pic32/CryptoLib_mapping_pb.h"
#include "wolfssl/wolfcrypt/port/pic32/CryptoLib_Hardware_pb.h"

#include "wolfssl/wolfcrypt/rsa.h"
#include "wolfssl/wolfcrypt/random.h"
#include "wolfssl/wolfcrypt/port/pic32/crypt_wolfcryptcb.h"
#include "wolfssl/wolfcrypt/port/pic32/crypt_rsa_pukcl.h"
#include "wolfssl/wolfcrypt/cryptocb.h"
#include "wolfssl/wolfcrypt/error-crypt.h"   /* CRYPTOCB_UNAVAILABLE */

#include "pukcc_vector.h"

/* Documented expected SelfTest results (data sheet sec.43.3.3.1). PUKCL_OK
 * with different check values would mean the ROM library is not the one these
 * headers describe - worth catching, because everything after it would
 * silently compute nonsense. */
#define PUKCC_SELFTEST_CHECK1   0x6E70DDD2u
#define PUKCC_SELFTEST_CHECK2   0x25C8D64Fu

/* Crypto RAM window (CryptoLib_mapping_pb.h): absolute = MSB_EXTENT | offset. */
#define CR_PROBE_OFF    ((uint32_t)0x1000)
#define CR_PROBE_ADDR   ((volatile uint32_t *)(MSB_EXTENT_CRYPTORAM | CR_PROBE_OFF))

/* The devId crypt_wolfcryptcb.c registers itself under. Not a choice this
 * module gets to make - CRYPT_WCCB_Initialize() hardcodes 0 - but the TLS
 * contexts have to be told the same number, so it is named here and used from
 * net_pres_enc_glue*.c. */
#define PUKCC_WOLFSSL_DEVID  0

static PUKCC_STATUS_T s_status;
static PUKCL_PARAM    s_param;

/* Callback bookkeeping. The vendored CRYPT_WCCB_Initialize() registers a
 * callback that silently returns CRYPTOCB_UNAVAILABLE on anything it cannot
 * do, and wolfSSL then silently falls back to software - so a board doing
 * every RSA in software looks exactly like a board doing none. These counters
 * are what tell the two apart, and they are why this module registers its own
 * thin wrapper rather than calling CRYPT_WCCB_Initialize(). */
static uint32_t s_cb_calls;      /* callback entered                        */
static uint32_t s_cb_rsa;        /* ... for an RSA private/public operation */
static uint32_t s_cb_handled;    /* ... and the driver actually did it      */
static uint32_t s_cb_declined;   /* ... public RSA, deliberately left to software */
static int      s_cb_last_ret;

static int pukcc_crypto_cb(int devId, wc_CryptoInfo *info, void *ctx)
{
    int ret = CRYPTOCB_UNAVAILABLE;

    s_cb_calls++;
    if ((info != NULL) && (info->algo_type == WC_ALGO_TYPE_PK) &&
        (info->pk.type == WC_PK_TYPE_RSA)) {
        /* PRIVATE operations only, and this restriction is not caution - it
         * is a measured bug fence. Handing the vendored driver a PUBLIC
         * operation as well made the MQTT client fail its handshake with
         * RSA_PAD_E (-201), i.e. the hardware returned a result whose padding
         * did not verify, and the board's TLS server wedged in the fallout.
         * Private ops (the board's own key) went through 91 times without a
         * fault in the same firmware. Public ops are also where there is
         * nothing to win: e = 65537 costs single-digit milliseconds in
         * software against the ~230 ms of a private operation.
         *
         * The likely defect is in the vendor file, not here:
         * Crypt_RSA_PublicEncrypt()/PublicDecrypt() copy out
         * *(info->pk.rsa.outLen) bytes for a value the caller has not sized
         * yet, and every path there reads info->rng.rng - a DIFFERENT arm of
         * the wc_CryptoInfo union than the one in use, which aliases
         * info->pk.type rather than a real RNG. Fixing that belongs upstream;
         * declining the operation is what this project needs. */
        if ((info->pk.rsa.type == RSA_PRIVATE_ENCRYPT) ||
            (info->pk.rsa.type == RSA_PRIVATE_DECRYPT)) {
            s_cb_rsa++;
            ret = Crypt_RSA_HandleReq(devId, info, ctx);
            s_cb_last_ret = ret;
            if (ret == 0) {
                s_cb_handled++;
            }
        } else {
            s_cb_declined++;
        }
    }
    return ret;
}

/* Does the Crypto RAM window actually answer? The one assumption this module
 * cannot check any other way - that MSB_EXTENT_CRYPTORAM is where this part
 * really maps it. Restores whatever it found. */
static bool pukcc_probe_ram(void)
{
    volatile uint32_t *p = CR_PROBE_ADDR;
    uint32_t saved = *p;
    bool ok;

    *p = 0xA5C3F00Du;
    ok = (*p == 0xA5C3F00Du);
    if (ok) {
        *p = 0x5A3C0FF2u;
        ok = (*p == 0x5A3C0FF2u);
    }
    *p = saved;
    return ok;
}

static void pukcc_commands_init(void);

void PUKCC_Initialize(void)
{
    (void)memset(&s_status, 0, sizeof s_status);

    /* Registered first: when the accelerator turns out NOT to be usable,
     * 'pukcc_status' is the only thing that can say why. */
    pukcc_commands_init();

    /* Clock first - see the file header for why nothing else does this. */
    MCLK_REGS->MCLK_AHBMASK |= MCLK_AHBMASK_PUKCC_Msk;

    /* Mandatory wait for the automatic Crypto RAM clear (sec.43.3.3.1),
     * bounded: this runs inside APP_Initialize(), and a part that never
     * finishes clearing must not cost the firmware its boot. */
    {
        uint32_t guard = 1000000u;
        while (((PUKCCSR & BIT_PUKCCSR_CLRRAM_BUSY) != 0u) && (guard-- != 0u)) {
            /* spin */
        }
        if (guard == 0u) {
            SYS_CONSOLE_PRINT("PUKCC: Crypto RAM clear never finished - staying on software RSA\r\n");
            return;
        }
    }

    s_status.ram_ok = pukcc_probe_ram();
    if (!s_status.ram_ok) {
        SYS_CONSOLE_PRINT("PUKCC: Crypto RAM did not answer at 0x%08lX - staying on software RSA\r\n",
                          (unsigned long)(MSB_EXTENT_CRYPTORAM | CR_PROBE_OFF));
        return;
    }

    {
        PUKCL_PARAM *pvPUKCLParam = &s_param;
        (void)memset(&s_param, 0, sizeof s_param);
        vPUKCL_Process(SelfTest, pvPUKCLParam);
        s_status.last_status = PUKCL(u2Status);
        if (s_status.last_status != PUKCL_OK) {
            SYS_CONSOLE_PRINT("PUKCC: SelfTest failed, status 0x%04X - staying on software RSA\r\n",
                              (unsigned)s_status.last_status);
            return;
        }
        s_status.lib_version = s_param.P.PUKCL_SelfTest.u4Version;
        s_status.hw_version  = s_param.P.PUKCL_SelfTest.u4PUKCCVersion;
        s_status.check1      = s_param.P.PUKCL_SelfTest.u4CheckNum1;
        s_status.check2      = s_param.P.PUKCL_SelfTest.u4CheckNum2;
    }

    if ((s_status.check1 != PUKCC_SELFTEST_CHECK1) || (s_status.check2 != PUKCC_SELFTEST_CHECK2)) {
        SYS_CONSOLE_PRINT("PUKCC: SelfTest checks %08lX/%08lX, expected %08lX/%08lX"
                          " - staying on software RSA\r\n",
                          (unsigned long)s_status.check1, (unsigned long)s_status.check2,
                          (unsigned long)PUKCC_SELFTEST_CHECK1, (unsigned long)PUKCC_SELFTEST_CHECK2);
        return;
    }

    /* Hands every wc_CryptoCb-aware RSA operation to crypt_rsa_pukcl.c, for
     * any key whose RsaKey was initialised with this devId. Everything else
     * (and every error inside the driver) falls back to software by returning
     * CRYPTOCB_UNAVAILABLE, so this cannot take TLS down. */
    /* Registration itself happens in PUKCC_DevId(), not here - see there. */

    s_status.ready = true;
    SYS_CONSOLE_PRINT("PUKCC: ready (PUKCL v%lu, hw v%lu) - RSA offloaded\r\n",
                      (unsigned long)s_status.lib_version, (unsigned long)s_status.hw_version);
}

const PUKCC_STATUS_T *PUKCC_Status(void)
{
    return &s_status;
}

int PUKCC_DevId(void)
{
    if (!s_status.ready) {
        return INVALID_DEVID;
    }

    /* Registering here rather than once at init, and idempotently, is not
     * belt-and-braces: wolfSSL_Init() -> wolfCrypt_Init() -> wc_CryptoCb_Init()
     * WIPES the callback table (sets every entry back to INVALID_DEVID). A
     * registration done in APP_Initialize() is therefore silently gone by the
     * time the first TLS context exists - which is exactly what happened, and
     * it looks identical to "the accelerator is not helping": every RSA still
     * ran in software, at the same 455 ms, with the callback never entered
     * (0 calls in 'pukcc_status'). Callers ask for the devId right after
     * wolfSSL_Init(), so doing it here gets the ordering right by
     * construction. wc_CryptoCb_RegisterDevice() finds and updates an existing
     * entry, so repeating it is free. */
    (void)wc_CryptoCb_RegisterDevice(PUKCC_WOLFSSL_DEVID, pukcc_crypto_cb, NULL);
    return PUKCC_WOLFSSL_DEVID;
}

// *****************************************************************************
// Section: console commands
// *****************************************************************************

static void cmd_pukcc_status(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    CMD_PRINT(pCmdIO, "PUKCC: %s, Crypto RAM %s at 0x%08lX\r\n",
              s_status.ready ? "ready, wolfSSL RSA offloaded" : "NOT ready, RSA in software",
              s_status.ram_ok ? "ok" : "FAILED",
              (unsigned long)(MSB_EXTENT_CRYPTORAM | CR_PROBE_OFF));
    CMD_PRINT(pCmdIO, "  PUKCL version %lu, PUKCC hw version %lu, wolfSSL devId %d\r\n",
              (unsigned long)s_status.lib_version, (unsigned long)s_status.hw_version,
              PUKCC_DevId());
    CMD_PRINT(pCmdIO, "  SelfTest checks %08lX / %08lX (expected %08lX / %08lX)\r\n",
              (unsigned long)s_status.check1, (unsigned long)s_status.check2,
              (unsigned long)PUKCC_SELFTEST_CHECK1, (unsigned long)PUKCC_SELFTEST_CHECK2);
    CMD_PRINT(pCmdIO, "  last PUKCL status 0x%04X\r\n", (unsigned)s_status.last_status);
    CMD_PRINT(pCmdIO, "  crypto callback: %lu calls, %lu RSA, %lu done in hardware, last ret %d\r\n",
              (unsigned long)s_cb_calls, (unsigned long)s_cb_rsa,
              (unsigned long)s_cb_handled, s_cb_last_ret);
}

/* One RsaKey is 4.5 KB in this build (USE_FAST_MATH keeps eight fp_ints
 * inline), which is more than belongs on the console's stack. */
static RsaKey s_bench_key;
static WC_RNG s_bench_rng;
static uint8_t s_bench_in[256];
static uint8_t s_bench_out[256];

/* Times N raw RSA-2048 private-key operations on the fixed vector and checks
 * every result. Verifying matters at least as much as timing: the hand-written
 * CRT this module started out as was 14x faster AND wrong, and a
 * timing-only benchmark would have reported that as a success.
 *
 * 'pukcc_bench [n] [sw]' - with 'sw' the key is initialised with
 * INVALID_DEVID, so the very same call runs in wolfSSL's software path. That
 * is the honest A/B: same key, same input, same board, one line apart. */
static void cmd_pukcc_bench(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    uint32_t n = 5u, i, t0, dt, min = 0xFFFFFFFFu, max = 0u, bad = 0u, failed = 0u;
    uint64_t sum = 0u;
    int devId = PUKCC_DevId();
    word32 idx = 0, outLen;
    int ret;

    if (argc > 1) {
        int v = atoi(argv[1]);
        if ((v > 0) && (v <= 50)) {
            n = (uint32_t)v;
        }
    }
    if ((argc > 2) && (strcmp(argv[2], "sw") == 0)) {
        devId = INVALID_DEVID;
    }
    if ((devId != INVALID_DEVID) && !s_status.ready) {
        CMD_PRINT(pCmdIO, "PUKCC: not ready - 'pukcc_status' says why\r\n");
        return;
    }

    ret = wc_InitRsaKey_ex(&s_bench_key, NULL, devId);
    if (ret == 0) {
        ret = wc_RsaPrivateKeyDecode(PUKCC_VEC_KEY_DER, &idx, &s_bench_key,
                                     (word32)sizeof PUKCC_VEC_KEY_DER);
    }
    if (ret == 0) {
        ret = wc_InitRng(&s_bench_rng);
    }
    if (ret != 0) {
        CMD_PRINT(pCmdIO, "PUKCC: bench setup failed (%d)\r\n", ret);
        (void)wc_FreeRsaKey(&s_bench_key);
        return;
    }

    /* Same free-running DWT counter cpuload.c uses; arming it is idempotent. */
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;

    for (i = 0u; i < n; i++) {
        (void)memcpy(s_bench_in, PUKCC_VEC_X, sizeof s_bench_in);
        outLen = (word32)sizeof s_bench_out;
        t0 = DWT->CYCCNT;
        ret = wc_RsaDirect(s_bench_in, (word32)sizeof s_bench_in,
                           s_bench_out, &outLen, &s_bench_key,
                           RSA_PRIVATE_ENCRYPT, &s_bench_rng);
        dt = DWT->CYCCNT - t0;
        if (ret < 0) {
            failed++;
            continue;
        }
        if ((outLen != sizeof PUKCC_VEC_Y) ||
            (memcmp(s_bench_out, PUKCC_VEC_Y, sizeof PUKCC_VEC_Y) != 0)) {
            bad++;
        }
        if (dt < min) { min = dt; }
        if (dt > max) { max = dt; }
        sum += dt;
    }

    wc_FreeRng(&s_bench_rng);
    (void)wc_FreeRsaKey(&s_bench_key);

    if (failed == n) {
        CMD_PRINT(pCmdIO, "PUKCC: all %lu operations failed (last ret %d)\r\n",
                  (unsigned long)n, ret);
        return;
    }
    {
        uint32_t ok = n - failed;
        uint32_t mean = (uint32_t)(sum / ok);
        CMD_PRINT(pCmdIO, "RSA-2048 private op x%lu on %s: min %lu max %lu mean %lu cycles\r\n",
                  (unsigned long)ok,
                  (devId == INVALID_DEVID) ? "SOFTWARE" : "PUKCC",
                  (unsigned long)min, (unsigned long)max, (unsigned long)mean);
        CMD_PRINT(pCmdIO, "  = %lu.%02lu ms mean at 120 MHz, result %s (%lu wrong, %lu failed)\r\n",
                  (unsigned long)(mean / 120000u), (unsigned long)((mean / 1200u) % 100u),
                  ((bad == 0u) && (failed == 0u)) ? "CORRECT" : "WRONG",
                  (unsigned long)bad, (unsigned long)failed);
    }
}

static const SYS_CMD_DESCRIPTOR pukcc_cmd_tbl[] = {
    {"pukcc_status", (SYS_CMD_FNC)cmd_pukcc_status, ": PUKCC state, self-test result and wolfSSL devId"},
    {"pukcc_bench",  (SYS_CMD_FNC)cmd_pukcc_bench,  ": time+verify an RSA-2048 private op (pukcc_bench [n] [sw])"},
};

static void pukcc_commands_init(void)
{
    if (!SYS_CMD_ADDGRP(pukcc_cmd_tbl, (int)(sizeof pukcc_cmd_tbl / sizeof *pukcc_cmd_tbl),
                        "pukcc", ": public-key accelerator")) {
        SYS_CONSOLE_PRINT("PUKCC: SYS_CMD_ADDGRP failed\r\n");
    }
}
