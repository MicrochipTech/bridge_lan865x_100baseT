/*******************************************************************************
  LAN865x register access, IEEE transmitter test modes, PLCA control and SQI

  File Name:
    lan865x_diag.c

  Summary:
    Implementation of the diagnostics layer described in lan865x_diag.h.

  Description:
    Everything here revolves around ONE register operation slot. The Harmony
    LAN865x driver is callback-based and must not be called re-entrantly from a
    console command handler, so a command only records what it wants; the actual
    driver call, the timeout supervision and the result printing all happen in
    LAN865X_DIAG_Tasks(), which the application calls from its main loop.

    Two things are built on top of that slot:

      - A verify follow-up. A write on its own only proves the TC6 transaction
        ran, not that the register kept the value, so a completed write can chain
        straight into a read of the same address and report a verdict. This is
        what makes 'testmode' trustworthy without an oscilloscope.

      - A test-mode auto-revert deadline, checked while the slot is free.

    This file has no dependency on the host application: no TCP/IP stack, no
    persistent-configuration layer, no shared state. See the header for how to
    drop it into another project.
 *******************************************************************************/

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>                                          /* strtoul() */
#include <string.h>                                           /* strcmp()  */

#include "definitions.h"
#include "configuration.h"                                   /* DRV_LAN865X_PLCA_* defaults */
#include "config/default/system/console/sys_console.h"
#include "config/default/system/time/sys_time.h"
#include "config/default/driver/lan865x/drv_lan865x.h"
#include "system/command/sys_command.h"
#include "lan865x_diag.h"
#include "cmd_print.h"                                        /* CMD_PRINT/CMD_MSG/CMD_PRINT_OR_CONSOLE - reply to pCmdIO, not always the serial console */

// *****************************************************************************
// Section: Module state
// *****************************************************************************

#define LAN_TIMEOUT_MS  200u    /* Max wait for a LAN865x register callback (matches GM/FOL WAIT-state timeout) */

typedef enum {
    LAN_IDLE,
    LAN_WAIT_READ,
    LAN_WAIT_WRITE,
    LAN_WAIT_RMW
} lan_state_t;

/* Written from the driver callbacks, read from LAN865X_DIAG_Tasks(). */
static volatile bool     s_op_complete = false;
static volatile bool     s_op_success  = false;
static volatile uint32_t s_read_value  = 0u;

static lan_state_t s_state        = LAN_IDLE;
static uint32_t    s_addr         = 0u;
static uint32_t    s_value        = 0u;
static uint32_t    s_mask         = 0u;     /* read-modify-write bit mask (LAN_WAIT_RMW)      */
static uint32_t    s_rmw_final    = 0u;     /* word the driver actually wrote back on RMW     */
static uint64_t    s_expire_tick  = 0u;     /* SYS_TIME tick at which the operation times out */
static bool        s_op_initiated = false;

/* Who to send the eventual result to - LAN865X_DIAG_Tasks() completes the pending
 * register operation asynchronously, well after the command handler that started
 * it (cmd_lan_read/write/rmw, cmd_testmode, cmd_plca_stat) has already returned,
 * so CMD_PRINT(pCmdIO, ...) is not usable there directly. Every one of those
 * commands funnels through this one shared state machine (LAN865X_DIAG_Busy()
 * rejects a second request outright), so there is only ever one requester to
 * remember at a time. NULL means the operation was started from boot/background
 * code (e.g. the testmode auto-revert timer, far below), not a command -
 * CMD_PRINT_OR_CONSOLE() then falls back to the serial console, same as every
 * print here did before this existed. */
static SYS_CMD_DEVICE_NODE *s_diag_pCmdIO = NULL;

/* A write on its own only proves the TC6 transaction ran, not that the register kept the
 * value. These let a completed write chain into a verifying read of the same address, so
 * every mode change reports its own readback. Cleared once the verdict is printed. */
static LAN865X_VERIFY_RESULT s_verify_result = LAN865X_VERIFY_IDLE; /* queryable [VERIFY] outcome */
static bool     s_verify_armed   = false;   /* after this write, read the addr back  */
static bool     s_verify_pending = false;   /* the following read is that readback   */
static uint32_t s_verify_expect  = 0u;
static uint32_t s_verify_mask    = 0u;

/* Optional auto-revert so a forgotten test mode cannot silently keep the link down. */
static bool     s_revert_armed = false;
static uint64_t s_revert_tick  = 0u;

/* PLCA shadow. Seeded from the build configuration so the module answers sensibly
 * before anything has been applied, then kept current by LAN865X_DIAG_ApplyPlca().
 * Holding the count here is what keeps this module independent of any persistent
 * configuration layer. */
static uint8_t s_plca_node_id  = DRV_LAN865X_PLCA_NODE_ID_IDX0;
static uint8_t s_plca_node_cnt = DRV_LAN865X_PLCA_NODE_COUNT_IDX0;

/* plca_stat: reads a fixed sequence of PLCA-related registers through the same
 * single-slot state machine, one lan_read per LAN865X_DIAG_Tasks() iteration.
 * s_plcastat_start_pending covers the one-time counter-enable RMW that precedes
 * the very first sequence; s_plcastat_active covers the sequence itself. */
#define PLCASTAT_STEPS 10u
static const uint32_t s_plcastat_addr[PLCASTAT_STEPS] = {
    LAN865X_PLCA_STS, LAN865X_STS1,   LAN865X_STS3,    LAN865X_PRSSTS,
    LAN865X_TOCNTH,   LAN865X_TOCNTL, LAN865X_BCNCNTH, LAN865X_BCNCNTL,
    LAN865X_STATS10,  LAN865X_T1SPCSDIAG2
};
static uint32_t s_plcastat_val[PLCASTAT_STEPS];
static uint8_t  s_plcastat_idx           = 0u;
static bool     s_plcastat_active        = false;
static bool     s_plcastat_start_pending = false;
static bool     s_plca_ctrs_enabled      = false;

/* SYS_TIME ticks per millisecond, resolved on first use. */
static uint64_t s_ticks_per_ms = 0u;

/* SQI: a small setup sequence (TOID, poll mode, enable - each RMW+verify per the
 * datasheet) followed by a steady state that re-reads SQISTS0 roughly once a
 * second, folding every valid sample into a running min/max/count. Shares the
 * single register slot above: sqi_tasks() only acts while s_state == LAN_IDLE,
 * so lan_read/write/rmw/testmode/plca_node/plca_stat always get first claim on a
 * given pass and SQI simply waits for the next one. */
