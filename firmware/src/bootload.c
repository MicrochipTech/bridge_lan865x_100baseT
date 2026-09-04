/*******************************************************************************
  Firmware self-update into the inactive flash bank - implementation

  See bootload.h for the why.

  Shape follows testserver.c and port_mirror.c: one polled state machine driven
  from APP_STATE_IDLE, no interrupts, no RTOS dependency, all replies through
  CMD_PRINT() so they reach whoever asked (serial or Telnet).

  Addresses, timings and register behaviour used here come from the SAME54
  data sheet (DS60001507K) sections 25.6.2/25.6.3 (memory organization and bank
  swapping), 25.6.6 (command and data interface), 25.6.6.3 (read-while-write)
  and Table 54-40 (tFPW 1.5 ms typ / 3 ms max per page, tFEB 50 ms typ /
  200 ms max per block). docs/dual-bank-bootloader-plan.md has the derivation.
*******************************************************************************/

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>                                           /* snprintf() */
#include <stdlib.h>                                          /* strtoul() */
#include <string.h>                                          /* strcmp(), memset() */

#include "configuration.h"                                   /* SYS_TIME_CPU_CLOCK_FREQUENCY */
#include "definitions.h"                                     /* NVMCTRL/CMCC PLIBs, CoreDebug, DWT, *_REGS */
#include "config/default/library/emulated_eeprom/emulated_eeprom.h"   /* EMU_EEPROM_PageBufferCommit() */
#include "config/default/system/console/sys_console.h"
#include "config/default/library/tcpip/tcpip.h"
#include "config/default/library/tcpip/tcp.h"
#include "system/command/sys_command.h"
#include "system/time/sys_time.h"
#include "bootload.h"
#include "cmd_print.h"                                        /* CMD_PRINT - reply to pCmdIO, not always the serial console */

/* ---------------------------------------------------------------------------
 * Flash geometry. Not configurable, and not guessed either:
 *
 *  - The 1 MiB array is two 512 KiB banks. Both are mapped in the main address
 *    space at the same time; the ACTIVE one (the one the CPU boots and
 *    executes from) is at 0x00000000, so the inactive one - the update target -
 *    is at 0x00080000, whatever STATUS.AFIRST currently says. AFIRST is read
 *    only to REPORT which physical bank that happens to be (25.6.3).
 *  - The image is capped at 0x7C000, not 0x80000: the top 16 KiB of every bank
 *    is the emulated EEPROM window (EEPROM_EMULATOR_EEPROM_START_ADDRESS =
 *    0xFC000 = top of the inactive bank), which holds the LIVE environment
 *    while an update runs. This cap is what keeps the running board's IP
 *    configuration structurally out of reach of the bootloader; the project's
 *    ROM_LENGTH is being set to the same 0x7C000 so the linker cannot place
 *    code there either (WP3).
 * ------------------------------------------------------------------------- */
#define BL_BANK_SIZE      0x00080000u                        /* 512 KiB per bank */
#define BL_TARGET_BASE    0x00080000u                        /* inactive bank, always */
#define BL_MAX_IMAGE      0x0007C000u                        /* 496 KiB - bank minus the EEPROM window */
#define BL_ENV_WINDOW     0x000FC000u                        /* live emulated EEPROM - never written by the image */
#define BL_ENV_SHADOW     0x0007C000u                        /* where it has to be for the NEXT boot - see bl_commit() */
#define BL_ENV_SIZE       0x00004000u                        /* 16 KiB = 2 blocks = 32 pages */
#define BL_PAGE           NVMCTRL_FLASH_PAGESIZE             /* 512  - write granularity */
#define BL_BLOCK          NVMCTRL_FLASH_BLOCKSIZE            /* 8192 - erase granularity */

#define BL_PORT_DEFAULT   5567u
#define BL_MAGIC          0x424C4452u                        /* 'BLDR' */
#define BL_HDR_WORDS      4u                                 /* magic, size, crc32, header crc32 */
#define BL_BUDGET         8192u                              /* max bytes drained per Tasks() call */
#define BL_VERIFY_CHUNK   4096u                              /* bytes CRC'd per Tasks() call */
#define BL_CONN_TIMEOUT_MS  120000u                          /* armed, but nobody connects */
#define BL_STALL_TIMEOUT_MS  30000u                          /* connected, but no progress */
#define BL_COMMIT_DELAY_MS     500u                          /* let the commit reply reach the client first */
#define BL_SWAP_DELAY_MS       300u                          /* let the copy result reach the serial console */

#define BL_NVM_ERR_MASK   (NVMCTRL_INTFLAG_ADDRE_Msk | NVMCTRL_INTFLAG_PROGE_Msk | \
                           NVMCTRL_INTFLAG_LOCKE_Msk | NVMCTRL_INTFLAG_NVME_Msk)

/* ---------------------------------------------------------------------------
 * Probation: an image that boots but cannot be reached rolls itself back
 *
 * A CRC-correct image can still be unusable - wrong configuration, a PHY that
 * does not come up, anything that leaves the board off the network. Nobody can
 * update it again, because updating needs the network. So the swap is put on
 * probation: the image that performs it leaves a note behind, the image that
 * boots from it starts a timer, and only a confirmation from whoever can
 * actually reach the board over the network clears it. No confirmation, no
 * keeping the new image - the board swaps back on its own.
 *
 * Deliberately NOT a __attribute__((persistent)) variable, unlike crashlog.c's
 * record: that one is written and read by the same firmware, so wherever the
 * linker puts it both ends agree. This note is written by the OLD image and read
 * by the NEW one - two builds, two potentially different .pbss layouts - so its
 * address is pinned here instead, at the top of the 8 KB Backup RAM, far from
 * what .pbss allocates from 0x47000000 upwards (crashlog: 520 bytes). Backup RAM
 * is not touched by the C runtime and survives a warm reset; verified on
 * hardware with nothing but the existing CLI: poke 0x47001FE0, reset, peek.
 *
 * RCAUSE guards against acting on a stale note: the note is only honoured when
 * this boot was actually caused by the NVM controller (i.e. by BKSWRST). After
 * a power cycle or an ordinary reset it is discarded - which is also the
 * behaviour to want, because whoever cut the power has intervened and nothing
 * should roll back behind their back.
 * ------------------------------------------------------------------------- */
#define BL_PROB_ADDR      0x47001FE0u
#define BL_PROB_MAGIC     0x424C5052u                        /* 'BLPR' */
#define BL_PROB_ARMED     1u
#define BL_PROB_REVERTED  2u
#define BL_PROBATION_S    180u                               /* generous: the tool confirms within ~15 s */

