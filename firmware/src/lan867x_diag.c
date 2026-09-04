/*******************************************************************************
  eth1 (LAN867x) MDIO register access - the LAN867x counterpart to
  lan865x_diag.c's lan_read/lan_write.

  Description:
    One MDIO client, one register operation slot - the same design as
    lan865x_diag.c (LAN867X_DIAG_Busy() rejects a second request outright),
    just driving MDIO/MIIM instead of the LAN865x's SPI register API.

    The actual clause-22/clause-45 protocol handling (which addresses need
    the multi-phase MMD sequence, resuming that sequence one MIIM transaction
    per call) is not reimplemented here - it already exists in the LAN867x
    PHY driver itself (LAN867x_Read_Register/LAN867x_Write_Register,
    drv_extphy_lan867x.c/h), which this module calls once per
    LAN867X_DIAG_Tasks() pass, exactly the way the driver's own internal
    init/detect state machine does. See LAN867X_REG_OBJ.vendorData for how a
    call in progress is resumed: IDLE_PHASE (0) starts a new operation, any
    other value means "keep going" and Set_Operation_Flow() is not
    re-evaluated.

    A second MIIM client, alongside the one the ETHPHY driver keeps open for
    itself and the one app.c's boot banner opens - DRV_MIIM_INSTANCE_CLIENTS
    was raised from 2 to 3 in configuration.h for this.
 *******************************************************************************/

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>                                          /* strtoul() */

#include "definitions.h"
#include "configuration.h"                                   /* DRV_LAN867x_PHY_ADDRESS */
#include "config/default/system/console/sys_console.h"
#include "config/default/system/time/sys_time.h"
#include "config/default/driver/miim/drv_miim.h"
#include "config/default/driver/ethphy/src/dynamic/drv_extphy_lan867x.h"
#include "system/command/sys_command.h"
#include "lan867x_diag.h"
#include "cmd_print.h"                                        /* CMD_PRINT/CMD_PRINT_OR_CONSOLE - reply to pCmdIO, not always the serial console */

// *****************************************************************************
// Section: Module state
// *****************************************************************************

#define ETH1_TIMEOUT_MS  200u    /* generous: a clause-45 op is at most ~5 MDIO transactions, one per Tasks() pass */

typedef enum {
    ETH1_IDLE,
    ETH1_WAIT_READ,
    ETH1_WAIT_WRITE
} eth1_state_t;

static eth1_state_t s_state        = ETH1_IDLE;
static uint32_t     s_addr         = 0u;
static uint16_t     s_wdata        = 0u;
static uint16_t     s_rdata        = 0u;
static uint64_t     s_expire_tick  = 0u;
static bool         s_op_started   = false;   /* first Tasks() pass for the current op: (re)arm the timeout, clear vendorData */
static uint64_t     s_ticks_per_ms = 0u;

/* Who to send the eventual result to - see lan865x_diag.c's s_diag_pCmdIO for
 * why this exists: LAN867X_DIAG_Tasks() completes the operation well after
 * the command handler that started it has already returned. NULL falls back
 * to the serial console (CMD_PRINT_OR_CONSOLE). */
static SYS_CMD_DEVICE_NODE *s_diag_pCmdIO = NULL;

/* Lazily-opened second MIIM client (the ETHPHY driver keeps its own), plus
 * the persistent LAN867X_REG_OBJ the driver's Read/Write_Register helpers
 * resume their multi-phase sequence through across calls. */
static DRV_HANDLE               s_miim_handle = DRV_HANDLE_INVALID;
static DRV_MIIM_OPERATION_HANDLE s_miim_op    = 0;
static LAN867X_REG_OBJ           s_client;

// *****************************************************************************
// Section: Helpers
// *****************************************************************************

bool LAN867X_DIAG_Busy(void) {
    return (s_state != ETH1_IDLE);
}