typedef enum {
    SQI_OFF,
    SQI_SET_TOID,       /* RMW SQICFG0 pending    */
    SQI_SET_POLLMODE,   /* RMW SQICFG2 pending    */
    SQI_ARM,            /* RMW SQICTL SQIEN=1 pending */
    SQI_RUNNING,         /* armed; polling SQISTS0 ~1x/s */
    SQI_RECOVER_CLEAR,   /* SQIERR seen: RMW SQICTL SQIEN=0 */
    SQI_RECOVER_SET      /* re-arm: RMW SQICTL SQIEN=1 */
} sqi_state_t;

#define SQI_POLL_MS  1000u   /* the datasheet's own recommended polling interval */

static sqi_state_t s_sqi_state      = SQI_OFF;
static uint8_t     s_sqi_toid       = LAN865X_SQI_TOID_ALL;
static uint64_t    s_sqi_next_poll  = 0u;
static bool        s_sqi_poll_inflt = false;   /* the in-flight read is our own poll, not a user lan_read */

static uint32_t s_sqi_samples   = 0u;
static uint32_t s_sqi_errors    = 0u;
static uint8_t  s_sqi_last_val  = 0u;
static uint8_t  s_sqi_min_val   = 0u;
static uint8_t  s_sqi_max_val   = 0u;
static uint8_t  s_sqi_last_errc = 0u;
static bool     s_sqi_have_val  = false;

/* Optional periodic auto-print of the report, armed by 'sqi report <seconds>'.
 * Independent of the register slot - it only reads already-accumulated state,
 * so it fires on its own schedule and never competes with lan_read/write/rmw. */
static bool     s_sqi_report_armed = false;
static uint64_t s_sqi_report_period_ticks = 0u;
static uint64_t s_sqi_report_next  = 0u;

// *****************************************************************************
// Section: Helpers
// *****************************************************************************

const char *LAN865X_DIAG_TestModeName(uint32_t mode) {
    switch (mode) {
        case 0u:  return "normal operation";
        case 1u:  return "test mode 1 (output voltage / timing jitter)";
        case 2u:  return "test mode 2 (output droop)";
        case 3u:  return "test mode 3 (PSD mask / transmitter distortion)";
        case 4u:  return "test mode 4 (transmitter high impedance)";
        default:  return "reserved";
    }
}

bool LAN865X_DIAG_Busy(void) {
    return (s_state != LAN_IDLE);
}

/* SQIVAL (0..7) decoded to the SNR/BER range from datasheet DS60001734F 11.5.53. */
static const char *sqi_val_name(uint8_t v) {
    switch (v) {
        case 0u: return "0 worst  (SNR <= ~5dB,  BER >= ~3.8E-2)";
        case 1u: return "1        (SNR ~5-10dB,  BER ~3.8E-2..7.8E-4)";
        case 2u: return "2        (SNR ~10-12dB, BER ~7.8E-4..3.4E-5)";
        case 3u: return "3        (SNR ~12-14dB, BER ~3.4E-5..2.7E-7)";
        case 4u: return "4        (SNR ~14-16dB, BER ~2.7E-7..1.4E-10)";
        case 5u: return "5        (SNR ~16-17dB, BER ~1.4E-10..7.2E-13)";
        case 6u: return "6        (SNR ~17-18dB, BER ~7.2E-13..9.9E-16)";
        case 7u: return "7 best   (SNR >= ~18dB, BER <= ~9.9E-16)";
        default: return "?";
    }
}

/* SQIERRC (0..5), per the same section - only defined while SQIERR is set. */
static const char *sqi_errc_name(uint8_t c) {
    switch (c) {
        case 0u: return "none";
        case 1u: return "low threshold above max limitation";
        case 2u: return "high threshold above max limitation";
        case 3u: return "low threshold below min limitation";
        case 4u: return "high threshold below min limitation";
        case 5u: return "low threshold above high threshold";
        default: return "undefined";
    }
}

uint8_t LAN865X_DIAG_PlcaNodeId(void)  { return s_plca_node_id;  }
uint8_t LAN865X_DIAG_PlcaNodeCnt(void) { return s_plca_node_cnt; }

/* Abandon the current operation. Also drops any pending verify follow-up: a
 * leftover flag would otherwise attach a bogus verdict to whatever register is
 * read next. */
static void lan_abort(void) {
    s_state           = LAN_IDLE;
    s_op_initiated    = false;
    s_verify_armed    = false;
    s_verify_pending  = false;
    s_plcastat_active = false;   /* a timed-out step must not be mistaken for the next one */
    s_sqi_poll_inflt  = false;   /* a timed-out SQI poll must not be mistaken for the next op */
}

/* Fold one SQISTS0 read into the running SQI statistics. Called only for the
 * background poll (s_sqi_poll_inflt), silently - a printed line every second
 * would flood the console. Accumulated state is shown on demand by the 'sqi'
 * command instead. On SQIERR the datasheet requires clearing and re-setting
 * SQIEN before accumulation can resume. */
static void sqi_record_sample(uint32_t sts0) {
    bool    sqierr = (sts0 & LAN865X_SQISTS0_SQIERR) != 0u;
    bool    sqivld = (sts0 & LAN865X_SQISTS0_SQIVLD) != 0u;
    uint8_t val    = (uint8_t)((sts0 & LAN865X_SQISTS0_SQIVAL_MASK) >> LAN865X_SQISTS0_SQIVAL_SHIFT);
    uint8_t errc   = (uint8_t)(sts0 & LAN865X_SQISTS0_SQIERRC_MASK);

    if (sqierr) {
        s_sqi_errors++;
        s_sqi_last_errc = errc;
        SYS_CONSOLE_PRINT("[SQI] error: %s - recovering (SQIEN off/on)\n\r", sqi_errc_name(errc));
        s_sqi_state = SQI_RECOVER_CLEAR;
        return;
    }
    if (!sqivld) {
        return;   /* accumulation still running, nothing new yet */
    }
    s_sqi_last_val = val;
    if (!s_sqi_have_val || (val < s_sqi_min_val)) { s_sqi_min_val = val; }
    if (!s_sqi_have_val || (val > s_sqi_max_val)) { s_sqi_max_val = val; }
    s_sqi_have_val = true;
    s_sqi_samples++;
}

/* Print the accumulated SQI report - the same text for an interactive 'sqi'
 * query and for the periodic auto-report armed by 'sqi report <seconds>'. */
static void sqi_print_report(void) {
    if (!LAN865X_DIAG_SqiActive()) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] not monitoring - 'sqi <node 0..254>' or 'sqi all' to start\n\r");
        return;
    }
    if (s_sqi_toid == LAN865X_SQI_TOID_ALL) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] node: all (weighted)   state: %s\n\r",
                          (s_sqi_state == SQI_RUNNING) ? "running" : "starting");
    } else {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] node: %u   state: %s\n\r",
                          (unsigned int)s_sqi_toid,
                          (s_sqi_state == SQI_RUNNING) ? "running" : "starting");
    }
    if (!s_sqi_have_val) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] no valid sample yet - accumulation duration depends on traffic\n\r");
    } else {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] last: %s\n\r", sqi_val_name(s_sqi_last_val));
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] min: %u   max: %u   samples: %u\n\r",
                          (unsigned int)s_sqi_min_val, (unsigned int)s_sqi_max_val,
                          (unsigned int)s_sqi_samples);
    }
    if (s_sqi_errors > 0u) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] errors: %u (last: %s)\n\r",
                          (unsigned int)s_sqi_errors, sqi_errc_name(s_sqi_last_errc));
    }
}