typedef struct {
    uint32_t magic;
    uint32_t state;
    uint32_t count;      /* how many swaps this note has seen - diagnostic only */
    uint32_t crc;        /* over the three words above; Backup RAM is undefined on first power-up */
} bl_prob_t;

#define BL_PROB           ((volatile bl_prob_t *)BL_PROB_ADDR)

typedef enum {
    BL_IDLE = 0,        /* nothing armed; the TCP port is not even open */
    BL_WAIT_CONN,       /* armed, socket listening, no client yet */
    BL_RECV_HDR,        /* client connected, collecting the 16-byte header */
    BL_RECV_IMG,        /* streaming image bytes into the inactive bank */
    BL_VERIFY,          /* CRC32 pass over what was just written */
    BL_VERIFIED,        /* image is in the inactive bank and verified, waiting for 'commit' */
    BL_COMMIT_WAIT,     /* commit accepted; letting the reply reach the client before the copy */
    BL_COMMIT_SWAP      /* environment copied; letting that line out before BKSWRST */
} bl_state_t;

typedef enum {
    BL_E_NONE = 0,
    BL_E_STATE,         /* command issued in the wrong state */
    BL_E_ARGS,          /* bad or missing arguments */
    BL_E_SIZE,          /* image too large / zero */
    BL_E_SOCKET,        /* TCPIP_TCP_ServerOpen() failed */
    BL_E_HEADER,        /* magic / size / crc mismatch in the data-socket header */
    BL_E_CONN,          /* connection dropped or timed out mid-transfer */
    BL_E_NVM,           /* NVMCTRL reported ADDRE/PROGE/LOCKE/NVME */
    BL_E_CRC,           /* image in flash does not match the announced CRC32 */
    BL_E_ENVCOPY        /* the environment hand-over copy failed - bank NOT swapped */
} bl_err_t;

static bl_state_t s_state = BL_IDLE;
static bl_err_t   s_err = BL_E_NONE;
static TCP_SOCKET s_sock = INVALID_SOCKET;
static TCP_PORT   s_port = BL_PORT_DEFAULT;

static uint32_t s_size;                                      /* announced image size, bytes */
static uint32_t s_crc_want;                                  /* announced image CRC32 */
static uint32_t s_written;                                   /* bytes already committed to flash */
static uint32_t s_received;                                  /* bytes taken off the socket */
static uint32_t s_page[BL_PAGE / 4u];                        /* one page, word aligned */
static uint32_t s_fill;                                      /* bytes currently in s_page */
static uint32_t s_hdr[BL_HDR_WORDS];
static uint32_t s_hdr_fill;
static bool     s_erase_due;                                 /* next page starts a block -> erase first */
static uint32_t s_verify_off;
static uint32_t s_verify_crc;
static uint64_t s_last_progress;                             /* SYS_TIME ticks, for the idle timeout */
static uint32_t s_env_copy_us;                               /* measured cost of the environment hand-over */
static bool     s_prob_active;                               /* this boot is on probation */
static uint64_t s_prob_start;                                /* SYS_TIME ticks, 0 = not started yet */
static uint32_t s_prob_count;                                /* swap counter carried in the note */
static uint64_t s_prob_revert_at;                            /* SYS_TIME ticks, 0 = not reverting */
static uint32_t s_prob_boot[5];                              /* note as found at boot: magic,state,count,crc,rcause */

static const char *const s_state_name[] = {
    "IDLE", "WAIT_CONN", "RECV_HDR", "RECV_IMG", "VERIFY", "VERIFIED", "COMMIT_WAIT", "COMMIT_SWAP"
};
static const char *const s_err_name[] = {
    "none", "wrong-state", "bad-args", "bad-size", "socket",
    "bad-header", "connection", "nvm-error", "crc-mismatch", "env-copy"
};

/* ---------------------------------------------------------------------------
 * CRC32
 *
 * IEEE 802.3 / zlib flavour (reflected, init 0xFFFFFFFF, final XOR) so the PC
 * side can use zlib.crc32() unchanged and the two numbers can be compared as
 * they are. Nibble table (64 bytes) rather than the usual 1 KiB byte table:
 * about 20 cycles/byte, so a full 200 KiB image costs ~35 ms of CPU - which is
 * why the verify pass below is chunked instead of done in one go.
 * ------------------------------------------------------------------------- */
static const uint32_t s_crc_tab[16] = {
    0x00000000u, 0x1DB71064u, 0x3B6E20C8u, 0x26D930ACu,
    0x76DC4190u, 0x6B6B51F4u, 0x4DB26158u, 0x5005713Cu,
    0xEDB88320u, 0xF00F9344u, 0xD6D6A3E8u, 0xCB61B38Cu,
    0x9B64C2B0u, 0x86D3D2D4u, 0xA00AE278u, 0xBDBDF21Cu
};

static uint32_t bl_crc32_update(uint32_t crc, const uint8_t *p, uint32_t n)
{
    while (n-- > 0u) {
        uint8_t b = *p++;
        crc = s_crc_tab[(crc ^ (uint32_t)b) & 0x0Fu] ^ (crc >> 4);
        crc = s_crc_tab[(crc ^ ((uint32_t)b >> 4)) & 0x0Fu] ^ (crc >> 4);
    }
    return crc;
}

/* ---------------------------------------------------------------------------
 * Elapsed time
 *
 * DWT->CYCCNT, not SYS_TIME - and that is not a style preference. Erasing or
 * programming the bank the CPU is executing from stalls the bus for the whole
 * operation (25.6.6.1), and SYS_TIME's hardware counter here is a 16-bit TC0
 * that wraps every 1 ms (plib_tc0.c: COUNT16, DIV1, CC0 = 59999) with its
 * overflow interrupt extending the count in software. A stall of ~38 ms means
 * ~37 of those overflows are never serviced and are simply lost, so SYS_TIME
 * under-reports badly: it put the environment copy at 6.4 ms when the real cost
 * is around 120 ms. The Cortex-M4 cycle counter keeps counting core clocks
 * through a bus stall, so it measures what actually happened.
 *
 * Same counter cpuload.c uses; arming it is idempotent and deliberately does
 * NOT reset CYCCNT, which would zero it out from under a running cpuload
 * measurement (see the comment there). Only deltas are used, so its ~35.8 s
 * rollover at 120 MHz does not matter for anything measured here.
 * ------------------------------------------------------------------------- */
#define BL_CYCLES_PER_US   (SYS_TIME_CPU_CLOCK_FREQUENCY / 1000000u)