/* Opens the second MIIM client on first use and (re)binds s_client to it.
 * Returns false if the client could not be opened - MIIM has no free slot
 * (DRV_MIIM_INSTANCE_CLIENTS) or the driver isn't ready yet. */
static bool eth1_client_ready(void) {
    if (s_miim_handle == DRV_HANDLE_INVALID) {
        s_miim_handle = DRV_MIIM_Open(DRV_MIIM_INDEX_0, DRV_IO_INTENT_SHARED);
        if (s_miim_handle == DRV_HANDLE_INVALID) {
            return false;
        }
        s_client.miimBase    = &DRV_MIIM_OBJECT_BASE_Default;
        s_client.miimHandle  = s_miim_handle;
        s_client.miimOpHandle = &s_miim_op;
        s_client.phyAddress  = DRV_LAN867x_PHY_ADDRESS;
    }
    return true;
}

// *****************************************************************************
// Section: Programmatic API
// *****************************************************************************

bool LAN867X_DIAG_Read(uint32_t addr) {
    if (s_state != ETH1_IDLE) {
        return false;
    }
    s_addr       = addr;
    s_op_started = false;
    s_state      = ETH1_WAIT_READ;
    return true;
}

bool LAN867X_DIAG_Write(uint32_t addr, uint16_t value) {
    if (s_state != ETH1_IDLE) {
        return false;
    }
    s_addr       = addr;
    s_wdata      = value;
    s_op_started = false;
    s_state      = ETH1_WAIT_WRITE;
    return true;
}

// *****************************************************************************
// Section: State machine service
// *****************************************************************************

void LAN867X_DIAG_Tasks(void) {
    if (s_ticks_per_ms == 0u) {
        s_ticks_per_ms = (uint64_t)SYS_TIME_FrequencyGet() / 1000ULL;
    }
    if (s_state == ETH1_IDLE) {
        return;
    }

    if (!eth1_client_ready()) {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "eth1 (LAN867x): MIIM client not available (retry later)\n\r");
        s_state = ETH1_IDLE;
        return;
    }

    if (!s_op_started) {
        s_client.vendorData = 0u;   /* IDLE_PHASE - (re)start the clause-22/45 sequence from scratch */
        s_expire_tick = SYS_TIME_Counter64Get() + (uint64_t)ETH1_TIMEOUT_MS * s_ticks_per_ms;
        s_op_started  = true;
    }

    DRV_MIIM_RESULT res;
    if (s_state == ETH1_WAIT_READ) {
        res = LAN867x_Read_Register(&s_client, s_addr, &s_rdata);
    } else {
        res = LAN867x_Write_Register(&s_client, s_addr, s_wdata);
    }

    if (res == DRV_MIIM_RES_PENDING) {
        if ((int64_t)(SYS_TIME_Counter64Get() - s_expire_tick) >= 0) {
            CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "eth1 (LAN867x) %s timeout for addr=0x%08X\n\r",
                              (s_state == ETH1_WAIT_READ) ? "Read" : "Write", (unsigned int)s_addr);
            s_state = ETH1_IDLE;
        }
        return;   /* keep waiting - called again next Tasks() pass */
    }

    if (res == DRV_MIIM_RES_OK) {
        if (s_state == ETH1_WAIT_READ) {
            CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "eth1 (LAN867x) Read OK: Addr=0x%08X Value=0x%04X\n\r",
                              (unsigned int)s_addr, (unsigned int)s_rdata);
        } else {
            CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "eth1 (LAN867x) Write OK: Addr=0x%08X Value=0x%04X\n\r",
                              (unsigned int)s_addr, (unsigned int)s_wdata);
        }
    } else {
        CMD_PRINT_OR_CONSOLE(s_diag_pCmdIO, "eth1 (LAN867x) %s failed for addr=0x%08X (result=%d)\n\r",
                          (s_state == ETH1_WAIT_READ) ? "Read" : "Write", (unsigned int)s_addr, (int)res);
    }
    s_state = ETH1_IDLE;
}