/* Decode the values collected by a completed plca_stat sequence (see
 * s_plcastat_addr for the order) and print the report. */
static void lan_plcastat_report(void) {
    uint32_t plca_sts = s_plcastat_val[0];
    uint32_t sts1     = s_plcastat_val[1];
    uint32_t sts3     = s_plcastat_val[2];
    uint32_t prssts   = s_plcastat_val[3];
    uint32_t tocnt    = ((s_plcastat_val[4] & 0xFFFFu) << 16) | (s_plcastat_val[5] & 0xFFFFu);
    uint32_t bcncnt   = ((s_plcastat_val[6] & 0xFFFFu) << 16) | (s_plcastat_val[7] & 0xFFFFu);
    uint32_t xcol     = s_plcastat_val[8] & 0xFFu;
    uint32_t cortxcnt = s_plcastat_val[9] & 0xFFFFu;

    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] link: %s (PLCA_STS.PST=%u)\n\r",
                      (plca_sts & 0x8000u) ? "in range" : "OUT OF RANGE",
                      (unsigned int)((plca_sts >> 15) & 0x1u));
    /* Datasheet-confirmed (11.5.16, see LAN865X_PRSSTS in the header): the coordinator's
     * configured TO count between BEACONs, not this node's own PLCA_CTRL1.NODE_CNT. */
    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] coordinator cycle length: %u (PRSSTS.MAXID, may differ from our own NODE_CNT)\n\r",
                      (unsigned int)((prssts >> 8) & 0xFFu));
    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] since last check: %lu transmit opportunities, %lu BEACONs\n\r",
                      (unsigned long)tocnt, (unsigned long)bcncnt);

    /* Per datasheet (11.5.4) this is only accurate when exactly one unmasked STS1 bit is
     * set - our reports routinely show several at once, so treat this as a rough pointer,
     * not a reliable value, whenever more than one [PLCA] event line follows. */
    if ((sts3 & 0xFFu) != 0u) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] last transmit-opportunity event at ID %u (STS3.ERRTOID, unreliable if multiple events below)\n\r",
                          (unsigned int)(sts3 & 0xFFu));
    }

    /* Datasheet (11.5.2): "Physical collision on the network was detected. This does not
     * include logical collisions due to normal operation of PLCA." A duplicate PLCA ID does
     * NOT set this - that shows up as RXINTO/UNEXPB below instead, confirmed on this bench. */
    if (sts1 & LAN865X_STS1_TXCOL) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] event: PHYSICAL TRANSMIT COLLISION (STS1.TXCOL) - not a PLCA addressing conflict, a real electrical-layer fault\n\r");
    }
    if (xcol != 0u) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] excessive collisions since last check: %u (STATS10.XCOL - MAC-level, datasheet warns this can be confused by PLCA's own logical collisions)\n\r",
                          (unsigned int)xcol);
    }
    /* Datasheet section 7.3: the counter actually recommended for this purpose, immune to
     * PLCA's internal logical collisions unlike a plain MAC counter (XCOL above). "In a
     * properly configured and operating PLCA mixing segment, no transmit collisions should
     * be detected and the transmit collision counter should remain zero." */
    if (cortxcnt != 0u) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] corrupted transmissions since last check: %u (T1SPCSDIAG2.CORTXCNT - the datasheet-recommended physical collision counter)\n\r",
                          (unsigned int)cortxcnt);
    }
    if (sts1 & LAN865X_STS1_UNEXPB) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] event: unexpected BEACON received - check for a second coordinator\n\r");
    }
    if (sts1 & LAN865X_STS1_BCNBFTO) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] event: BEACON received before our own transmit opportunity\n\r");
    }
    if (sts1 & LAN865X_STS1_RXINTO) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] event: frame received during our own transmit opportunity\n\r");
    }
    if (sts1 & LAN865X_STS1_EMPCYC) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] event: empty PLCA cycle - no node transmitted\n\r");
    }
    /* Datasheet: ACMA-mode specific. We don't use ACMA on this project; unlikely to fire. */
    if (sts1 & LAN865X_STS1_UNCRS) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] event: unexpected carrier sense during ACMA time slot (STS1.UNCRS)\n\r");
    }
    /* Datasheet: "detection of PLCA BEACON symbols when PLCA is not enabled... the local
     * node is operating with PLCA disabled on a segment with PLCA enabled nodes." Fires on
     * THIS node when PLCA is off locally but others on the segment run PLCA. */
    if (sts1 & LAN865X_STS1_PLCASYM) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] event: PLCA symbols detected while PLCA is disabled locally (STS1.PLCASYM)\n\r");
    }
    if (sts1 & LAN865X_STS1_PSTC) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] event: PLCA status changed since last check\n\r");
    }
    if ((sts1 & LAN865X_STS1_PLCA_MASK) == 0u) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] no PLCA event flags since last check\n\r");
    }
}

// *****************************************************************************
// Section: Driver callbacks
// *****************************************************************************

/* LAN865X register callback for read operations */
static void lan_read_callback(void *reserved1, bool success, uint32_t addr, uint32_t value, void *pTag, void *reserved2) {
    s_op_success = success;
    s_read_value = value;
    s_op_complete = true;
}

/* LAN865X register callback for write operations */
static void lan_write_callback(void *reserved1, bool success, uint32_t addr, uint32_t value, void *pTag, void *reserved2) {
    s_op_success = success;
    s_op_complete = true;
}

/* LAN865X callback for read-modify-write. Unlike a plain write, the driver documents
 * `value` here as the word actually written back into the PHY (drv_lan865x.h), which is
 * worth keeping - lan_write_callback() would discard it and leave the previous read
 * value standing, which then gets reported as the RMW result. */
static void lan_rmw_callback(void *reserved1, bool success, uint32_t addr, uint32_t value, void *pTag, void *reserved2) {
    s_op_success = success;
    s_rmw_final = value;
    s_op_complete = true;
}

// *****************************************************************************
// Section: Programmatic API
// *****************************************************************************

bool LAN865X_DIAG_Read(uint32_t addr) {
    if (s_state != LAN_IDLE) {
        return false;
    }
    s_addr           = addr;
    s_verify_pending = false;
    s_op_complete    = false;
    s_op_initiated   = false;
    s_state          = LAN_WAIT_READ;
    return true;
}