static uint32_t bl_cyc_now(void)
{
    CoreDebug->DEMCR |= (uint32_t)CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= (uint32_t)DWT_CTRL_CYCCNTENA_Msk;
    return DWT->CYCCNT;
}

static uint32_t bl_cyc_us_since(uint32_t start)
{
    return (DWT->CYCCNT - start) / BL_CYCLES_PER_US;   /* wraps correctly */
}

/* ---------------------------------------------------------------------------
 * Caches
 *
 * Two of them sit between a flash read and the array, and both have to be
 * dealt with before reading back something that was just programmed:
 *
 *  - the CMCC, which startup_xc32.c enables at boot;
 *  - the NVMCTRL's own line cache, one bit per AHB port (CTRLA.CACHEDIS0/1).
 *
 * CMCC_InvalidateAll() leaves the cache DISABLED - plib_cmcc.c clears CTRL.CEN,
 * waits for CSTS to drop, writes MAINT0.INVALL and never turns CEN back on.
 * Calling it and walking away would silently cost the whole system its cache
 * for good, so the re-enable here is not redundant.
 * ------------------------------------------------------------------------- */
static void bl_cache_invalidate(void)
{
    CMCC_InvalidateAll();
    CMCC_REGS->CMCC_CTRL = CMCC_CTRL_CEN_Msk;
}

/* CRC a range of flash with the NVM line cache out of the way. The CMCC must
 * have been invalidated once before the first call of a pass (bl_cache_invalidate). */
static uint32_t bl_crc32_flash(uint32_t addr, uint32_t len, uint32_t crc)
{
    uint16_t ctrla = NVMCTRL_REGS->NVMCTRL_CTRLA;
    NVMCTRL_REGS->NVMCTRL_CTRLA = (uint16_t)(ctrla | NVMCTRL_CTRLA_CACHEDIS0_Msk | NVMCTRL_CTRLA_CACHEDIS1_Msk);
    crc = bl_crc32_update(crc, (const uint8_t *)addr, len);
    NVMCTRL_REGS->NVMCTRL_CTRLA = ctrla;
    return crc;
}

/* ---------------------------------------------------------------------------
 * NVM helpers
 * ------------------------------------------------------------------------- */

/* 'A' if BANKA is the one mapped at 0x0 (STATUS.AFIRST=1), else 'B'. Says which
 * PHYSICAL bank is running; it does not change any address used here. */
static char bl_active_bank(void)
{
    return ((NVMCTRL_REGS->NVMCTRL_STATUS & NVMCTRL_STATUS_AFIRST_Msk) != 0u) ? 'A' : 'B';
}

/* Read and clear the NVMCTRL error flags. NVMCTRL_ErrorGet() returns the whole
 * INTFLAG (DONE included), so the caller must mask - a completed command
 * legitimately has DONE set. */
static uint16_t bl_nvm_errors(void)
{
    return (uint16_t)(NVMCTRL_ErrorGet() & BL_NVM_ERR_MASK);
}

static void bl_close_socket(void)
{
    if (s_sock != INVALID_SOCKET) {
        (void)TCPIP_TCP_Close(s_sock);   /* graceful by default: sends what is still queued, then FIN */
        s_sock = INVALID_SOCKET;
    }
}

static void bl_sock_say(const char *line)
{
    if (s_sock != INVALID_SOCKET) {
        (void)TCPIP_TCP_ArrayPut(s_sock, (const uint8_t *)line, (uint16_t)strlen(line));
        (void)TCPIP_TCP_Flush(s_sock);
    }
}

/* Give up: tell the client if there still is one, drop the socket, go back to
 * IDLE. The running firmware is never affected by this - only the contents of
 * the inactive bank become meaningless, and nothing boots from there. */
static void bl_fail(bl_err_t err, const char *what)
{
    char line[80];
    s_err = err;
    (void)snprintf(line, sizeof line, "BL: ERR %s\r\n", what);
    bl_sock_say(line);
    bl_close_socket();
    s_state = BL_IDLE;
    SYS_CONSOLE_PRINT("bootload: failed - %s (rx=%lu written=%lu)\n\r",
                      what, (unsigned long)s_received, (unsigned long)s_written);
}

static void bl_mark_progress(void)
{
    s_last_progress = SYS_TIME_Counter64Get();
}

static bool bl_timed_out(uint32_t ms)
{
    uint32_t hz = SYS_TIME_FrequencyGet();
    if (hz == 0u) {
        return false;
    }
    return ((SYS_TIME_Counter64Get() - s_last_progress) > (((uint64_t)hz * ms) / 1000ULL));
}

/* ---------------------------------------------------------------------------
 * Probation note in Backup RAM - see the block comment at the top
 * ------------------------------------------------------------------------- */
static uint32_t bl_prob_crc(uint32_t magic, uint32_t state, uint32_t count)
{
    uint32_t words[3];
    words[0] = magic;
    words[1] = state;
    words[2] = count;
    return bl_crc32_update(0xFFFFFFFFu, (const uint8_t *)words, sizeof words) ^ 0xFFFFFFFFu;
}

static bool bl_prob_valid(void)
{
    return (BL_PROB->magic == BL_PROB_MAGIC) &&
           (BL_PROB->crc == bl_prob_crc(BL_PROB->magic, BL_PROB->state, BL_PROB->count));
}

static void bl_prob_write(uint32_t state, uint32_t count)
{
    BL_PROB->magic = BL_PROB_MAGIC;
    BL_PROB->state = state;
    BL_PROB->count = count;
    BL_PROB->crc = bl_prob_crc(BL_PROB_MAGIC, state, count);

    /* The very next thing a caller does is BKSWRST, which resets the device
     * within microseconds. Stores to RAM are posted - 'volatile' orders them
     * against each other but does not wait for them to reach memory - so
     * without this barrier the reset can happen while the note is still in the
     * write buffer, and the image that boots finds nothing.
     *
     * That is not theory: the first probation run on hardware (2026-09-04) came
     * up with the note reading all zeros and 'probation=off', with RCAUSE
     * correctly showing an NVM reset. NVIC_SystemReset() has a DSB built in for
     * the same reason; NVMCTRL_BankSwap() is a bare register write and has not. */
    __DSB();
}

static void bl_prob_clear(void)
{
    BL_PROB->magic = 0u;
    BL_PROB->state = 0u;
    BL_PROB->count = 0u;
    BL_PROB->crc = 0u;
}