// *****************************************************************************
// Section: Console commands
// *****************************************************************************

static const char *BUSY_MSG = "ERROR: Previous eth1 (LAN867x) operation still in progress\n\r";

static void cmd_eth1_read(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc != 2) {
        CMD_PRINT(pCmdIO, "Usage: eth1_read <addr_hex>\n\r");
        CMD_PRINT(pCmdIO, "Example: eth1_read 0x18   (clause-22 STS1)\n\r");
        return;
    }
    s_diag_pCmdIO = pCmdIO;
    if (!LAN867X_DIAG_Read((uint32_t)strtoul(argv[1], NULL, 0))) {
        CMD_PRINT(pCmdIO, "%s", BUSY_MSG);
    }
}

static void cmd_eth1_write(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc != 3) {
        CMD_PRINT(pCmdIO, "Usage: eth1_write <addr_hex> <value_hex>\n\r");
        CMD_PRINT(pCmdIO, "Example: eth1_write 0x1FCA02 0x0800   (PLCA_CTRL1: NODE_CNT=8, NODE_ID=0)\n\r");
        return;
    }
    uint32_t value = (uint32_t)strtoul(argv[2], NULL, 0);
    if (value > 0xFFFFu) {
        CMD_PRINT(pCmdIO, "eth1_write: value 0x%08X does not fit a 16-bit MDIO register\n\r", (unsigned int)value);
        return;
    }
    s_diag_pCmdIO = pCmdIO;
    if (!LAN867X_DIAG_Write((uint32_t)strtoul(argv[1], NULL, 0), (uint16_t)value)) {
        CMD_PRINT(pCmdIO, "%s", BUSY_MSG);
    }
}

static void cmd_eth1_help(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    CMD_PRINT(pCmdIO, "eth1 (LAN867x) MDIO register commands:\n\r");
    CMD_PRINT(pCmdIO, "  eth1_read  <addr>            - Read  eth1 LAN867x register over MDIO (hex address)\n\r");
    CMD_PRINT(pCmdIO, "  eth1_write <addr> <value>    - Write eth1 LAN867x register over MDIO (hex addr, 16-bit hex value)\n\r");
    CMD_PRINT(pCmdIO, "\n\rAddress < 0x20 = clause-22 register (0..31). Address >= 0x20 = clause-45,\n\r");
    CMD_PRINT(pCmdIO, "packed as (DEVAD << 16) | register - see drv_extphy_lan867x.h ePHY_VENDOR_REG.\n\r");
    CMD_PRINT(pCmdIO, "Example: eth1_read 0x02        (PHYID1)\n\r");
    CMD_PRINT(pCmdIO, "Example: eth1_read 0x1FCA02    (PLCA_CTRL1: NODE_CNT<<8 | NODE_ID)\n\r");
    CMD_PRINT(pCmdIO, "Example: eth1_read 0x1FCA03    (PLCA_STS: bit15 = PST, in range)\n\r");
}

static const SYS_CMD_DESCRIPTOR eth1_cmd_tbl[] = {
    {"eth1help",  (SYS_CMD_FNC) cmd_eth1_help,  ": list the eth1 (LAN867x) MDIO register commands"},
    {"eth1_read", (SYS_CMD_FNC) cmd_eth1_read,  ": read eth1 LAN867x register over MDIO (eth1_read <addr_hex>)"},
    {"eth1_write",(SYS_CMD_FNC) cmd_eth1_write, ": write eth1 LAN867x register over MDIO (eth1_write <addr_hex> <value_hex>)"},
};

void LAN867X_DIAG_Initialize(void) {
    if (!SYS_CMD_ADDGRP(eth1_cmd_tbl, (int)(sizeof eth1_cmd_tbl / sizeof *eth1_cmd_tbl),
                        "eth1", ": eth1 (LAN867x) MDIO registers")) {
        SYS_CONSOLE_PRINT("LAN867X_DIAG: SYS_CMD_ADDGRP failed\n\r");
    }
}