bool LAN865X_DIAG_Write(uint32_t addr, uint32_t value) {
    if (s_state != LAN_IDLE) {
        return false;
    }
    s_addr         = addr;
    s_value        = value;
    s_op_complete  = false;
    s_op_initiated = false;
    s_state        = LAN_WAIT_WRITE;
    return true;
}

LAN865X_VERIFY_RESULT LAN865X_DIAG_VerifyResult(void) {
    return s_verify_result;
}

bool LAN865X_DIAG_Rmw(uint32_t addr, uint32_t mask, uint32_t value) {
    if (s_state != LAN_IDLE) {
        return false;
    }
    s_addr  = addr;
    s_mask  = mask;
    s_value = value;

    /* Verify the masked field only; bits elsewhere in the register are none of our business.
     * Self-clearing bits (e.g. T1SPMACTL.RST) will legitimately report FAIL here. */
    s_verify_expect  = value;
    s_verify_mask    = mask;
    s_verify_armed   = true;
    s_verify_pending = false;
    s_verify_result  = LAN865X_VERIFY_PENDING;
    s_op_complete    = false;
    s_op_initiated   = false;
    s_state          = LAN_WAIT_RMW;
    return true;
}

bool LAN865X_DIAG_TestMode(uint32_t mode, uint32_t seconds) {
    if (s_state != LAN_IDLE) {
        return false;
    }
    if (mode > LAN865X_TESTMODE_MAX) {
        return false;
    }

    s_addr           = LAN865X_T1STSTCTL;
    s_value          = (mode & 0x7u) << 13u;
    s_verify_expect  = s_value;
    s_verify_mask    = LAN865X_T1STSTCTL_MASK;
    s_verify_armed   = true;          /* chain into the readback - the actual proof */
    s_verify_pending = false;
    s_op_complete    = false;
    s_op_initiated   = false;
    s_state          = LAN_WAIT_WRITE;

    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[TESTMODE] requesting %u - %s (T1STSTCTL=0x%08X)\n\r",
                      (unsigned int)mode, LAN865X_DIAG_TestModeName(mode), (unsigned int)s_value);

    if (mode == 0u) {
        s_revert_armed = false;
    } else {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[TESTMODE] the T1S link is down while this mode is active\n\r");
        if (seconds > 0u) {
            s_revert_tick  = SYS_TIME_Counter64Get() +
                             (uint64_t)seconds * (uint64_t)SYS_TIME_FrequencyGet();
            s_revert_armed = true;
            CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[TESTMODE] auto-revert armed in %u s\n\r", (unsigned int)seconds);
        } else {
            CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[TESTMODE] no timeout given - revert with 'testmode 0'\n\r");
        }
    }
    return true;
}

/* Apply PLCA node id + node count to the LAN865x. Sets the driver node id and queues
 * the PLCA_CTRL1 register write via the module's state machine. Shared by cmd_plca_node.
 * Skips if a LAN register op is already in progress. */
void LAN865X_DIAG_ApplyPlca(uint8_t node_id, uint8_t node_cnt) {
    if (s_state != LAN_IDLE) {
        SYS_CONSOLE_PRINT("[PLCA] LAN busy - apply skipped (retry when idle)\r\n");
        return;
    }
    s_plca_node_id  = node_id;
    s_plca_node_cnt = node_cnt;
    /* A related in-house project also updates the driver's internal node-id
     * shadow here via DRV_LAN865X_SetPlcaNodeId(), so it survives a driver-triggered
     * Loss-of-Framing-Error re-init. That function is itself a hand-patch to that
     * project's drv_lan865x.h/drv_lan865x_api.c (not a stock Harmony driver API) and is
     * not present in this project's (newer) LAN865x driver package - out of scope for
     * this port (see CLAUDE.md section 3). Known gap: a node id set here is applied to
     * the live register below, but would revert to the compiled/env default across a
     * LOFE-triggered re-init, not just a full reset. */
    /* Write PLCA_CTRL1 register: bits[15:8]=NODE_CNT, bits[7:0]=NODE_ID */
    s_addr         = LAN865X_PLCA_CTRL1;
    s_value        = ((uint32_t)node_cnt << 8u) | node_id;
    s_op_complete  = false;
    s_op_initiated = false;
    s_state        = LAN_WAIT_WRITE;
    SYS_CONSOLE_PRINT("[PLCA] node ID set to %u (NODE_CNT=%u, reg=0x%08lX)\r\n",
                      (unsigned)node_id, (unsigned)node_cnt, s_value);
}

bool LAN865X_DIAG_PlcaStat(void) {
    if (s_state != LAN_IDLE) {
        return false;
    }

    if (!s_plca_ctrs_enabled) {
        /* One-time: TOCNT/BCNCNT stay at zero until CTRCTRL enables them. No
         * verify readback - the counts reported afterwards speak for themselves. */
        s_plca_ctrs_enabled      = true;
        s_plcastat_start_pending = true;
        s_addr           = LAN865X_CTRCTRL;
        s_mask           = LAN865X_CTRCTRL_TOCTRE | LAN865X_CTRCTRL_BCNCTRE;
        s_value          = s_mask;
        s_verify_armed   = false;
        s_verify_pending = false;
        s_op_complete    = false;
        s_op_initiated   = false;
        s_state          = LAN_WAIT_RMW;
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] enabling transmit-opportunity/BEACON counters (one-time)\n\r");
    } else {
        s_plcastat_active = true;
        s_plcastat_idx    = 0u;
        s_addr            = s_plcastat_addr[0];
        s_op_complete     = false;
        s_op_initiated    = false;
        s_state           = LAN_WAIT_READ;
    }
    return true;
}

/* Start (or restart) continuous SQI monitoring. Resets the accumulated
 * statistics and hands the setup sequence to sqi_tasks(), which picks it up
 * as soon as the single register slot is free - it does not have to be free
 * right now. */
void LAN865X_DIAG_SqiStart(uint8_t toid) {
    s_sqi_toid       = toid;
    s_sqi_samples    = 0u;
    s_sqi_errors     = 0u;
    s_sqi_have_val   = false;
    s_sqi_last_val   = 0u;
    s_sqi_min_val    = 0u;
    s_sqi_max_val    = 0u;
    s_sqi_last_errc  = 0u;
    s_sqi_poll_inflt = false;
    s_sqi_state      = SQI_SET_TOID;
}

void LAN865X_DIAG_SqiStop(void) {
    if (s_sqi_state == SQI_OFF) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] already stopped\n\r");
        return;
    }
    s_sqi_state       = SQI_OFF;
    s_sqi_poll_inflt  = false;
    s_sqi_report_armed = false;
    if (LAN865X_DIAG_Rmw(LAN865X_SQICTL, LAN865X_SQICTL_SQIEN, 0u)) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] monitoring stopped\n\r");
    } else {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] polling stopped, but LAN busy - SQIEN NOT cleared "
                          "(retry 'sqi off' if that matters)\n\r");
    }
}