/* Seconds left before this boot rolls itself back, 0 if not on probation. */
static uint32_t bl_prob_left_s(void)
{
    uint32_t hz = SYS_TIME_FrequencyGet();
    uint64_t elapsed;

    if (!s_prob_active || (s_prob_start == 0u) || (hz == 0u)) {
        return s_prob_active ? BL_PROBATION_S : 0u;
    }
    elapsed = (SYS_TIME_Counter64Get() - s_prob_start) / hz;
    return (elapsed >= (uint64_t)BL_PROBATION_S) ? 0u : (uint32_t)(BL_PROBATION_S - elapsed);
}

/* "off", "ARMED 137s left" or "reverting" - for the status and info lines. */
static const char *bl_prob_text(void)
{
    static char buf[24];

    if (!s_prob_active) {
        return "off";
    }
    if (s_prob_revert_at != 0u) {
        return "reverting";
    }
    (void)snprintf(buf, sizeof buf, "ARMED %lus left", (unsigned long)bl_prob_left_s());
    return buf;
}

static void bl_prob_boot_check(void)
{
    bool nvm_reset = ((RSTC_REGS->RSTC_RCAUSE & RSTC_RCAUSE_NVM_Msk) != 0u);

    /* Snapshot exactly what this boot found, before anything is cleared - the
     * console is not up yet at this point (BOOTLOAD_Initialize runs inside
     * SYS_Initialize), so 'bootload info' is where it can be read. */
    s_prob_boot[0] = BL_PROB->magic;
    s_prob_boot[1] = BL_PROB->state;
    s_prob_boot[2] = BL_PROB->count;
    s_prob_boot[3] = BL_PROB->crc;
    s_prob_boot[4] = (uint32_t)RSTC_REGS->RSTC_RCAUSE;

    if (!bl_prob_valid()) {
        bl_prob_clear();            /* undefined Backup RAM on a cold start, or nothing pending */
        return;
    }
    if (!nvm_reset) {
        /* The note survived, but this boot did not come from a bank swap - a
         * power cycle or a plain reset in between. Someone has been at the
         * board; do not roll anything back behind their back. */
        SYS_CONSOLE_PRINT("bootload: discarding a stale probation note (reset cause was not NVM)\n\r");
        bl_prob_clear();
        return;
    }

    s_prob_count = BL_PROB->count;
    if (BL_PROB->state == BL_PROB_ARMED) {
        s_prob_active = true;
        s_prob_start = 0u;          /* started on the first Tasks() pass */
        SYS_CONSOLE_PRINT("bootload: this image is ON PROBATION after a bank swap (#%lu).\n\r"
                          "bootload: 'bootload confirm' within %us keeps it - otherwise the board "
                          "swaps back to the previous image.\n\r",
                          (unsigned long)s_prob_count, (unsigned)BL_PROBATION_S);
    } else if (BL_PROB->state == BL_PROB_REVERTED) {
        SYS_CONSOLE_PRINT("bootload: the previous update was ROLLED BACK - it was not confirmed "
                          "within %us, so this (older) image was restored. Swap #%lu.\n\r",
                          (unsigned)BL_PROBATION_S, (unsigned long)s_prob_count);
        bl_prob_clear();
    } else {
        bl_prob_clear();
    }
}

/* Called every Tasks() pass while this boot is on probation. */
static void bl_prob_tasks(void)
{
    uint32_t hz;

    /* Rollback announced, waiting for the console to drain before the reset -
     * same reason as BL_COMMIT_SWAP: SYS_CONSOLE output only leaves the buffer
     * when the console task runs, and the single line saying why the board just
     * rebooted is worth more than the 300 ms it costs. */
    if (s_prob_revert_at != 0u) {
        if (SYS_TIME_Counter64Get() >= s_prob_revert_at) {
            bl_prob_write(BL_PROB_REVERTED, s_prob_count);
            NVMCTRL_BankSwap();     /* resets; the restored image reports the rollback */
        }
        return;
    }

    if (s_prob_start == 0u) {
        s_prob_start = SYS_TIME_Counter64Get();
        return;
    }
    if (bl_prob_left_s() != 0u) {
        return;
    }

    SYS_CONSOLE_PRINT("bootload: not confirmed within %us - swapping back to the previous "
                      "image now\n\r", (unsigned)BL_PROBATION_S);
    hz = SYS_TIME_FrequencyGet();
    s_prob_revert_at = SYS_TIME_Counter64Get() +
                       (((uint64_t)hz * BL_SWAP_DELAY_MS) / 1000ULL);
}

/* ---------------------------------------------------------------------------
 * State machine
 * ------------------------------------------------------------------------- */

static void bl_start_verify(void)
{
    s_verify_off = 0u;
    s_verify_crc = 0xFFFFFFFFu;
    s_state = BL_VERIFY;
    bl_mark_progress();
}

static void bl_recv_header(void)
{
    uint16_t ready = TCPIP_TCP_GetIsReady(s_sock);
    uint32_t want = (uint32_t)sizeof s_hdr - s_hdr_fill;
    uint16_t got;

    if (ready == 0u) {
        return;
    }
    if ((uint32_t)ready < want) {
        want = ready;
    }
    got = TCPIP_TCP_ArrayGet(s_sock, ((uint8_t *)s_hdr) + s_hdr_fill, (uint16_t)want);
    s_hdr_fill += got;
    if (got > 0u) {
        bl_mark_progress();
    }
    if (s_hdr_fill < sizeof s_hdr) {
        return;
    }

    /* The header repeats what 'arm' already announced. That is the point: a
     * stray connection, or a second GUI that armed nothing, cannot get a single
     * byte into flash. */
    if (s_hdr[0] != BL_MAGIC) {
        bl_fail(BL_E_HEADER, "magic");
        return;
    }
    if ((bl_crc32_update(0xFFFFFFFFu, (const uint8_t *)s_hdr, 12u) ^ 0xFFFFFFFFu) != s_hdr[3]) {
        bl_fail(BL_E_HEADER, "header-crc");
        return;
    }
    if ((s_hdr[1] != s_size) || (s_hdr[2] != s_crc_want)) {
        bl_fail(BL_E_HEADER, "does-not-match-arm");
        return;
    }

    s_erase_due = true;              /* first page of block 0 still has to be erased */
    s_state = BL_RECV_IMG;
    SYS_CONSOLE_PRINT("bootload: receiving %lu bytes into 0x%08lX (bank %c is running)\n\r",
                      (unsigned long)s_size, (unsigned long)BL_TARGET_BASE, bl_active_bank());
}

