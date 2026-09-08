/*
 * pukcc.h - the SAME54's Public Key Cryptography Controller, used for the one
 * operation that dominates this firmware's CPU cost: the RSA-2048 private-key
 * operation inside every TLS handshake.
 *
 * Why this exists: measured on the bench (docs/resource-cost-tls-mqtt.md
 * sec.4.2), one mutual-TLS handshake costs ~139 M cycles = 1.16 s of CPU, and
 * 89.4 M of those - 745 ms - are a SINGLE uninterruptible call doing the
 * modular exponentiation in software (wolfSSL's sp_cortexm assembly). For that
 * three quarters of a second the bare-metal round-robin loop does not turn:
 * no bridge forwarding pass, no TCP/IP task, no console. The part has had a
 * public-key accelerator all along, with its library (PUKCL) in on-chip ROM,
 * and this firmware never touched it.
 *
 * What this module is NOT: an implementation of RSA. Microchip's own PUKCL
 * driver is already vendored in this project (and was already being compiled,
 * just switched off); this brings the peripheral up, registers that driver
 * with wolfSSL, and provides the console commands to prove on real hardware
 * that the result is correct and how much faster it is. See pukcc.c.
 *
 * Data sheet: SAME54 sec.43 (PUKCC/PUKCL). The parameter blocks come from
 * wolfSSL's vendored port headers, third_party/wolfssl/wolfssl/wolfcrypt/
 * port/pic32/CryptoLib_*_pb.h - headers only, nothing in wolfSSL calls them;
 * this module is the missing glue.
 */
#ifndef PUKCC_H
#define PUKCC_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Result of PUKCC_Initialize(), also what 'pukcc status' reports. Kept as a
 * struct rather than a bool because when this does not work, WHY it did not
 * work is the whole question - a failed self-test and a failed Crypto RAM
 * probe need completely different fixes. */
typedef struct {
    bool     ready;             /* self-test passed, services are usable      */
    bool     ram_ok;            /* Crypto RAM answered a write/read probe     */
    uint32_t lib_version;       /* PUKCL version reported by SelfTest         */
    uint32_t hw_version;        /* PUKCC version reported by SelfTest         */
    uint32_t check1, check2;    /* SelfTest check values (see pukcc.c)        */
    uint16_t last_status;       /* last PUKCL u2Status seen                   */
} PUKCC_STATUS_T;

/* Enables the PUKCC clock, waits out the Crypto RAM clear, probes that RAM,
 * and runs the mandatory SelfTest. Safe to call once from APP_Initialize();
 * never traps or hangs on failure - a board with a broken accelerator must
 * still boot and serve TLS in software. */
void PUKCC_Initialize(void);

const PUKCC_STATUS_T *PUKCC_Status(void);

static inline bool PUKCC_IsReady(void)
{
    return PUKCC_Status()->ready;
}

/* The wolfSSL devId the vendored PUKCL driver registered itself under, or
 * INVALID_DEVID when the accelerator is not usable. A TLS context handed this
 * gets its RSA private-key operations done in hardware; handed INVALID_DEVID
 * it behaves exactly as before. That is the whole switch, and it is why the
 * failure mode here is "as slow as it always was" rather than "no TLS". */
int PUKCC_DevId(void);

#ifdef __cplusplus
}
#endif

#endif /* PUKCC_H */