bool LAN865X_DIAG_SqiActive(void) {
    return (s_sqi_state != SQI_OFF);
}

/* Drives the SQI setup sequence and the steady-state poll. Only acts while
 * the shared register slot is idle: on any given pass, a user command
 * (lan_read/write/rmw/testmode/plca_node/plca_stat) that got there first simply
 * wins, and this is tried again on the next call - there is no starvation
 * because SQI's own operations are equally quick and this runs every
 * main-loop pass. */
static void sqi_tasks(void) {
    if (s_state != LAN_IDLE) {
        return;
    }

    switch (s_sqi_state) {
        case SQI_OFF:
            break;

        case SQI_SET_TOID:
            (void)LAN865X_DIAG_Rmw(LAN865X_SQICFG0, LAN865X_SQICFG0_TOID_MASK,
                                    ((uint32_t)s_sqi_toid << LAN865X_SQICFG0_TOID_SHIFT)
                                        & LAN865X_SQICFG0_TOID_MASK);
            s_sqi_state = SQI_SET_POLLMODE;
            break;

        case SQI_SET_POLLMODE:
            (void)LAN865X_DIAG_Rmw(LAN865X_SQICFG2, LAN865X_SQICFG2_SQIINTTHR_MASK,
                                    LAN865X_SQICFG2_SQIINTTHR_POLL);
            s_sqi_state = SQI_ARM;
            break;

        case SQI_ARM:
            (void)LAN865X_DIAG_Rmw(LAN865X_SQICTL, LAN865X_SQICTL_SQIEN, LAN865X_SQICTL_SQIEN);
            s_sqi_state     = SQI_RUNNING;
            s_sqi_next_poll = SYS_TIME_Counter64Get();   /* poll right away */
            if (s_sqi_toid == LAN865X_SQI_TOID_ALL) {
                CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] monitoring all nodes (weighted) - 'sqi' shows the report\n\r");
            } else {
                CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[SQI] monitoring node %u - 'sqi' shows the report\n\r",
                                  (unsigned int)s_sqi_toid);
            }
            break;

        case SQI_RUNNING: {
            uint64_t now = SYS_TIME_Counter64Get();
            if ((int64_t)(now - s_sqi_next_poll) >= 0) {
                s_sqi_next_poll  = now + (uint64_t)SQI_POLL_MS * s_ticks_per_ms;
                s_sqi_poll_inflt = true;
                if (!LAN865X_DIAG_Read(LAN865X_SQISTS0)) {
                    s_sqi_poll_inflt = false;   /* can't happen given the guard above, stay honest */
                }
            }
            break;
        }

        case SQI_RECOVER_CLEAR:
            (void)LAN865X_DIAG_Rmw(LAN865X_SQICTL, LAN865X_SQICTL_SQIEN, 0u);
            s_sqi_state = SQI_RECOVER_SET;
            break;

        case SQI_RECOVER_SET:
            (void)LAN865X_DIAG_Rmw(LAN865X_SQICTL, LAN865X_SQICTL_SQIEN, LAN865X_SQICTL_SQIEN);
            s_sqi_state = SQI_RUNNING;
            break;

        default:
            break;
    }
}

// *****************************************************************************
// Section: State machine service
// *****************************************************************************