static void bl_recv_image(void)
{
    uint32_t budget = BL_BUDGET;

    while (budget > 0u) {
        /* A full page in RAM: erase its block if it starts one, then program it.
         * Only THIS part waits for the NVM - draining the socket into s_page
         * continues while an erase runs, which is the whole point of programming
         * the bank the CPU is not executing from (RWW). */
        if (s_fill == BL_PAGE) {
            uint16_t nvm_err;

            if (NVMCTRL_IsBusy()) {
                break;               /* erase or previous page write still running - next call */
            }
            nvm_err = bl_nvm_errors();
            if (nvm_err != 0u) {
                bl_fail(BL_E_NVM, "nvm-error");
                return;
            }
            if (s_erase_due) {
                (void)NVMCTRL_BlockErase(BL_TARGET_BASE + s_written);
                s_erase_due = false;
                break;               /* come back when STATUS.READY is up again */
            }
            (void)NVMCTRL_PageWrite(s_page, BL_TARGET_BASE + s_written);
            s_written += BL_PAGE;
            s_fill = 0u;
            s_erase_due = ((s_written % BL_BLOCK) == 0u);
            bl_mark_progress();

            if (s_written >= s_size) {
                bl_start_verify();
                return;
            }
            continue;
        }

        {
            uint16_t ready = TCPIP_TCP_GetIsReady(s_sock);
            uint32_t want = BL_PAGE - s_fill;
            uint16_t got;

            if (ready == 0u) {
                break;               /* nothing waiting right now */
            }
            if (want > budget) {
                want = budget;
            }
            if ((uint32_t)ready < want) {
                want = ready;
            }
            got = TCPIP_TCP_ArrayGet(s_sock, ((uint8_t *)s_page) + s_fill, (uint16_t)want);
            if (got == 0u) {
                break;   /* ready>0 but nothing handed over: do not spin the main loop */
            }
            s_fill += got;
            s_received += got;
            budget -= got;
            bl_mark_progress();
        }

        /* Last page of a shorter-than-a-page tail: pad with 0xFF (an erased
         * flash byte) so the page write below has a full page to commit. */
        if ((s_received >= s_size) && (s_fill < BL_PAGE) && ((s_written + s_fill) >= s_size)) {
            (void)memset(((uint8_t *)s_page) + s_fill, 0xFF, BL_PAGE - s_fill);
            s_fill = BL_PAGE;
        }
    }
}

static void bl_verify(void)
{
    uint32_t chunk;

    /* The last page write may still be running. Reading the bank being
     * programmed would stall the bus until it finishes (25.6.6.1) - correct,
     * but pointlessly so, and this is also where the last write's error flags
     * are picked up. */
    if (NVMCTRL_IsBusy()) {
        return;
    }
    if (s_verify_off == 0u) {
        if (bl_nvm_errors() != 0u) {
            bl_fail(BL_E_NVM, "nvm-error");
            return;
        }
        bl_cache_invalidate();       /* once per pass - see bl_crc32_flash() */
    }

    chunk = s_size - s_verify_off;
    if (chunk > BL_VERIFY_CHUNK) {
        chunk = BL_VERIFY_CHUNK;
    }
    s_verify_crc = bl_crc32_flash(BL_TARGET_BASE + s_verify_off, chunk, s_verify_crc);
    s_verify_off += chunk;

    if (s_verify_off < s_size) {
        return;
    }

    s_verify_crc ^= 0xFFFFFFFFu;
    if (s_verify_crc != s_crc_want) {
        char line[96];
        (void)snprintf(line, sizeof line, "BL: ERR crc got=0x%08lX want=0x%08lX\r\n",
                       (unsigned long)s_verify_crc, (unsigned long)s_crc_want);
        bl_sock_say(line);
        bl_close_socket();
        s_err = BL_E_CRC;
        s_state = BL_IDLE;
        SYS_CONSOLE_PRINT("bootload: CRC mismatch - image in the inactive bank is NOT usable\n\r");
        return;
    }

    {
        char line[96];
        (void)snprintf(line, sizeof line, "BL: OK written=%lu crc=0x%08lX\r\n",
                       (unsigned long)s_size, (unsigned long)s_verify_crc);
        bl_sock_say(line);
    }
    bl_close_socket();
    s_err = BL_E_NONE;
    s_state = BL_VERIFIED;
    SYS_CONSOLE_PRINT("bootload: image verified in the inactive bank (%lu bytes, crc=0x%08lX).\n\r"
                      "bootload: 'bootload commit' activates it (bank swap + reset).\n\r",
                      (unsigned long)s_size, (unsigned long)s_verify_crc);
}

/* ---------------------------------------------------------------------------
 * Commit: hand the environment over, then swap the banks
 *
 * The emulated EEPROM lives at a FIXED logical address, 0xFC000 - which is the
 * top of whichever bank is currently NOT running, because the active bank is
 * mapped at 0x0 and the other one directly above it. A bank swap therefore
 * moves that address onto different physical flash: the record the running
 * firmware has been reading would be at 0x7C000 after the swap, where nothing
 * looks for it, and the new firmware would find a blank window at 0xFC000,
 * format it, and come up with the compiled-in default IP - disconnecting itself
 * from the very network the update arrived over.
 *
 * So the last thing before BKSWRST is a byte-exact copy of the whole 16 KiB
 * window from 0xFC000 to 0x7C000. After the swap, 0xFC000 is the top of the
 * bank that was active during the transfer - exactly where the copy landed - and
 * the new firmware finds its environment unchanged. The copy is raw, so it is
 * agnostic to the emulated EEPROM's page headers and wear-levelling state.
 *
 * The destination is in the bank the CPU is executing from, so unlike the image
 * write this one is NOT read-while-write: the AHB stalls for the duration of
 * every erase and page write (25.6.6.1). Measured cost is well under a second,
 * immediately before a deliberate reset. See docs/dual-bank-bootloader-plan.md
 * section 4 for why this option was chosen over moving the EEPROM window.
 * ------------------------------------------------------------------------- */
static bool bl_copy_region(uint32_t dst, uint32_t src, uint32_t len)
{
    uint32_t off;

    NVMCTRL_SetWriteMode(NVMCTRL_WMODE_MAN);
    (void)bl_nvm_errors();

    for (off = 0u; off < len; off += BL_BLOCK) {
        (void)NVMCTRL_BlockErase(dst + off);
        while (NVMCTRL_IsBusy()) {
            /* stalls here: same bank the CPU fetches from */
        }
        if (bl_nvm_errors() != 0u) {
            return false;
        }
    }

    bl_cache_invalidate();
    for (off = 0u; off < len; off += BL_PAGE) {
        (void)memcpy(s_page, (const void *)(src + off), BL_PAGE);
        (void)NVMCTRL_PageWrite(s_page, dst + off);
        while (NVMCTRL_IsBusy()) {
            /* as above */
        }
        if (bl_nvm_errors() != 0u) {
            return false;
        }
    }

    /* Read back before trusting it: if this copy is wrong the new firmware comes
     * up without a network configuration, which is worse than not updating. */
    bl_cache_invalidate();
    return (memcmp((const void *)dst, (const void *)src, len) == 0);
}