void LAN865X_DIAG_Tasks(void) {
    if (s_ticks_per_ms == 0u) {
        s_ticks_per_ms = (uint64_t)SYS_TIME_FrequencyGet() / 1000ULL;
    }

    switch (s_state) {
        case LAN_IDLE:
            break;

        case LAN_WAIT_READ:
            if (!s_op_complete) {
                if (!s_op_initiated) {
                    TCPIP_MAC_RES result = DRV_LAN865X_ReadRegister(0, s_addr, true, lan_read_callback, NULL);
                    if (result != TCPIP_MAC_RES_OK) {
                        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X Read failed to start: result=%d\n\r", result);
                        lan_abort();
                    } else {
                        s_expire_tick = SYS_TIME_Counter64Get() + (uint64_t)LAN_TIMEOUT_MS * s_ticks_per_ms;
                        s_op_initiated = true;
                    }
                } else {
                    if ((int64_t)(SYS_TIME_Counter64Get() - s_expire_tick) >= 0) {
                        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X Read timeout for addr=0x%08X\n\r", (unsigned int)s_addr);
                        lan_abort();
                    }
                }
            } else if (s_sqi_poll_inflt) {
                /* Our own background SQI poll, not a user command - stay silent,
                 * the 'sqi' command reports the accumulated state on demand. */
                s_sqi_poll_inflt = false;
                if (s_op_success) {
                    sqi_record_sample(s_read_value);
                }
                s_state = LAN_IDLE;
                s_op_initiated = false;
            } else if (s_plcastat_active) {
                if (s_op_success) {
                    s_plcastat_val[s_plcastat_idx] = s_read_value;
                    s_plcastat_idx++;
                    if (s_plcastat_idx < PLCASTAT_STEPS) {
                        s_addr         = s_plcastat_addr[s_plcastat_idx];
                        s_op_complete  = false;
                        s_op_initiated = false;
                        /* state stays LAN_WAIT_READ - next Tasks() call issues this read */
                    } else {
                        s_plcastat_active = false;
                        s_op_initiated    = false;
                        s_state           = LAN_IDLE;
                        lan_plcastat_report();
                    }
                } else {
                    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[PLCA] stat read failed at addr=0x%08X - aborting\n\r",
                                      (unsigned int)s_addr);
                    s_plcastat_active = false;
                    s_op_initiated    = false;
                    s_state           = LAN_IDLE;
                }
            } else {
                if (s_op_success) {
                    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X Read OK: Addr=0x%08X Value=0x%08X\n\r",
                                      (unsigned int)s_addr, (unsigned int)s_read_value);
                    if (s_verify_pending) {
                        uint32_t got  = s_read_value    & s_verify_mask;
                        uint32_t want = s_verify_expect & s_verify_mask;
                        if (got == want) {
                            s_verify_result = LAN865X_VERIFY_PASS;
                            CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[VERIFY] PASS addr=0x%08X masked=0x%08X (mask 0x%08X)\n\r",
                                              (unsigned int)s_addr, (unsigned int)got,
                                              (unsigned int)s_verify_mask);
                        } else {
                            s_verify_result = LAN865X_VERIFY_FAIL;
                            CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[VERIFY] FAIL addr=0x%08X expected=0x%08X got=0x%08X (mask 0x%08X)\n\r",
                                              (unsigned int)s_addr, (unsigned int)want,
                                              (unsigned int)got, (unsigned int)s_verify_mask);
                        }
                        s_verify_pending = false;
                    }
                    /* Decode the test mode for any read of T1STSTCTL, including a
                     * bare 'lan_read 0x000308FB' or the 'testmode' query. */
                    if (s_addr == LAN865X_T1STSTCTL) {
                        uint32_t m = (s_read_value & LAN865X_T1STSTCTL_MASK) >> 13u;
                        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[TESTMODE] now %u - %s\n\r",
                                          (unsigned int)m, LAN865X_DIAG_TestModeName(m));
                    }
                } else {
                    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X Read failed for addr=0x%08X\n\r", (unsigned int)s_addr);
                    if (s_verify_pending) {
                        s_verify_result = LAN865X_VERIFY_FAIL;
                        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "[VERIFY] FAIL addr=0x%08X - readback did not complete\n\r",
                                          (unsigned int)s_addr);
                        s_verify_pending = false;
                    }
                }
                s_state = LAN_IDLE;
                s_op_initiated = false;
            }
            break;

        case LAN_WAIT_WRITE:
            if (!s_op_complete) {
                if (!s_op_initiated) {
                    TCPIP_MAC_RES result = DRV_LAN865X_WriteRegister(0, s_addr, s_value, true, lan_write_callback, NULL);
                    if (result != TCPIP_MAC_RES_OK) {
                        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X Write failed to start: result=%d\n\r", result);
                        lan_abort();
                    } else {
                        s_expire_tick = SYS_TIME_Counter64Get() + (uint64_t)LAN_TIMEOUT_MS * s_ticks_per_ms;
                        s_op_initiated = true;
                    }
                } else {
                    if ((int64_t)(SYS_TIME_Counter64Get() - s_expire_tick) >= 0) {
                        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X Write timeout for addr=0x%08X\n\r", (unsigned int)s_addr);
                        lan_abort();
                    }
                }
            } else {
                if (s_op_success) {
                    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X Write OK: Addr=0x%08X Value=0x%08X\n\r",
                                      (unsigned int)s_addr, (unsigned int)s_value);
                } else {
                    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X Write failed for addr=0x%08X\n\r", (unsigned int)s_addr);
                    s_verify_armed = false;   /* nothing worth reading back */
                }
                s_op_initiated = false;
                if (s_verify_armed) {
                    /* Chain straight into the readback of the same address. */
                    s_verify_armed   = false;
                    s_verify_pending = true;
                    s_op_complete    = false;
                    s_state          = LAN_WAIT_READ;
                } else {
                    s_state = LAN_IDLE;
                }
            }
            break;

        case LAN_WAIT_RMW:
            if (!s_op_complete) {
                if (!s_op_initiated) {
                    TCPIP_MAC_RES result = DRV_LAN865X_ReadModifyWriteRegister(0, s_addr, s_value,
                                                                               s_mask, true,
                                                                               lan_rmw_callback, NULL);
                    if (result != TCPIP_MAC_RES_OK) {
                        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X RMW failed to start: result=%d\n\r", result);
                        lan_abort();
                    } else {
                        s_expire_tick = SYS_TIME_Counter64Get() + (uint64_t)LAN_TIMEOUT_MS * s_ticks_per_ms;
                        s_op_initiated = true;
                    }
                } else {
                    if ((int64_t)(SYS_TIME_Counter64Get() - s_expire_tick) >= 0) {
                        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X RMW timeout for addr=0x%08X\n\r", (unsigned int)s_addr);
                        lan_abort();
                    }
                }
            } else {
                if (s_op_success) {
                    /* s_rmw_final comes from lan_rmw_callback: the word the driver
                     * actually wrote back, not the previous read value. */
                    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X RMW OK: Addr=0x%08X Mask=0x%08X Value=0x%08X Final=0x%08X\n\r",
                                      (unsigned int)s_addr, (unsigned int)s_mask,
                                      (unsigned int)s_value, (unsigned int)s_rmw_final);
                } else {
                    CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "LAN865X RMW failed for addr=0x%08X\n\r", (unsigned int)s_addr);
                    s_verify_armed = false;
                }
                s_op_initiated = false;
                if (s_verify_armed) {
                    s_verify_armed   = false;
                    s_verify_pending = true;
                    s_op_complete    = false;
                    s_state          = LAN_WAIT_READ;
                } else if (s_plcastat_start_pending) {
                    /* Counter-enable RMW done - kick off the plca_stat read sequence. */
                    s_plcastat_start_pending = false;
                    s_plcastat_active        = true;
                    s_plcastat_idx           = 0u;
                    s_addr                   = s_plcastat_addr[0];
                    s_op_complete            = false;
                    s_state                  = LAN_WAIT_READ;
                } else {
                    s_state = LAN_IDLE;
                }
            }
            break;

        default:
            break;
    }

    /* Auto-revert of a test mode, if 'testmode <n> <seconds>' armed one. Only fires
     * while the register machine is free, so it never collides with a pending op. */
    if (s_revert_armed && (s_state == LAN_IDLE) &&
        ((int64_t)(SYS_TIME_Counter64Get() - s_revert_tick) >= 0)) {
        s_revert_armed = false;
        /* Fires up to 600s after the command that armed it - whichever
         * session that was may be long gone, so this (and the write
         * completion it triggers below) goes to the serial console. */
        s_diag_pCmdIO = NULL;
        SYS_CONSOLE_PRINT("[TESTMODE] auto-revert: restoring normal operation\n\r");
        s_addr           = LAN865X_T1STSTCTL;
        s_value          = 0u;
        s_verify_expect  = 0u;
        s_verify_mask    = LAN865X_T1STSTCTL_MASK;
        s_verify_armed   = true;
        s_verify_pending = false;
        s_op_complete    = false;
        s_op_initiated   = false;
        s_state          = LAN_WAIT_WRITE;
    }

    sqi_tasks();

    /* Periodic auto-report, armed by 'sqi report <seconds>'. Only reads
     * already-accumulated state, so it runs on its own schedule regardless
     * of the register slot. */
    if (s_sqi_report_armed) {
        uint64_t now = SYS_TIME_Counter64Get();
        if ((int64_t)(now - s_sqi_report_next) >= 0) {
            s_sqi_report_next = now + s_sqi_report_period_ticks;
            sqi_print_report();
        }
    }
}

// *****************************************************************************
// Section: Console commands
// *****************************************************************************

static const char *BUSY_MSG = "ERROR: Previous LAN operation still in progress\n\r";

/* LAN865X Register read command */
static void cmd_lan_read(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc != 2) {
        CMD_PRINT(pCmdIO, "Usage: lan_read <address_hex>\n\r");
        CMD_PRINT(pCmdIO, "Example: lan_read 0x00040000\n\r");
        return;
    }
    s_diag_pCmdIO = pCmdIO;
    if (!LAN865X_DIAG_Read((uint32_t)strtoul(argv[1], NULL, 0))) {
        CMD_PRINT(pCmdIO, "%s", BUSY_MSG);
    }
}

/* LAN865X Register write command */
static void cmd_lan_write(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc != 3) {
        CMD_PRINT(pCmdIO, "Usage: lan_write <address_hex> <value_hex>\n\r");
        CMD_PRINT(pCmdIO, "Example: lan_write 0x00040000 0x12345678\n\r");
        return;
    }
    s_diag_pCmdIO = pCmdIO;
    if (!LAN865X_DIAG_Write((uint32_t)strtoul(argv[1], NULL, 0),
                            (uint32_t)strtoul(argv[2], NULL, 0))) {
        CMD_PRINT(pCmdIO, "%s", BUSY_MSG);
    }
}

/* lan_rmw <addr> <mask> <value> - read-modify-write a single register, then verify.
 * Driver semantics (tc6.c): new = (old & ~mask) | value. Note that 'value' is NOT
 * masked by the driver, so bits outside the mask are set unconditionally - hence the
 * warning below. Exists so that single bits in registers such as T1SPMACTL can be
 * changed without the host having to read, combine and write the whole word. */
static void cmd_lan_rmw(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc != 4) {
        CMD_PRINT(pCmdIO, "Usage: lan_rmw <addr_hex> <mask_hex> <value_hex>\n\r");
        CMD_PRINT(pCmdIO, "       new = (old & ~mask) | value\n\r");
        CMD_PRINT(pCmdIO, "Example: lan_rmw 0x000308F9 0x00000001 0x00000001   (set LBE)\n\r");
        CMD_PRINT(pCmdIO, "Example: lan_rmw 0x000308F9 0x00000001 0x00000000   (clear LBE)\n\r");
        return;
    }

    if (LAN865X_DIAG_Busy()) {
        CMD_PRINT(pCmdIO, "%s", BUSY_MSG);
        return;
    }

    uint32_t addr  = (uint32_t)strtoul(argv[1], NULL, 0);
    uint32_t mask  = (uint32_t)strtoul(argv[2], NULL, 0);
    uint32_t value = (uint32_t)strtoul(argv[3], NULL, 0);

    if (mask == 0u) {
        CMD_PRINT(pCmdIO, "lan_rmw: mask is 0 - nothing would change, use lan_write instead\n\r");
        return;
    }
    if ((value & ~mask) != 0u) {
        CMD_PRINT(pCmdIO, "lan_rmw: WARNING value has bits outside the mask (0x%08X); "
                          "the driver ORs them in regardless\n\r",
                          (unsigned int)(value & ~mask));
    }

    s_diag_pCmdIO = pCmdIO;
    (void)LAN865X_DIAG_Rmw(addr, mask, value);
}

/* testmode [0..4] [seconds] - select an IEEE 802.3-2022 §147.5.2 transmitter test mode.
 * Without arguments it only reads T1STSTCTL back and reports the decoded current mode.
 * Modes 1..4 stop normal traffic by design, so the bridged link goes down; the optional
 * timeout restores normal operation on its own, which keeps a forgotten test mode from
 * looking like a broken board later. */
static void cmd_testmode(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (LAN865X_DIAG_Busy()) {
        CMD_PRINT(pCmdIO, "%s", BUSY_MSG);
        return;
    }

    if (argc < 2) {
        /* Query only - the read path decodes T1STSTCTL by itself. */
        s_diag_pCmdIO = pCmdIO;
        (void)LAN865X_DIAG_Read(LAN865X_T1STSTCTL);
        return;
    }

    uint32_t mode = (uint32_t)strtoul(argv[1], NULL, 0);
    if (mode > LAN865X_TESTMODE_MAX) {
        CMD_PRINT(pCmdIO, "Usage: testmode [0..%u] [seconds]\n\r", (unsigned int)LAN865X_TESTMODE_MAX);
        CMD_PRINT(pCmdIO, "  0 = normal, 1 = voltage/jitter, 2 = droop, 3 = PSD mask, 4 = TX high-Z\n\r");
        CMD_PRINT(pCmdIO, "  no argument = show the current mode\n\r");
        return;
    }

    uint32_t secs = 0u;
    if (argc >= 3) {
        secs = (uint32_t)strtoul(argv[2], NULL, 0);
        if ((secs < 1u) || (secs > 600u)) {
            CMD_PRINT(pCmdIO, "testmode: seconds must be 1..600\n\r");
            return;
        }
    }

    s_diag_pCmdIO = pCmdIO;
    (void)LAN865X_DIAG_TestMode(mode, secs);
}

/* plca_stat - PLCA bus health below the IP-frame level: link range, configured
 * segment size, transmit-opportunity/BEACON counts since the last call, and any
 * sticky PLCA event flags. BEACON/COMMIT control symbols never reach 'mirror'/
 * 'sniffer' - those tap the RX frame path, and PLCA signaling sits below it - so
 * this is the register-level substitute for a bus analyzer or oscilloscope. */
static void cmd_plca_stat(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv) {
    if (LAN865X_DIAG_Busy()) {
        CMD_PRINT(pCmdIO, "%s", BUSY_MSG);
        return;
    }
    s_diag_pCmdIO = pCmdIO;
    (void)LAN865X_DIAG_PlcaStat();
}

static void cmd_plca_node(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv) {
    if (argc < 2) {
        /* No parameter: show the node id and node count last applied */
        CMD_PRINT(pCmdIO, "[PLCA] current node ID: %u (NODE_CNT=%u)\r\n",
                          (unsigned)s_plca_node_id, (unsigned)s_plca_node_cnt);
        return;
    }
    /* Live override (not persisted - use 'setenv plca_id'/'saveenv' for that). The
     * node count is the one last applied, so the two stay consistent.
     * LAN865X_DIAG_ApplyPlca()'s own confirmation print stays on the serial
     * console regardless of pCmdIO - see that function's comment (it is also
     * called from env.c's boot-time env_apply(), which has no pCmdIO of its
     * own to offer, and there is no reliable way here to tell those two
     * callers apart on the far side of that shared function). */
    LAN865X_DIAG_ApplyPlca((uint8_t)strtoul(argv[1], NULL, 0), s_plca_node_cnt);
}