static void bl_commit_copy(void)
{
    uint32_t t0;

    /* Anything the emulated EEPROM still holds in its page buffer has to be in
     * flash before the copy - otherwise it would be lost with the old bank. */
    (void)EMU_EEPROM_PageBufferCommit();

    t0 = bl_cyc_now();
    if (!bl_copy_region(BL_ENV_SHADOW, BL_ENV_WINDOW, BL_ENV_SIZE)) {
        s_err = BL_E_ENVCOPY;
        s_state = BL_VERIFIED;      /* image is still fine - 'commit' may be retried */
        SYS_CONSOLE_PRINT("bootload: environment copy FAILED - banks NOT swapped, "
                          "nothing changed\n\r");
        return;
    }
    s_env_copy_us = bl_cyc_us_since(t0);

    /* Say it, then swap one console-drain later. SYS_CONSOLE output is queued and
     * only goes out when the console task runs from the main loop, so a BKSWRST
     * issued right here would reset the device with this line still in the
     * buffer - and the one record of whether the hand-over worked would be lost
     * exactly when it is wanted. */
    SYS_CONSOLE_PRINT("bootload: environment copied 0x%08lX -> 0x%08lX in %lu us; "
                      "swapping banks in %ums (device resets)\n\r",
                      (unsigned long)BL_ENV_WINDOW, (unsigned long)BL_ENV_SHADOW,
                      (unsigned long)s_env_copy_us, (unsigned)BL_SWAP_DELAY_MS);
    s_state = BL_COMMIT_SWAP;
    bl_mark_progress();
}

static void bl_commit_swap(void)
{
    /* Leave the note for the image that is about to boot. Written here rather
     * than with the environment copy so that a failed copy - which does not
     * swap - never arms a probation nobody is on. */
    bl_prob_write(BL_PROB_ARMED, s_prob_count + 1u);

    NVMCTRL_BankSwap();             /* BKSWRST: atomic, flips AFIRST, resets the device */

    /* Not reached. If it ever is, the command was rejected outright - and the
     * note has to go, or the next ordinary reset would look like a swap. */
    bl_prob_clear();
    s_err = BL_E_NVM;
    s_state = BL_VERIFIED;
    SYS_CONSOLE_PRINT("bootload: BKSWRST did not take effect (INTFLAG=0x%04X)\n\r",
                      (unsigned)NVMCTRL_ErrorGet());
}

void BOOTLOAD_Tasks(void)
{
    if (s_prob_active) {
        bl_prob_tasks();            /* orthogonal to the transfer state machine */
    }

    switch (s_state) {
    case BL_IDLE:
    case BL_VERIFIED:
        break;

    case BL_WAIT_CONN:
        if (TCPIP_TCP_IsConnected(s_sock)) {
            (void)TCPIP_TCP_WasReset(s_sock);   /* clear the stack's reset flag, as iperf.c/testserver.c do */
            s_hdr_fill = 0u;
            s_state = BL_RECV_HDR;
            bl_mark_progress();
            SYS_CONSOLE_PRINT("bootload: client connected on port %u\n\r", (unsigned)s_port);
        } else if (bl_timed_out(BL_CONN_TIMEOUT_MS)) {
            bl_fail(BL_E_CONN, "no-client-timeout");
        } else {
            /* keep waiting */
        }
        break;

    case BL_RECV_HDR:
    case BL_RECV_IMG:
        if (TCPIP_TCP_WasReset(s_sock) || TCPIP_TCP_WasDisconnected(s_sock)) {
            bl_fail(BL_E_CONN, "connection-lost");
            break;
        }
        if (bl_timed_out(BL_STALL_TIMEOUT_MS)) {
            bl_fail(BL_E_CONN, "stalled");
            break;
        }
        if (s_state == BL_RECV_HDR) {
            bl_recv_header();
        } else {
            bl_recv_image();
        }
        break;

    case BL_VERIFY:
        bl_verify();
        break;

    case BL_COMMIT_WAIT:
        /* 'commit' answers first and swaps a moment later: the reply is a TCP
         * segment that the stack can only put on the wire from its own task, so
         * resetting inside the command handler would take the console down
         * before the client ever saw the acknowledgement. */
        if (bl_timed_out(BL_COMMIT_DELAY_MS)) {
            bl_commit_copy();
        }
        break;

    case BL_COMMIT_SWAP:
        if (bl_timed_out(BL_SWAP_DELAY_MS)) {
            bl_commit_swap();
        }
        break;

    default:
        break;
    }
}

bool BOOTLOAD_IsActive(void)
{
    return ((s_state != BL_IDLE) && (s_state != BL_VERIFIED));
}

/* ---------------------------------------------------------------------------
 * CLI
 * ------------------------------------------------------------------------- */

static void bl_print_status(SYS_CMD_DEVICE_NODE *pCmdIO)
{
    CMD_PRINT(pCmdIO, "BL: state=%s bank=%c rx=%lu written=%lu size=%lu err=%u (%s) probation=%s\n\r",
              s_state_name[s_state], bl_active_bank(),
              (unsigned long)s_received, (unsigned long)s_written, (unsigned long)s_size,
              (unsigned)s_err, s_err_name[s_err], bl_prob_text());
}

static void bl_cmd_arm(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    uint32_t size, crc;

    if (BOOTLOAD_IsActive()) {
        s_err = BL_E_STATE;
        CMD_PRINT(pCmdIO, "BL: ERR already-active (state=%s)\n\r", s_state_name[s_state]);
        return;
    }
    if (argc < 4) {
        s_err = BL_E_ARGS;
        CMD_PRINT(pCmdIO, "BL: ERR usage: bootload arm <bytes> <crc32hex>\n\r");
        return;
    }
    size = (uint32_t)strtoul(argv[2], NULL, 0);
    crc  = (uint32_t)strtoul(argv[3], NULL, 16);

    if ((size == 0u) || (size > BL_MAX_IMAGE)) {
        s_err = BL_E_SIZE;
        CMD_PRINT(pCmdIO, "BL: ERR size %lu not in 1..%lu\n\r",
                  (unsigned long)size, (unsigned long)BL_MAX_IMAGE);
        return;
    }

    s_sock = TCPIP_TCP_ServerOpen(IP_ADDRESS_TYPE_IPV4, (TCP_PORT)s_port, 0);
    if (s_sock == INVALID_SOCKET) {
        s_err = BL_E_SOCKET;
        CMD_PRINT(pCmdIO, "BL: ERR server-open\n\r");
        return;
    }

    s_size = size;
    s_crc_want = crc;
    s_written = 0u;
    s_received = 0u;
    s_fill = 0u;
    s_hdr_fill = 0u;
    s_erase_due = false;
    s_err = BL_E_NONE;
    s_state = BL_WAIT_CONN;
    bl_mark_progress();

    /* The PLIB's page write only issues the WP command when CTRLA.WMODE is MAN
     * (plib_nvmctrl.c) - it is MAN out of reset and nothing here changes it, but
     * a quad-word write elsewhere sets and restores it, so pin it explicitly
     * rather than inherit whatever the last writer left behind. */
    NVMCTRL_SetWriteMode(NVMCTRL_WMODE_MAN);

    CMD_PRINT(pCmdIO, "BL: READY port=%u max=%lu\n\r", (unsigned)s_port, (unsigned long)BL_MAX_IMAGE);
}