/* sqi [<node>|all|off|report <seconds>|report off] - continuous PLCA transmit-
 * opportunity signal quality monitoring (datasheet DS60001734F 7.5). SQI is
 * not a single register value: the PHY accumulates it from live traffic over
 * a duration that depends on how much traffic is flowing, so this starts a
 * background poll (~1x/s) that keeps the accumulation running and folds every
 * valid sample into a running min/max/count; a bare 'sqi' shows that
 * accumulated report without disturbing it - it does not itself trigger a new
 * register access. 'sqi report <seconds>' prints that same report on its own
 * schedule instead of only on demand (starts monitoring on all nodes first if
 * nothing is running yet); 'sqi report off' stops the auto-print without
 * necessarily stopping the monitoring itself - 'sqi off' stops both. */
static void cmd_sqi(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    s_diag_pCmdIO = pCmdIO;   /* also the target for any periodic 'sqi report N' auto-print armed below */

    if (argc < 2) {
        sqi_print_report();
        return;
    }

    if (strcmp(argv[1], "off") == 0) {
        LAN865X_DIAG_SqiStop();
        return;
    }

    if (strcmp(argv[1], "report") == 0) {
        if (argc < 3) {
            CMD_PRINT(pCmdIO, "Usage: sqi report <seconds 1..3600>|off\n\r");
            return;
        }
        if (strcmp(argv[2], "off") == 0) {
            if (s_sqi_report_armed) {
                s_sqi_report_armed = false;
                CMD_PRINT(pCmdIO, "[SQI] auto-report stopped\n\r");
            } else {
                CMD_PRINT(pCmdIO, "[SQI] auto-report already stopped\n\r");
            }
            return;
        }
        uint32_t secs = (uint32_t)strtoul(argv[2], NULL, 0);
        if ((secs < 1u) || (secs > 3600u)) {
            CMD_PRINT(pCmdIO, "sqi report: seconds must be 1..3600\n\r");
            return;
        }
        if (!LAN865X_DIAG_SqiActive()) {
            /* Convenience: 'sqi report 5' alone should not just print
             * "not monitoring" every 5 s - start monitoring on all nodes. */
            LAN865X_DIAG_SqiStart(LAN865X_SQI_TOID_ALL);
        }
        s_sqi_report_period_ticks = (uint64_t)secs * (uint64_t)SYS_TIME_FrequencyGet();
        s_sqi_report_next         = SYS_TIME_Counter64Get();   /* first report right away */
        s_sqi_report_armed        = true;
        CMD_PRINT(pCmdIO, "[SQI] auto-report every %u s armed\n\r", (unsigned int)secs);
        return;
    }

    uint32_t toid;
    if (strcmp(argv[1], "all") == 0) {
        toid = LAN865X_SQI_TOID_ALL;
    } else {
        toid = strtoul(argv[1], NULL, 0);
        if (toid > 0xFEu) {
            CMD_PRINT(pCmdIO, "Usage: sqi [<node 0..254>|all|off]\n\r");
            CMD_PRINT(pCmdIO, "  no argument = show the accumulated report\n\r");
            return;
        }
    }

    LAN865X_DIAG_SqiStart((uint8_t)toid);
    CMD_PRINT(pCmdIO, "[SQI] starting - poll ~1x/s, give it a few seconds to accumulate\n\r");
}

static void cmd_lan_help(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    CMD_PRINT(pCmdIO, "LAN865x diagnostics commands:\n\r");
    CMD_PRINT(pCmdIO, "  lan_read  <addr>             - Read  LAN865X register (hex address)\n\r");
    CMD_PRINT(pCmdIO, "  lan_write <addr> <value>     - Write LAN865X register (hex addr, hex value)\n\r");
    CMD_PRINT(pCmdIO, "  lan_rmw <addr> <mask> <val>  - Read-modify-write + verify: new=(old&~mask)|val\n\r");
    CMD_PRINT(pCmdIO, "  testmode [0..4] [seconds]    - IEEE TX test mode, verified by readback (no arg = show)\n\r");
    CMD_PRINT(pCmdIO, "  plca_node [id]               - Get/set PLCA node ID (no arg = show current)\n\r");
    CMD_PRINT(pCmdIO, "  plca_stat                    - PLCA bus health below IP-frame level (link/nodes/TO+BEACON counts/events)\n\r");
    CMD_PRINT(pCmdIO, "  sqi [node|all|off]           - Signal Quality Indicator, continuous (no arg = show report)\n\r");
    CMD_PRINT(pCmdIO, "  sqi report <sec>|off         - auto-print the sqi report every <sec> s, or stop doing so\n\r");
    CMD_PRINT(pCmdIO, "\n\rAddress = (MMS << 16) | offset. MMS 3 = PHY PMA/PMD, MMS 4 = vendor specific.\n\r");
    CMD_PRINT(pCmdIO, "Example: lan_read 0x0004CA02   (PLCA_CTRL1: NODE_CNT<<8 | NODE_ID)\n\r");
    CMD_PRINT(pCmdIO, "Example: testmode 1 30         (test mode 1, auto-revert after 30 s)\n\r");
    CMD_PRINT(pCmdIO, "Example: sqi all               (monitor all PLCA nodes, weighted average)\n\r");
    CMD_PRINT(pCmdIO, "Example: sqi report 5          (print the report every 5 s, starts monitoring if needed)\n\r");
}

static const SYS_CMD_DESCRIPTOR lan_cmd_tbl[] = {
    {"lanhelp",   (SYS_CMD_FNC) cmd_lan_help,   ": list the LAN865x diagnostics commands"},
    {"lan_read",  (SYS_CMD_FNC) cmd_lan_read,   ": read LAN865X register (lan_read <addr_hex>)"},
    {"lan_write", (SYS_CMD_FNC) cmd_lan_write,  ": write LAN865X register (lan_write <addr_hex> <value_hex>)"},
    {"lan_rmw",   (SYS_CMD_FNC) cmd_lan_rmw,    ": read-modify-write + verify (lan_rmw <addr> <mask> <value>)"},
    {"testmode",  (SYS_CMD_FNC) cmd_testmode,   ": IEEE transmitter test mode (testmode [0..4] [seconds], no arg = show)"},
    {"plca_node", (SYS_CMD_FNC) cmd_plca_node,  ": get/set PLCA node ID (plca_node [id], no arg: show current)"},
    {"plca_stat", (SYS_CMD_FNC) cmd_plca_stat,  ": PLCA bus health below IP-frame level (link/nodes/TO+BEACON counts/events)"},
    {"sqi",       (SYS_CMD_FNC) cmd_sqi,        ": continuous Signal Quality Indicator (sqi [node|all|off], sqi report <sec>|off, no arg: show report)"},
};

void LAN865X_DIAG_Initialize(void) {
    if (!SYS_CMD_ADDGRP(lan_cmd_tbl, (int)(sizeof lan_cmd_tbl / sizeof *lan_cmd_tbl),
                        "lan", ": LAN865x registers, test modes, PLCA")) {
        SYS_CONSOLE_PRINT("LAN865X_DIAG: SYS_CMD_ADDGRP failed\n\r");
    }
}