static void bl_cmd_abort(SYS_CMD_DEVICE_NODE *pCmdIO)
{
    if (!BOOTLOAD_IsActive() && (s_state != BL_VERIFIED)) {
        CMD_PRINT(pCmdIO, "BL: ERR not-active\n\r");
        return;
    }
    bl_close_socket();
    s_state = BL_IDLE;
    s_err = BL_E_NONE;
    CMD_PRINT(pCmdIO, "BL: ABORTED\n\r");
}

/* Activate the image that is sitting verified in the inactive bank. Replies
 * first and does the work BL_COMMIT_DELAY_MS later, from BOOTLOAD_Tasks() - see
 * the BL_COMMIT_WAIT case there for why. */
static void bl_cmd_commit(SYS_CMD_DEVICE_NODE *pCmdIO)
{
    if (s_state != BL_VERIFIED) {
        s_err = BL_E_STATE;
        CMD_PRINT(pCmdIO, "BL: ERR no verified image (state=%s)\n\r", s_state_name[s_state]);
        return;
    }
    s_state = BL_COMMIT_WAIT;
    bl_mark_progress();
    CMD_PRINT(pCmdIO, "BL: COMMIT env-copy+bankswap in %ums, the board will reset\n\r",
              (unsigned)BL_COMMIT_DELAY_MS);
}

/* Clears the probation note: someone reached this board over the network and is
 * happy with what is running. The updater sends it after 'bootload verify' has
 * confirmed the running image; a person can type it. Never an error - confirming
 * when nothing is pending is a no-op, not a failure. */
static void bl_cmd_confirm(SYS_CMD_DEVICE_NODE *pCmdIO)
{
    if (s_prob_active) {
        s_prob_active = false;
        s_prob_revert_at = 0u;
        bl_prob_clear();
        CMD_PRINT(pCmdIO, "BL: CONFIRMED image kept, rollback cancelled\n\r");
        SYS_CONSOLE_PRINT("bootload: confirmed - the new image is kept\n\r");
    } else {
        bl_prob_clear();
        CMD_PRINT(pCmdIO, "BL: CONFIRMED nothing was pending\n\r");
    }
}

static void bl_cmd_info(SYS_CMD_DEVICE_NODE *pCmdIO)
{
    char bank = bl_active_bank();

    CMD_PRINT(pCmdIO, "bootload - dual-bank firmware update (receive, verify, commit)\n\r");
    CMD_PRINT(pCmdIO, "  running bank      : %c (STATUS.AFIRST=%u), mapped at 0x00000000\n\r",
              bank, (unsigned)((NVMCTRL_REGS->NVMCTRL_STATUS & NVMCTRL_STATUS_AFIRST_Msk) != 0u));
    CMD_PRINT(pCmdIO, "  update target     : 0x%08lX .. 0x%08lX (bank %c)\n\r",
              (unsigned long)BL_TARGET_BASE, (unsigned long)(BL_TARGET_BASE + BL_MAX_IMAGE - 1u),
              (bank == 'A') ? 'B' : 'A');
    CMD_PRINT(pCmdIO, "  max image         : %lu bytes (bank %lu - EEPROM window 16384)\n\r",
              (unsigned long)BL_MAX_IMAGE, (unsigned long)BL_BANK_SIZE);
    CMD_PRINT(pCmdIO, "  live env window   : 0x%08lX .. 0x%08lX (never written here)\n\r",
              (unsigned long)BL_ENV_WINDOW, (unsigned long)(BL_ENV_WINDOW + 0x4000u - 1u));
    CMD_PRINT(pCmdIO, "  page / block      : %lu / %lu bytes\n\r",
              (unsigned long)BL_PAGE, (unsigned long)BL_BLOCK);
    CMD_PRINT(pCmdIO, "  region locks      : RUNLOCK=0x%08lX (1 = unlocked)\n\r",
              (unsigned long)NVMCTRL_RegionLockStatusGet());
    CMD_PRINT(pCmdIO, "  data port         : %u\n\r", (unsigned)s_port);
    CMD_PRINT(pCmdIO, "  probation         : %s (window %us, swap #%lu)\n\r",
              bl_prob_text(), (unsigned)BL_PROBATION_S, (unsigned long)s_prob_count);
    CMD_PRINT(pCmdIO, "  note found at boot: magic=0x%08lX state=%lu count=%lu crc=0x%08lX rcause=0x%02lX\n\r",
              (unsigned long)s_prob_boot[0], (unsigned long)s_prob_boot[1],
              (unsigned long)s_prob_boot[2], (unsigned long)s_prob_boot[3],
              (unsigned long)s_prob_boot[4]);
    bl_print_status(pCmdIO);
}

/* CRC32 over the RUNNING image at 0x0 - the post-reboot proof that the image
 * that is executing is the one that was sent. Blocking on purpose: it is a
 * one-shot console command, ~35 ms of CPU for a 200 KiB image, and making it
 * asynchronous would mean carrying a pCmdIO around for a reply that arrives
 * later. The transfer's own verify pass (bl_verify) is the chunked one. */
static void bl_cmd_verify(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    uint32_t size, want, crc;

    if (argc < 4) {
        CMD_PRINT(pCmdIO, "BL: ERR usage: bootload verify <bytes> <crc32hex>\n\r");
        return;
    }
    size = (uint32_t)strtoul(argv[2], NULL, 0);
    want = (uint32_t)strtoul(argv[3], NULL, 16);
    if ((size == 0u) || (size > BL_MAX_IMAGE)) {
        CMD_PRINT(pCmdIO, "BL: ERR size %lu not in 1..%lu\n\r",
                  (unsigned long)size, (unsigned long)BL_MAX_IMAGE);
        return;
    }

    bl_cache_invalidate();
    crc = bl_crc32_flash(0x00000000u, size, 0xFFFFFFFFu) ^ 0xFFFFFFFFu;

    CMD_PRINT(pCmdIO, "BL: RUNNING bank=%c size=%lu crc=0x%08lX match=%u\n\r",
              bl_active_bank(), (unsigned long)size, (unsigned long)crc, (unsigned)(crc == want));
}

/* Proof of the one assumption everything else rests on: that the bank the CPU
 * is NOT executing from really is reachable at 0x00080000, that erase and page
 * write work there, and that the application survives it (run it while iperf or
 * testserver traffic is going through the bridge - that is test T3).
 *
 * Blocking: the erase alone is 50 ms typical, 200 ms worst case (Table 54-40).
 * Acceptable for a hand-typed diagnostic command, and it keeps the test free of
 * any state-machine subtleties it is meant to be independent of. */
static void bl_cmd_selftest(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    uint32_t addr = BL_TARGET_BASE;
    uint32_t i, bad = 0u;
    uint32_t t0;
    uint32_t erase_us, write_us;
    const volatile uint32_t *readback;

    if (BOOTLOAD_IsActive()) {
        CMD_PRINT(pCmdIO, "BL: ERR busy (state=%s)\n\r", s_state_name[s_state]);
        return;
    }
    if (argc >= 3) {
        addr = (uint32_t)strtoul(argv[2], NULL, 16);
    }
    if ((addr < BL_TARGET_BASE) || (addr > (BL_TARGET_BASE + BL_MAX_IMAGE - BL_PAGE)) ||
        ((addr % BL_BLOCK) != 0u)) {
        CMD_PRINT(pCmdIO, "BL: ERR addr must be block-aligned inside 0x%08lX..0x%08lX\n\r",
                  (unsigned long)BL_TARGET_BASE,
                  (unsigned long)(BL_TARGET_BASE + BL_MAX_IMAGE - BL_PAGE));
        return;
    }

    CMD_PRINT(pCmdIO, "bootload selftest: bank %c is running; erasing+writing 0x%08lX\n\r",
              bl_active_bank(), (unsigned long)addr);

    NVMCTRL_SetWriteMode(NVMCTRL_WMODE_MAN);
    (void)bl_nvm_errors();

    t0 = bl_cyc_now();
    (void)NVMCTRL_BlockErase(addr);
    while (NVMCTRL_IsBusy()) { /* 50 ms typ, 200 ms max */ }
    erase_us = bl_cyc_us_since(t0);
    if (bl_nvm_errors() != 0u) {
        CMD_PRINT(pCmdIO, "BL: FAIL erase reported an NVMCTRL error\n\r");
        return;
    }

    bl_cache_invalidate();
    readback = (const volatile uint32_t *)addr;
    for (i = 0u; i < (BL_PAGE / 4u); i++) {
        if (readback[i] != 0xFFFFFFFFu) {
            bad++;
        }
    }
    if (bad != 0u) {
        CMD_PRINT(pCmdIO, "BL: FAIL %lu of %lu words not blank after erase\n\r",
                  (unsigned long)bad, (unsigned long)(BL_PAGE / 4u));
        return;
    }

    for (i = 0u; i < (BL_PAGE / 4u); i++) {
        s_page[i] = 0xB0070000u + i;          /* recognisable in a 'dump' */
    }
    t0 = bl_cyc_now();
    (void)NVMCTRL_PageWrite(s_page, addr);
    while (NVMCTRL_IsBusy()) { /* 1.5 ms typ, 3 ms max */ }
    write_us = bl_cyc_us_since(t0);
    if (bl_nvm_errors() != 0u) {
        CMD_PRINT(pCmdIO, "BL: FAIL page write reported an NVMCTRL error\n\r");
        return;
    }

    bl_cache_invalidate();
    for (i = 0u; i < (BL_PAGE / 4u); i++) {
        if (readback[i] != (0xB0070000u + i)) {
            bad++;
        }
    }

    CMD_PRINT(pCmdIO, "  erase %lu us, write %lu us, readback [0]=0x%08lX [127]=0x%08lX\n\r",
              (unsigned long)erase_us, (unsigned long)write_us,
              (unsigned long)readback[0], (unsigned long)readback[(BL_PAGE / 4u) - 1u]);
    CMD_PRINT(pCmdIO, "BL: selftest %s (%lu mismatching words)\n\r",
              (bad == 0u) ? "PASS" : "FAIL", (unsigned long)bad);
    s_fill = 0u;                                   /* s_page was used as scratch */
}

static void cmd_bootload(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    if (argc < 2) {
        bl_print_status(pCmdIO);
        return;
    }
    if (strcmp(argv[1], "info") == 0) {
        bl_cmd_info(pCmdIO);
    } else if (strcmp(argv[1], "arm") == 0) {
        bl_cmd_arm(pCmdIO, argc, argv);
    } else if (strcmp(argv[1], "abort") == 0) {
        bl_cmd_abort(pCmdIO);
    } else if (strcmp(argv[1], "commit") == 0) {
        bl_cmd_commit(pCmdIO);
    } else if (strcmp(argv[1], "confirm") == 0) {
        bl_cmd_confirm(pCmdIO);
    } else if (strcmp(argv[1], "verify") == 0) {
        bl_cmd_verify(pCmdIO, argc, argv);
    } else if (strcmp(argv[1], "selftest") == 0) {
        bl_cmd_selftest(pCmdIO, argc, argv);
    } else {
        CMD_PRINT(pCmdIO, "usage: bootload [info|arm <bytes> <crc32hex>|abort|commit|confirm|"
                          "verify <bytes> <crc32hex>|selftest [addr_hex]]\n\r");
    }
}

static const SYS_CMD_DESCRIPTOR bootload_cmd_tbl[] = {
    {"bootload", (SYS_CMD_FNC) cmd_bootload,
     ": firmware update into the inactive flash bank (bootload info|arm|abort|commit|confirm|verify|selftest)"},
};

void BOOTLOAD_Initialize(void)
{
    if (!SYS_CMD_ADDGRP(bootload_cmd_tbl, (int)(sizeof bootload_cmd_tbl / sizeof *bootload_cmd_tbl),
                        "bootload", ": dual-bank firmware update")) {
        SYS_CONSOLE_PRINT("BOOTLOAD: SYS_CMD_ADDGRP failed\n\r");
    }

    bl_prob_boot_check();   /* did the previous boot hand this image a probation note? */
}
