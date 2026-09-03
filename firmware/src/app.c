/*******************************************************************************
  MPLAB Harmony Application Source File

  Company:
    Microchip Technology Inc.

  File Name:
    app.c

  Summary:
    This file contains the source code for the MPLAB Harmony application.

  Description:
    This file contains the source code for the MPLAB Harmony application.  It
    implements the logic of the application's state machine and it may call
    API routines of other MPLAB Harmony modules in the system, such as drivers,
    system services, and middleware.  However, it does not call any of the
    system interfaces (such as the "Initialize" and "Tasks" functions) of any of
    the modules in the system or make any assumptions about when those functions
    are called.  That is the responsibility of the configuration-specific system
    files.
 *******************************************************************************/

// *****************************************************************************
// *****************************************************************************
// Section: Included Files
// *****************************************************************************
// *****************************************************************************

#include "app.h"
#include "tcpip/tcpip.h"
#include <stdio.h>              /* snprintf - DumpMem() line formatting */
#include <stdlib.h>             /* strtoul - CLI argument parsing */
#include <string.h>             /* strcmp, memcpy */
#include "system/console/sys_console.h"
#include "system/command/sys_command.h"
#include "system/time/sys_time.h"
#include "config/default/library/tcpip/telnet.h"
#define TCPIP_THIS_MODULE_ID    TCPIP_MODULE_MANAGER
#include "config/default/library/tcpip/src/tcpip_packet.h"
#include "env.h"
#include "lan865x_diag.h"
#include "port_mirror.h"
#include "noip_test.h"
#include "testserver.h"
#include "crashlog.h"
#include "cpuload.h"
#include "leds.h"
#include "cmd_print.h"          /* CMD_PRINT/CMD_MSG - reply to pCmdIO, not always the serial console */
#include "definitions.h"        /* sysObj - only for APP_PumpNetworkStack(), see its comment */
#include "config/default/driver/miim/drv_miim.h"   /* boot banner: read eth1's PHY ID over MDIO */

// *****************************************************************************
// *****************************************************************************
// Section: Global Data Definitions
// *****************************************************************************
// *****************************************************************************

// *****************************************************************************
/* Application Data

  Summary:
    Holds application data

  Description:
    This structure holds the application's data.

  Remarks:
    This structure should be initialized by the APP_Initialize function.

    Application strings and buffers are be defined outside this structure.
*/

APP_DATA appData;

// *****************************************************************************
// *****************************************************************************
// Section: Application Callback Functions
// *****************************************************************************
// *****************************************************************************

/* initialization.c declares TCPIP_STACK_InitCallback as extern and hands its
   address to TCPIP_STACK_Init()->TCPIP_STACK_Initialize(), but never defines
   it; the tcpip_manager expects *ppStackInit to stay valid for the lifetime
   of the stack init, so it must point to storage with static duration. */
extern const TCPIP_NETWORK_CONFIG TCPIP_HOSTS_CONFIGURATION[];
extern const size_t TCPIP_HOSTS_CONFIGURATION_SIZE;
extern const TCPIP_STACK_MODULE_CONFIG TCPIP_STACK_MODULE_CONFIG_TBL[];
extern const size_t TCPIP_STACK_MODULE_CONFIG_TBL_SIZE;

int TCPIP_STACK_InitCallback(const struct TCPIP_STACK_INIT** ppStackInit)
{
    static TCPIP_STACK_INIT s_tcpipStackInit;

    s_tcpipStackInit.pNetConf   = TCPIP_HOSTS_CONFIGURATION;
    s_tcpipStackInit.nNets      = TCPIP_HOSTS_CONFIGURATION_SIZE;
    s_tcpipStackInit.pModConfig = TCPIP_STACK_MODULE_CONFIG_TBL;
    s_tcpipStackInit.nModules   = TCPIP_STACK_MODULE_CONFIG_TBL_SIZE;
    s_tcpipStackInit.initCback  = NULL;

    *ppStackInit = &s_tcpipStackInit;
    return 0;
}

/* Ported from a related in-house project:
   a hardcoded user/password check, registered with the Telnet server so an
   unauthenticated client cannot get a shell on the bridge over the network. */
bool TelnetAuthenticationHandler(const char* user, const char* password, const TCPIP_TELNET_CONN_INFO* pInfo, const void* hParam)
{
    (void)pInfo;
    (void)hParam;

    if ((strcmp(user, "admin") == 0) && (strcmp(password, "password") == 0)) {
        SYS_CONSOLE_PRINT("Telnet Access Authenticated\n\r");
        return true;
    } else {
        SYS_CONSOLE_PRINT("Telnet Access Declined\n\r");
        return false;
    }
}

const void* TelnetHandlerParam;

// *****************************************************************************
// *****************************************************************************
// Section: Application Local Functions
// *****************************************************************************
// *****************************************************************************

bool pktEth0Handler(TCPIP_NET_HANDLE hNet, TCPIP_MAC_PACKET* rxPkt, uint16_t frameType, const void* hParam);
const void *MyEth0HandlerParam;

bool pktEth1Handler(TCPIP_NET_HANDLE hNet, TCPIP_MAC_PACKET* rxPkt, uint16_t frameType, const void* hParam);
const void *MyEth1HandlerParam;

static void DumpMem(uint32_t addr, uint32_t count);
static bool Command_Init(void);

static uint32_t ipdump_mode = 0;
static uint32_t my_delay_time = 0;

static SYS_TIME_HANDLE timerHandle;

/* How fast the cooperative main loop actually spins - ported from a related
 * project alongside 'stats', where it is used to help separate the main-loop
 * cadence from the link/PLCA as a throughput ceiling. Incremented once per
 * APP_STATE_IDLE iteration; BRIDGE_TimerCallback (1 Hz) snapshots the delta
 * into idle_cycles_per_sec, printed by 'stats'. */
static volatile uint32_t s_idle_cycle_count = 0u;
static uint32_t s_idle_cycles_per_sec = 0u;

/* =========================================================
 * Deferred Packet Logging
 * =========================================================
 * Packet handlers store metadata into a ring buffer instead
 * of calling SYS_CONSOLE_PRINT()/DumpMem() directly.
 * APP_Tasks() drains the buffer (max 10 entries per call). */

#define PKT_LOG_BUF_SIZE    64u   /* ring buffer capacity; must be a power of 2 */
/* Full-frame capture: frame stored in shared pool (up to PKT_LOG_MAX_FRAME_SIZE bytes each) */
#define PKT_LOG_MAX_FRAMES     16u    /* number of full-size frames bufferable in pool */
#define PKT_LOG_MAX_FRAME_SIZE 1518u  /* max bytes per frame (standard Ethernet MTU)  */

typedef enum {
    PKT_LOG_NOIP = 0,  /* NoIP (0x88B5) frame from eth0 */
    PKT_LOG_ETH0 = 2,  /* generic frame from eth0        */
    PKT_LOG_ETH1 = 3,  /* generic frame from eth1        */
} pkt_log_type_t;

typedef struct {
    uint64_t       timestamp;    /* SYS_TIME_Counter64Get()                    */
    uint32_t       pkt_counter;  /* per-handler packet counter                 */
    uint32_t       noip_seq;     /* NoIP sequence number                       */
    uint16_t       frame_type;   /* EtherType                                  */
    uint16_t       length;       /* actual frame length in bytes               */
    uint32_t       data_offset;  /* offset into frame_data_pool[]              */
    uint16_t       data_len;     /* bytes stored in pool (may be 0 if dropped) */
    uint8_t        iface;        /* 0 = eth0, 1 = eth1                         */
    uint8_t        truncated;    /* 1 if frame data was truncated to fit pool  */
    pkt_log_type_t log_type;     /* entry classification                       */
    uint8_t        mac_src[6];   /* source MAC (extracted separately)          */
} PKT_LOG_ENTRY;

typedef struct {
    PKT_LOG_ENTRY     entries[PKT_LOG_BUF_SIZE];
    volatile uint32_t write_idx;     /* updated only by packet handlers  */
    volatile uint32_t read_idx;      /* updated only by APP_Tasks        */
    volatile uint32_t overflow_cnt;
    volatile uint32_t total_logged;
} PKT_LOG_BUF;

/* No explicit initializer: static-duration objects are zero-initialized by the
 * C standard regardless, and leaving it implicit lets the compiler place this
 * (and frame_data_pool below) in .bss. */
static PKT_LOG_BUF pkt_log;

/* Shared circular pool for storing complete frame bytes.
 * Holds up to PKT_LOG_MAX_FRAMES full-size Ethernet frames.
 * Aligned to 4 bytes for efficient ARM word-aligned access. */
#define FRAME_DATA_POOL_SIZE  ((uint32_t)PKT_LOG_MAX_FRAMES * (uint32_t)PKT_LOG_MAX_FRAME_SIZE)

typedef struct {
    uint8_t  pool[FRAME_DATA_POOL_SIZE]; /* circular frame data storage           */
    uint32_t write_offset;               /* next write position in pool (0-based) */
} FRAME_DATA_POOL;

/* No explicit initializer - see pkt_log's comment above. */
static FRAME_DATA_POOL frame_data_pool __attribute__((aligned(4)));

/* Lock-free single-producer/single-consumer ring buffer write.
 * On ARM Cortex-M, 32-bit aligned stores are single-instruction atomic.
 * write_idx is committed last so the reader never observes a partial entry.
 * Newest entries are dropped when the buffer is full.
 *
 * frame_data/frame_len provide the complete frame bytes to copy into the
 * shared pool.  The pool write_offset is advanced after the copy.
 * Wraparound safety: if the frame does not fit at the current write_offset
 * the function attempts to wrap to offset 0.  It only wraps if no pending
 * log entry references data in [0, copy_len), otherwise the frame is
 * truncated to the remaining bytes at the end of the pool.
 */
static void PktLog_Write(PKT_LOG_ENTRY *entry,
                         const uint8_t *frame_data, uint16_t frame_len)
{
    uint32_t next = (pkt_log.write_idx + 1u) & (PKT_LOG_BUF_SIZE - 1u);
    if (next == pkt_log.read_idx) {
        pkt_log.overflow_cnt++;
        return; /* ring buffer full - drop newest entry */
    }

    /* Clamp captured length to the maximum supported frame size */
    uint16_t copy_len = (frame_len > (uint16_t)PKT_LOG_MAX_FRAME_SIZE)
                        ? (uint16_t)PKT_LOG_MAX_FRAME_SIZE : frame_len;

    uint32_t pool_offset    = frame_data_pool.write_offset;
    uint8_t  truncated_flag = 0u;

    if (frame_data != NULL && copy_len > 0u) {
        uint32_t remaining = FRAME_DATA_POOL_SIZE - frame_data_pool.write_offset;

        if ((uint32_t)copy_len > remaining) {
            /* Frame does not fit at the current write position.
             * Attempt to wrap to the beginning of the pool.
             * This is safe only when no pending entry holds data in [0, copy_len). */
            bool ring_empty = (pkt_log.read_idx == pkt_log.write_idx);
            bool wrap_safe  = ring_empty ||
                              (pkt_log.entries[pkt_log.read_idx].data_offset >= (uint32_t)copy_len);

            if (wrap_safe) {
                /* Wrap: restart from pool beginning */
                pool_offset = 0u;
            } else {
                /* Cannot wrap safely - truncate to whatever space remains */
                copy_len       = (uint16_t)remaining;
                truncated_flag = 1u;
            }
        }

        if (copy_len > 0u) {
            memcpy(&frame_data_pool.pool[pool_offset], frame_data, copy_len);
            /* Advance the pool write pointer; reset to 0 if we exactly filled the end */
            uint32_t new_offset = pool_offset + (uint32_t)copy_len;
            frame_data_pool.write_offset = (new_offset >= FRAME_DATA_POOL_SIZE) ? 0u : new_offset;
        }
    }

    /* Store pool reference and flags in the ring entry */
    entry->data_offset = pool_offset;
    entry->data_len    = copy_len;
    entry->truncated   = truncated_flag;

    pkt_log.entries[pkt_log.write_idx] = *entry;
    pkt_log.total_logged++;
    pkt_log.write_idx = next; /* commit - must be the last store */
}

/* Read one entry from the ring buffer; returns false if empty. */
static bool PktLog_Read(PKT_LOG_ENTRY *entry)
{
    if (pkt_log.read_idx == pkt_log.write_idx) {
        return false; /* buffer empty */
    }
    *entry = pkt_log.entries[pkt_log.read_idx];
    pkt_log.read_idx = (pkt_log.read_idx + 1u) & (PKT_LOG_BUF_SIZE - 1u);
    return true;
}

void BRIDGE_TimerCallback(uintptr_t context) {
    static uint32_t s_last_idle_count = 0u;
    uint32_t now;
    (void)context;

    if (my_delay_time) my_delay_time--;

    now = s_idle_cycle_count;
    s_idle_cycles_per_sec = now - s_last_idle_count;   /* wraps correctly even if s_idle_cycle_count overflows */
    s_last_idle_count = now;
}

// Help command for Test group
static void test_help(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    (void)argc; (void)argv;
    CMD_PRINT(pCmdIO, "Test group commands:\n\r");
    CMD_PRINT(pCmdIO, "  help                         - Show this help\n\r");
    CMD_PRINT(pCmdIO, "  timestamp                    - Show build timestamp\n\r");
    CMD_PRINT(pCmdIO, "  uptime                       - Time since boot/last reset\n\r");
    CMD_PRINT(pCmdIO, "  ipdump <mode>                - Dump RX IP packets (0=off, 1=eth0, 2=eth1, 3=both)\n\r");
    CMD_PRINT(pCmdIO, "  stats                        - Show TX/RX counters for eth0 and eth1\n\r");
    CMD_PRINT(pCmdIO, "  meminfo                      - Free memory on the C-runtime heap and the TCP/IP heap\n\r");
    CMD_PRINT(pCmdIO, "  dump <addr> <count>          - Dump memory (hex addr, count)\n\r");
    CMD_PRINT(pCmdIO, "  peek <addr> [size]           - Read a single value (size=1|2|4)\n\r");
    CMD_PRINT(pCmdIO, "  poke <addr> <val> [size]     - Write a single value (size=1|2|4)\n\r");
    CMD_PRINT(pCmdIO, "  logclear                     - Clear deferred packet log buffer\n\r");
    CMD_PRINT(pCmdIO, "  logstat                      - Show deferred log statistics\n\r");
    CMD_PRINT(pCmdIO, "  faultlog [clear]             - Show (or clear) the last recorded HardFault analysis\n\r");
    CMD_PRINT(pCmdIO, "  crashtest                    - Deliberately trigger a HardFault (tests faultlog)\n\r");
    CMD_PRINT(pCmdIO, "  cpuload on|off|stats|reset|live - Per-task main-loop cycle profiling (default off)\n\r");
    CMD_PRINT(pCmdIO, "  led on|off                   - Turn the onboard LED1 (PC21) on or off\n\r");
    CMD_PRINT(pCmdIO, "\n\rLAN865x registers, test modes, PLCA: see 'lanhelp'\n\r");
    CMD_PRINT(pCmdIO, "Port mirror/sniffer: see 'mirror'/'sniffer'. Raw frame test: see 'noip_send'.\n\r");
    CMD_PRINT(pCmdIO, "TCP echo server: see 'testserver'. Persistent config: see 'showenv'.\n\r");
}

// stats command: print TX/RX software counters for both interfaces
static void cmd_stats(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    TCPIP_MAC_RX_STATISTICS rxStats;
    TCPIP_MAC_TX_STATISTICS txStats;
    const char *ifNames[] = {"eth0", "eth1"};
    int i;
    (void)argc; (void)argv;
    for (i = 0; i < 2; i++) {
        TCPIP_NET_HANDLE netH = TCPIP_STACK_NetHandleGet(ifNames[i]);
        if (netH == NULL) {
            CMD_PRINT(pCmdIO, "%s: not found\n\r", ifNames[i]);
            continue;
        }
        if (TCPIP_STACK_NetMACStatisticsGet(netH, &rxStats, &txStats)) {
            CMD_PRINT(pCmdIO, "%s TX: ok=%d err=%d qFull=%d pend=%d\n\r",
                ifNames[i], txStats.nTxOkPackets, txStats.nTxErrorPackets,
                txStats.nTxQueueFull, txStats.nTxPendBuffers);
            CMD_PRINT(pCmdIO, "%s RX: ok=%d err=%d nobufs=%d pend=%d\n\r",
                ifNames[i], rxStats.nRxOkPackets, rxStats.nRxErrorPackets,
                rxStats.nRxBuffNotAvailable, rxStats.nRxPendBuffers);
        } else {
            CMD_PRINT(pCmdIO, "%s: stats not available\n\r", ifNames[i]);
        }
    }
    CMD_PRINT(pCmdIO, "main loop: %lu cycles/s\n\r", (unsigned long)s_idle_cycles_per_sec);
}

// Timestamp command to show build info
static void show_timestamp(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    (void)argc; (void)argv;
    CMD_PRINT(pCmdIO, "======================================\n\r");
    CMD_PRINT(pCmdIO, "tcpip_iperf_lan865x bridge - Build Info\n\r");
    CMD_PRINT(pCmdIO, "Build Timestamp: "__DATE__" "__TIME__"\n\r");
    CMD_PRINT(pCmdIO, "======================================\n\r");
}

/* Time since boot/last reset, human-readable - the fast way to tell "the
 * board is still the same process that was running before" from "it silently
 * rebooted (watchdog, assert loop, pyocd reset) and only looks the same". */
static void cmd_uptime(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    uint64_t ticks = SYS_TIME_Counter64Get();
    uint32_t freq = SYS_TIME_FrequencyGet();
    uint64_t total_s = (freq != 0u) ? (ticks / freq) : 0u;
    uint32_t days  = (uint32_t)(total_s / 86400ULL);
    uint32_t hours = (uint32_t)((total_s % 86400ULL) / 3600ULL);
    uint32_t mins  = (uint32_t)((total_s % 3600ULL) / 60ULL);
    uint32_t secs  = (uint32_t)(total_s % 60ULL);
    (void)argc; (void)argv;

    CMD_PRINT(pCmdIO, "uptime: %ud %02u:%02u:%02u  (%lu s since boot/last reset)\r\n",
        (unsigned)days, (unsigned)hours, (unsigned)mins, (unsigned)secs,
        (unsigned long)total_s);
}

/* Ported from a related in-house project to
   read live config/state (structs, driver descriptors, register-backed
   variables) straight out of RAM over the CLI instead of guessing from source.
   One SYS_CONSOLE_PRINT() call per line instead of one per byte, and a wait
   for free ring-buffer space before each line: SYS_CONSOLE_PRINT() is
   fire-and-forget (its write_t() return value is discarded, see
   sys_console.c), so a tight burst of small prints silently loses data once
   the SERCOM TX ring buffer fills faster than 115200 baud can drain it. */
static void DumpMem(uint32_t addr, uint32_t count)
{
    uint8_t *puc = (uint8_t *) addr;
    uint32_t ix;

    for (ix = 0; ix < count; ix += 16u) {
        uint32_t lineBytes = (count - ix > 16u) ? 16u : (count - ix);
        char line[96];
        char ascii[17];
        int pos;
        uint32_t j;

        pos = snprintf(line, sizeof(line), "%08x: ", (unsigned int)(addr + ix));
        for (j = 0; j < 16u; j++) {
            if (j < lineBytes) {
                uint8_t b = puc[ix + j];
                pos += snprintf(line + pos, sizeof(line) - (size_t)pos, " %02x", b);
                ascii[j] = ((b > 31u) && (b < 127u)) ? (char)b : '.';
            } else {
                pos += snprintf(line + pos, sizeof(line) - (size_t)pos, "   ");
            }
        }
        ascii[lineBytes] = '\0';
        pos += snprintf(line + pos, sizeof(line) - (size_t)pos, "   %s\n\r", ascii);

        while (SYS_CONSOLE_WriteFreeBufferCountGet(SYS_CONSOLE_DEFAULT_INSTANCE) < (ssize_t)pos) {
            /* wait for the SERCOM TX interrupt to drain the ring buffer */
        }
        SYS_CONSOLE_PRINT("%s", line);
    }
}

static void cmd_logclear(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv) {
    (void)argc; (void)argv;
    pkt_log.read_idx     = pkt_log.write_idx; /* drain pending entries */
    pkt_log.overflow_cnt = 0u;
    pkt_log.total_logged = 0u;
    frame_data_pool.write_offset = 0u;
    CMD_PRINT(pCmdIO, "[LOG] ring buffer cleared\r\n");
}

static void cmd_logstat(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv) {
    (void)argc; (void)argv;
    uint32_t wi      = pkt_log.write_idx;  /* snapshot volatile index */
    uint32_t pending = (wi - pkt_log.read_idx) & (PKT_LOG_BUF_SIZE - 1u);
    CMD_PRINT(pCmdIO, "[LOG] total=%u pending=%u overflows=%u bufsize=%u\r\n",
        (unsigned)pkt_log.total_logged, (unsigned)pending,
        (unsigned)pkt_log.overflow_cnt, (unsigned)PKT_LOG_BUF_SIZE);
    CMD_PRINT(pCmdIO, "[LOG] pool_offset=%u pool_size=%u (%u frames x %u bytes)\r\n",
        (unsigned)frame_data_pool.write_offset,
        (unsigned)FRAME_DATA_POOL_SIZE,
        (unsigned)PKT_LOG_MAX_FRAMES,
        (unsigned)PKT_LOG_MAX_FRAME_SIZE);
}

static void cmd_faultlog(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv) {
    if ((argc >= 2) && (strcmp(argv[1], "clear") == 0)) {
        CRASHLOG_ClearRecord(pCmdIO);
        return;
    }
    CRASHLOG_PrintRecord(pCmdIO);
}

/* Test-only: deliberately triggers a HardFault to exercise crashlog.c end to
 * end - see CRASHLOG_TriggerTestFault()'s comment for why it uses "udf"
 * rather than an invalid address. */
static void cmd_crashtest(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv) {
    (void)argc; (void)argv;
    CMD_PRINT(pCmdIO, "crashtest: triggering a HardFault now...\n\r");
    CRASHLOG_TriggerTestFault();
}

/* cpuload on|off|stats|reset|live - see cpuload.c. Default is off; 'stats'
 * with no argument (or an unrecognized one) just prints current stats, same
 * shape as 'faultlog [clear]' above. */
static void cmd_cpuload(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv) {
    if (argc >= 2) {
        if (strcmp(argv[1], "on") == 0) {
            CPULOAD_Enable(true);
            CMD_PRINT(pCmdIO, "cpuload: enabled, stats reset\n\r");
            return;
        }
        if (strcmp(argv[1], "off") == 0) {
            CPULOAD_Enable(false);
            CMD_PRINT(pCmdIO, "cpuload: disabled\n\r");
            return;
        }
        if (strcmp(argv[1], "reset") == 0) {
            CPULOAD_Reset();
            CMD_PRINT(pCmdIO, "cpuload: stats reset\n\r");
            return;
        }
        if (strcmp(argv[1], "live") == 0) {
            CPULOAD_LiveStart(pCmdIO);
            return;
        }
    }
    CPULOAD_PrintStats(pCmdIO);
}

/* led on|off - see leds.c for the pin (PC21) and polarity. */
static void cmd_led(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv) {
    if (argc >= 2) {
        if (strcmp(argv[1], "on") == 0) {
            LEDS_Led1Set(true);
            CMD_PRINT(pCmdIO, "led: LED1 on\n\r");
            return;
        }
        if (strcmp(argv[1], "off") == 0) {
            LEDS_Led1Set(false);
            CMD_PRINT(pCmdIO, "led: LED1 off\n\r");
            return;
        }
    }
    CMD_PRINT(pCmdIO, "usage: led on|off\n\r");
}

static void my_dump(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc < 2) {
        CMD_PRINT(pCmdIO, "Usage: ipdump <mode>  (0=off, 1=eth0, 2=eth1, 3=both)\n\r");
        return;
    }
    ipdump_mode = strtoul(argv[1], NULL, 16);
    if (ipdump_mode == 0) {
        CMD_PRINT(pCmdIO, "IP Layer Dump de-activated\n\r");
    } else if (ipdump_mode == 1) {
        CMD_PRINT(pCmdIO, "IP Layer Dump activated on eth0\n\r");
    } else if (ipdump_mode == 2) {
        CMD_PRINT(pCmdIO, "IP Layer Dump activated on eth1\n\r");
    } else if (ipdump_mode == 3) {
        CMD_PRINT(pCmdIO, "IP Layer Dump activated on eth0 and eth1\n\r");
    } else {
        CMD_PRINT(pCmdIO, "Parameter out of range\n\r");
    }
}

/* Same hex dump as DumpMem() above, but for the "dump" command itself: replies
 * to whichever device issued it (CMD_PRINT) instead of always the serial
 * console. Kept as a separate function rather than adding a pCmdIO parameter
 * to DumpMem() - that one is also called from the deferred packet-log drain
 * in APP_Tasks() (PKT_LOG_ETH1), which has no command context.
 *
 * Reuses DumpMem()'s own SYS_CONSOLE_WriteFreeBufferCountGet() busy-wait
 * UNCONDITIONALLY, even for a Telnet-issued dump, on purpose - not a mistake.
 * Originally (2026-08-31, split off from DumpMem() for the Telnet
 * console-routing fix) this function had no flow control at all, and a
 * "dump" with enough bytes to overrun the serial UART's 1024-byte TX ring
 * buffer (SERCOM1_USART_Write() silently drops whatever does not fit, see
 * its own comment) produced garbled, not just truncated, serial output:
 * printing into the reused per-print consolePrintBuffer while the previous
 * line was still only partially drained interleaved bytes from two
 * different lines. A fixed per-line pacing delay fixed the corruption but
 * penalized every dump, including ones far too small to ever need it,
 * because it cannot tell whether the wait is actually necessary.
 *
 * The real fix needs no such guesswork and no pCmdIO-type detection either:
 * this busy-wait only ever blocks on the SERIAL console's own ring buffer,
 * which is a shared but effectively idle resource whenever nothing else is
 * printing to it - so for a Telnet-issued dump it reports "plenty of room"
 * almost immediately, in practice adding no delay, while a serial-issued
 * dump gets exactly the correct, load-adaptive throttling this check was
 * designed for.
 *
 * Telnet output's OWN correctness does not come from this check at all - it
 * comes from F_Telnet_MSG() (telnet.c, HAND-PATCH), which as of 2026-08-31
 * retries on NET_PRES_SocketWrite()'s actual return value instead of
 * discarding it, so a Telnet dump completes correctly regardless of size
 * (verified up to 39554 bytes / dump 32000), not just up to whatever
 * TCPIP_TELNET_SKT_TX_BUFF_SIZE happens to be. This busy-wait staying
 * transport-blind is still the right call, not an oversight: it is a no-op
 * for Telnet either way, so there is nothing to gain from teaching it about
 * pCmdIO's transport. Root-caused and all stages verified 2026-08-31 (see
 * docs/session-log.md). */
/* Lets telnet.c's F_Telnet_MSG() (HAND-PATCH, see that file) actually make
 * progress while it busy-waits for TX space, instead of just spinning.
 *
 * Root cause this works around: in this bare-metal, single-superloop build,
 * SYS_CMD_Tasks() - which is what runs a command handler like CmdDumpMem()
 * and, through it, F_Telnet_MSG() - is called BEFORE TCPIP_STACK_Task() and
 * NET_PRES_Tasks() in SYS_Tasks() (see config/default/tasks.c). Nothing else
 * in the loop can drain a Telnet socket's TX buffer until those two run, so a
 * plain busy-wait inside F_Telnet_MSG() (tried and measured 2026-08-31: still
 * truncated, and up to 6.6s slower for no gain) always burns its full timeout
 * waiting for a drain that can never happen without this call. Calling the
 * pump directly is safe here specifically because F_Telnet_MSG() is only ever
 * reached via the SYS_CMD_API .msg/.print callback (confirmed: no other
 * caller in telnet.c) - i.e. only from SYS_CMD_Tasks()'s call chain, which is
 * a sibling of TCPIP_STACK_Task() in SYS_Tasks(), never nested inside it. So
 * this is an out-of-turn extra call, not a reentrant one. */
void APP_PumpNetworkStack(void)
{
    TCPIP_STACK_Task(sysObj.tcpip);
    NET_PRES_Tasks(sysObj.netPres);
}

static void CmdDumpMem(SYS_CMD_DEVICE_NODE *pCmdIO, uint32_t addr, uint32_t count)
{
    uint8_t *puc = (uint8_t *) addr;
    uint32_t ix;

    for (ix = 0; ix < count; ix += 16u) {
        uint32_t lineBytes = (count - ix > 16u) ? 16u : (count - ix);
        char line[96];
        char ascii[17];
        int pos;
        uint32_t j;

        pos = snprintf(line, sizeof(line), "%08x: ", (unsigned int)(addr + ix));
        for (j = 0; j < 16u; j++) {
            if (j < lineBytes) {
                uint8_t b = puc[ix + j];
                pos += snprintf(line + pos, sizeof(line) - (size_t)pos, " %02x", b);
                ascii[j] = ((b > 31u) && (b < 127u)) ? (char)b : '.';
            } else {
                pos += snprintf(line + pos, sizeof(line) - (size_t)pos, "   ");
            }
        }
        ascii[lineBytes] = '\0';
        pos += snprintf(line + pos, sizeof(line) - (size_t)pos, "   %s\n\r", ascii);

        while (SYS_CONSOLE_WriteFreeBufferCountGet(SYS_CONSOLE_DEFAULT_INSTANCE) < (ssize_t)pos) {
            /* wait for the SERCOM TX interrupt to drain the ring buffer - see
             * this function's own comment for why this is correct even for
             * a Telnet-issued dump. */
        }
        CMD_PRINT(pCmdIO, "%s", line);
    }
}

/* CLI command: dump <address_hex> <count> */
static void cmd_mem_dump(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv)
{
    if (argc != 3) {
        CMD_PRINT(pCmdIO, "Usage: dump <address_hex> <count>\n\r");
        CMD_PRINT(pCmdIO, "Example: dump 0x20000000 64\n\r");
        return;
    }

    uint32_t addr  = strtoul(argv[1], NULL, 0);
    uint32_t count = strtoul(argv[2], NULL, 0);

    if (count == 0u) {
        CMD_PRINT(pCmdIO, "Count must be > 0\n\r");
        return;
    }

    CMD_PRINT(pCmdIO, "Memory dump: 0x%08X  %u bytes\n\r", (unsigned int)addr, (unsigned int)count);
    CmdDumpMem(pCmdIO, addr, count);
}

/* CLI command: peek <address_hex> [size=1|2|4, default 4] - read a single value */
static void cmd_mem_peek(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv)
{
    if (argc < 2 || argc > 3) {
        CMD_PRINT(pCmdIO, "Usage: peek <address_hex> [size=1|2|4]\n\r");
        CMD_PRINT(pCmdIO, "Example: peek 0x42000000 4\n\r");
        return;
    }

    uint32_t addr = strtoul(argv[1], NULL, 0);
    uint32_t size = (argc == 3) ? strtoul(argv[2], NULL, 0) : 4u;

    switch (size) {
        case 1u:
            CMD_PRINT(pCmdIO, "0x%08X: 0x%02X\n\r", (unsigned int)addr, (unsigned int)*(volatile uint8_t*)addr);
            break;
        case 2u:
            CMD_PRINT(pCmdIO, "0x%08X: 0x%04X\n\r", (unsigned int)addr, (unsigned int)*(volatile uint16_t*)addr);
            break;
        case 4u:
            CMD_PRINT(pCmdIO, "0x%08X: 0x%08X\n\r", (unsigned int)addr, (unsigned int)*(volatile uint32_t*)addr);
            break;
        default:
            CMD_PRINT(pCmdIO, "Size must be 1, 2, or 4\n\r");
            break;
    }
}

/* CLI command: poke <address_hex> <value_hex> [size=1|2|4, default 4] - write a single value */
static void cmd_mem_poke(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv)
{
    if (argc < 3 || argc > 4) {
        CMD_PRINT(pCmdIO, "Usage: poke <address_hex> <value_hex> [size=1|2|4]\n\r");
        CMD_PRINT(pCmdIO, "Example: poke 0x42000000 0x12345678 4\n\r");
        return;
    }

    uint32_t addr  = strtoul(argv[1], NULL, 0);
    uint32_t value = strtoul(argv[2], NULL, 0);
    uint32_t size  = (argc == 4) ? strtoul(argv[3], NULL, 0) : 4u;

    switch (size) {
        case 1u:
            *(volatile uint8_t*)addr = (uint8_t)value;
            CMD_PRINT(pCmdIO, "0x%08X <= 0x%02X\n\r", (unsigned int)addr, (unsigned int)(uint8_t)value);
            break;
        case 2u:
            *(volatile uint16_t*)addr = (uint16_t)value;
            CMD_PRINT(pCmdIO, "0x%08X <= 0x%04X\n\r", (unsigned int)addr, (unsigned int)(uint16_t)value);
            break;
        case 4u:
            *(volatile uint32_t*)addr = value;
            CMD_PRINT(pCmdIO, "0x%08X <= 0x%08X\n\r", (unsigned int)addr, (unsigned int)value);
            break;
        default:
            CMD_PRINT(pCmdIO, "Size must be 1, 2, or 4\n\r");
            break;
    }
}

/* meminfo: free memory on BOTH heaps.
 *  - C-runtime heap: XC32 uses nano-malloc (no mallinfo, and the whole heap is
 *    sbrk'd up front with free blocks tracked internally), so we report the total
 *    reserved size (_eheap-_heap) and PROBE the largest allocatable block with a
 *    non-destructive malloc/free binary search - a real "largest free chunk".
 *  - TCP/IP stack heap: the DRAM pool where packets/sockets/the MAC bridge
 *    allocate (same figures as the built-in 'heapinfo'). */
extern char _heap;            /* linker: C-runtime heap start (absolute symbol)  */
extern char _eheap;           /* linker: C-runtime heap end (= _heap + heap size) */
static size_t cheap_largest_free(size_t cap) {
    size_t lo = 1u, hi = cap, best = 0u;
    while (lo <= hi) {
        size_t mid = lo + (hi - lo) / 2u;
        void *p = malloc(mid);
        if (p) { free(p); best = mid; lo = mid + 1u; }
        else   { if (mid == 0u) break; hi = mid - 1u; }
    }
    return best;
}
static void cmd_meminfo(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    size_t total = (size_t)((uintptr_t)&_eheap - (uintptr_t)&_heap);  /* via uintptr_t: not UB pointer subtraction */
    size_t largest = cheap_largest_free(total);
    TCPIP_STACK_HEAP_HANDLE h;
    (void)argc; (void)argv;

    CMD_PRINT(pCmdIO, "C-runtime heap: total=%u  largest free block=%u  (nano-malloc; no exact free count)\r\n",
        (unsigned)total, (unsigned)largest);

    h = TCPIP_STACK_HeapHandleGet(TCPIP_STACK_HEAP_TYPE_INTERNAL, 0);
    if (h != 0) {
        CMD_PRINT(pCmdIO, "TCP/IP heap:    size=%u  free=%u  maxblock=%u  highwater=%u\r\n",
            (unsigned)TCPIP_STACK_HEAP_Size(h), (unsigned)TCPIP_STACK_HEAP_FreeSize(h),
            (unsigned)TCPIP_STACK_HEAP_MaxSize(h), (unsigned)TCPIP_STACK_HEAP_HighWatermark(h));
    } else {
        CMD_PRINT(pCmdIO, "TCP/IP heap:    (no handle)\r\n");
    }
}

static const SYS_CMD_DESCRIPTOR s_cmdTbl[] = {
    {"help", (SYS_CMD_FNC) test_help, ": show Test group commands"},
    {"timestamp", (SYS_CMD_FNC) show_timestamp, ": show build timestamp"},
    {"uptime", (SYS_CMD_FNC) cmd_uptime, ": time since boot/last reset (d hh:mm:ss)"},
    {"ipdump", (SYS_CMD_FNC) my_dump, ": dump rx ip packets (0:off 1:eth0 2:eth1 3:both)"},
    {"stats", (SYS_CMD_FNC) cmd_stats, ": show TX/RX counters for eth0 and eth1"},
    {"meminfo", (SYS_CMD_FNC) cmd_meminfo, ": free memory on the C-runtime heap and the TCP/IP heap"},
    {"dump", (SYS_CMD_FNC) cmd_mem_dump, ": dump memory (dump <addr_hex> <count>)"},
    {"peek", (SYS_CMD_FNC) cmd_mem_peek, ": read a single value (peek <addr_hex> [size=1|2|4])"},
    {"poke", (SYS_CMD_FNC) cmd_mem_poke, ": write a single value (poke <addr_hex> <value_hex> [size=1|2|4])"},
    {"logclear",     (SYS_CMD_FNC) cmd_logclear,     ": clear deferred packet log buffer"},
    {"logstat",      (SYS_CMD_FNC) cmd_logstat,      ": show deferred log statistics (total, pending, overflows)"},
    {"faultlog",     (SYS_CMD_FNC) cmd_faultlog,     ": show the last recorded HardFault analysis (faultlog [clear])"},
    {"crashtest",    (SYS_CMD_FNC) cmd_crashtest,    ": deliberately trigger a HardFault, to test faultlog"},
    {"cpuload",      (SYS_CMD_FNC) cmd_cpuload,      ": per-task main-loop cycle profiling (cpuload on|off|stats|reset|live)"},
    {"led",          (SYS_CMD_FNC) cmd_led,          ": turn the onboard LED1 (PC21) on or off (led on|off)"},
};

static bool Command_Init(void)
{
    return SYS_CMD_ADDGRP(s_cmdTbl, sizeof(s_cmdTbl) / sizeof(*s_cmdTbl), "Test", ": Test Commands");
}


// *****************************************************************************
// *****************************************************************************
// Section: Application Initialization and State Machine Functions
// *****************************************************************************
// *****************************************************************************

/*******************************************************************************
  Function:
    void APP_Initialize ( void )

  Remarks:
    See prototype in app.h.
 */

void APP_Initialize ( void )
{
    /* Place the App state machine in its initial state. */
    appData.state = APP_STATE_INIT;

    /* TCPIP_TELNET_AuthenticationRegister() is deferred to APP_STATE_SERVICE_TASKS,
     * NOT called here: TCPIP_TELNET_Initialize() (MCC-generated telnet.c) runs as
     * part of the TCP/IP stack's own module init, which at this point has only
     * been STARTED (not completed) by TCPIP_STACK_Init() - and unconditionally
     * resets its module-static telnetAuthHandler to NULL. Calling this here
     * appeared to succeed (non-NULL handle returned) but was silently wiped out
     * moments later, so every real login attempt found telnetAuthHandler == NULL
     * and fell through to "Access denied" without ever calling
     * TelnetAuthenticationHandler() - confirmed 2026-08-31 (see docs/session-log.md).
     * Same root cause class as the MIRROR_Initialize() bug above: an app.c call
     * into a TCP/IP module before that module's own init has actually run. */

    timerHandle = SYS_TIME_TimerCreate(0, SYS_TIME_MSToCount(1000), &BRIDGE_TimerCallback, (uintptr_t) NULL, SYS_TIME_PERIODIC);
    SYS_TIME_TimerStart(timerHandle);

    Command_Init();
    LEDS_Initialize();
    LAN865X_DIAG_Initialize();
    NOIP_Initialize();
    TESTSERVER_Initialize();
    /* MIRROR_Initialize() is deferred to APP_STATE_SERVICE_TASKS, NOT called here:
     * unlike the other three (which only register CLI commands), it allocates its
     * packet pool from the TCP/IP heap via TCPIP_PKT_PacketAlloc(). At this point
     * APP_Initialize() is still running synchronously inside SYS_Initialize() -
     * TCPIP_STACK_Init() has only started the stack's own (asynchronous)
     * initialization, the heap is not necessarily up yet, and calling into it
     * this early caused a hard fault (bus fault, invalid pointer deref inside
     * TCPIP_HEAP_MallocInline) - see docs/session-log.md. */
}


// *****************************************************************************
// Section: Boot banner - which MAC/PHY is actually fitted, and which revision
//
// Both identities have to be read from the hardware, and both reads are
// asynchronous: eth0's goes out over the TC6 SPI link, eth1's over MDIO. So the
// banner cannot be printed from APP_STATE_SERVICE_TASKS directly - it is
// assembled by banner_tasks() over the following main-loop passes and printed
// once, when every answer is in or has timed out. A PHY that is not fitted
// reads back all-ones or does not answer at all, which is exactly the
// information wanted, so a failed read is reported rather than hidden.
// *****************************************************************************

#define BANNER_LAN865X_DEVID    0x000A0094u  /* MMS10 Misc. Per errata DS80001075F
                                              * this - not OA_PHYID - is the
                                              * register that identifies the part. */
#define BANNER_PHY_REG_ID1      2u           /* IEEE 802.3 clause 22 PHY ID */
#define BANNER_PHY_REG_ID2      3u
/* Clause 22 PHYID2 fields, same masks as the driver's own drv_extphy_regs.h */
#define BANNER_PHYID2_OUI_LSB   0xFC00u
#define BANNER_PHYID2_MODEL     0x03F0u
#define BANNER_PHYID2_REV       0x000Fu
#define BANNER_STEP_TIMEOUT_MS  1500u

typedef enum {
    BANNER_IDLE = 0,
    BANNER_ETH0_START,
    BANNER_ETH0_WAIT,
    BANNER_ETH1_ID1_START,
    BANNER_ETH1_ID1_WAIT,
    BANNER_ETH1_ID2_START,
    BANNER_ETH1_ID2_WAIT,
    BANNER_PRINT
} banner_state_t;

static banner_state_t s_banner_state    = BANNER_IDLE;
static bool           s_banner_eth0_up  = false;
static bool           s_banner_eth1_up  = false;
static bool           s_banner_eth0_ok  = false;   /* DEVID actually read back */
static uint32_t       s_banner_devid    = 0u;
static bool           s_banner_eth1_ok  = false;   /* both PHY ID regs read back */
static uint16_t       s_banner_phyid1   = 0u;
static uint16_t       s_banner_phyid2   = 0u;
static uint64_t       s_banner_deadline = 0u;
static DRV_HANDLE     s_banner_miim     = DRV_HANDLE_INVALID;
static DRV_MIIM_OPERATION_HANDLE s_banner_miim_op = NULL;

static void banner_arm_timeout(void)
{
    uint64_t tps = (uint64_t)SYS_TIME_FrequencyGet();
    s_banner_deadline = SYS_TIME_Counter64Get() + ((tps * BANNER_STEP_TIMEOUT_MS) / 1000ULL);
}

static bool banner_timed_out(void)
{
    return ((int64_t)(SYS_TIME_Counter64Get() - s_banner_deadline) >= 0);
}

/* Start the identification. Called once, from APP_STATE_SERVICE_TASKS. */
static void banner_start(bool eth0_up, bool eth1_up)
{
    s_banner_eth0_up = eth0_up;
    s_banner_eth1_up = eth1_up;
    /* Must be armed here: BANNER_ETH0_START checks the deadline while waiting
       for env_apply()'s PLCA write to release the single register slot, and an
       unarmed (zero) deadline reads as already expired. */
    banner_arm_timeout();
    s_banner_state   = BANNER_ETH0_START;
}

/* One MDIO read, kicked off and left to complete. Returns false if the MIIM
   driver would not take the request, in which case the caller gives up on
   eth1's identity rather than blocking the banner. */
static bool banner_miim_read_start(uint16_t reg)
{
    DRV_MIIM_RESULT res = DRV_MIIM_RES_OK;

    if (s_banner_miim == DRV_HANDLE_INVALID) {
        /* A second client alongside the PHY driver's own - the MIIM instance is
           configured for DRV_MIIM_INSTANCE_CLIENTS (2), so there is room. */
        s_banner_miim = DRV_MIIM_Open(DRV_MIIM_INDEX_0, DRV_IO_INTENT_SHARED);
        if (s_banner_miim == DRV_HANDLE_INVALID) {
            return false;
        }
    }
    s_banner_miim_op = DRV_MIIM_Read(s_banner_miim, reg,
                                     (uint16_t)DRV_LAN8742A_PHY_ADDRESS,
                                     DRV_MIIM_OPERATION_FLAG_NONE, &res);
    return ((s_banner_miim_op != NULL) && (res >= DRV_MIIM_RES_OK));
}

/* Collect a started MDIO read. Returns true once it is finished either way;
   *pOk says whether a value was actually produced. */
static bool banner_miim_read_done(uint16_t *pVal, bool *pOk)
{
    uint32_t data = 0u;
    DRV_MIIM_RESULT res = DRV_MIIM_OperationResult(s_banner_miim, s_banner_miim_op, &data);

    if (res == DRV_MIIM_RES_PENDING) {
        if (!banner_timed_out()) {
            return false;
        }
        (void)DRV_MIIM_OperationAbort(s_banner_miim, s_banner_miim_op);
        *pOk = false;
        return true;
    }
    *pOk  = (res == DRV_MIIM_RES_OK);
    *pVal = (uint16_t)data;
    return true;
}

static void banner_print(void)
{
    /* eth0 - LAN865x family, DEVID: model in bits 19:4, revision in 3:0 */
    if (!s_banner_eth0_up) {
        SYS_CONSOLE_PRINT("eth0 (10BASE-T1S) : NOT AVAILABLE\n\r");
    } else if (!s_banner_eth0_ok) {
        SYS_CONSOLE_PRINT("eth0 (10BASE-T1S) : up    (chip ID could not be read)\n\r");
    } else {
        uint32_t model = (s_banner_devid >> 4) & 0xFFFFu;
        uint32_t rev   =  s_banner_devid       & 0xFu;
        const char *name = (model == 0x8651u) ? "LAN8651"
                         : (model == 0x8650u) ? "LAN8650" : "unknown model";
        SYS_CONSOLE_PRINT("eth0 (10BASE-T1S) : up    %s rev %u  (DEVID 0x%08X)\n\r",
                          name, (unsigned)rev, (unsigned int)s_banner_devid);
    }

    /* eth1 - external PHY, clause 22 PHY ID across registers 2 and 3 */
    if (!s_banner_eth1_up) {
        SYS_CONSOLE_PRINT("eth1 (100BASE-TX) : NOT AVAILABLE\n\r");
    } else if (!s_banner_eth1_ok) {
        SYS_CONSOLE_PRINT("eth1 (100BASE-TX) : up    (PHY ID could not be read)\n\r");
    } else {
        /* OUI is 22 bits: all of PHYID1, then PHYID2's top 6 */
        uint32_t oui   = ((uint32_t)s_banner_phyid1 << 6)
                       | (uint32_t)((s_banner_phyid2 & BANNER_PHYID2_OUI_LSB) >> 10);
        uint32_t model = (uint32_t)((s_banner_phyid2 & BANNER_PHYID2_MODEL) >> 4);
        uint32_t rev   = (uint32_t) (s_banner_phyid2 & BANNER_PHYID2_REV);
        /* Model 0x11 is what the AC320004-3 daughter board fitted on this bench
           reports - see README.md, where that pairing was confirmed against a
           known-good LAN8740A image. Anything else prints the number only,
           rather than guessing a name; the raw registers are always shown so a
           wrong label could not hide the real value. Note MCC selects the
           LAN8742A driver object, which drives either part for this purpose. */
        const char *part = (oui != 0x0001F0u) ? ""
                         : (model == 0x11u)   ? "LAN8740A " : "";
        SYS_CONSOLE_PRINT("eth1 (100BASE-TX) : up    %s %smodel 0x%02X rev %u  (OUI 0x%06X, PHYID %04X:%04X)\n\r",
                          (oui == 0x0001F0u) ? "Microchip" : "vendor", part,
                          (unsigned)model, (unsigned)rev, (unsigned int)oui,
                          (unsigned)s_banner_phyid1, (unsigned)s_banner_phyid2);
    }

    if (!s_banner_eth0_up || !s_banner_eth1_up) {
        SYS_CONSOLE_PRINT("Bridging is DISABLED - it needs both interfaces.\n\r");
        SYS_CONSOLE_PRINT("Continuing on the surviving interface; check the PHY/daughter board.\n\r");
    }
}

/* Driven from APP_STATE_IDLE, after LAN865X_DIAG_Tasks() so that a completed
   register read is visible in the same pass. */
static void banner_tasks(void)
{
    switch (s_banner_state) {
        case BANNER_IDLE:
            break;

        case BANNER_ETH0_START:
            if (!s_banner_eth0_up) {
                s_banner_state = BANNER_ETH1_ID1_START;   /* nothing to ask */
            } else if (!LAN865X_DIAG_Busy() && LAN865X_DIAG_Read(BANNER_LAN865X_DEVID)) {
                banner_arm_timeout();
                s_banner_state = BANNER_ETH0_WAIT;
            } else if (banner_timed_out()) {
                s_banner_state = BANNER_ETH1_ID1_START;   /* slot never freed up */
            } else {
                /* env_apply()'s PLCA write may still hold the single slot */
            }
            break;

        case BANNER_ETH0_WAIT:
            if (LAN865X_DIAG_LastReadValue(BANNER_LAN865X_DEVID, &s_banner_devid)) {
                s_banner_eth0_ok = true;
                s_banner_state   = BANNER_ETH1_ID1_START;
            } else if (banner_timed_out()) {
                s_banner_state = BANNER_ETH1_ID1_START;
            }
            break;

        case BANNER_ETH1_ID1_START:
            if (!s_banner_eth1_up) {
                s_banner_state = BANNER_PRINT;
            } else if (banner_miim_read_start(BANNER_PHY_REG_ID1)) {
                banner_arm_timeout();
                s_banner_state = BANNER_ETH1_ID1_WAIT;
            } else {
                s_banner_state = BANNER_PRINT;
            }
            break;

        case BANNER_ETH1_ID1_WAIT:
        {
            bool ok = false;
            if (banner_miim_read_done(&s_banner_phyid1, &ok)) {
                s_banner_state = ok ? BANNER_ETH1_ID2_START : BANNER_PRINT;
            }
            break;
        }

        case BANNER_ETH1_ID2_START:
            if (banner_miim_read_start(BANNER_PHY_REG_ID2)) {
                banner_arm_timeout();
                s_banner_state = BANNER_ETH1_ID2_WAIT;
            } else {
                s_banner_state = BANNER_PRINT;
            }
            break;

        case BANNER_ETH1_ID2_WAIT:
        {
            bool ok = false;
            if (banner_miim_read_done(&s_banner_phyid2, &ok)) {
                s_banner_eth1_ok = ok;
                s_banner_state   = BANNER_PRINT;
            }
            break;
        }

        case BANNER_PRINT:
        default:
            if (s_banner_miim != DRV_HANDLE_INVALID) {
                DRV_MIIM_Close(s_banner_miim);
                s_banner_miim = DRV_HANDLE_INVALID;
            }
            banner_print();
            s_banner_state = BANNER_IDLE;
            break;
    }
}

/******************************************************************************
  Function:
    void APP_Tasks ( void )

  Remarks:
    See prototype in app.h.
 */

void APP_Tasks ( void )
{

    /* Check the application's current state. */
    switch ( appData.state )
    {
        /* Application's initial state. */
        case APP_STATE_INIT:
        {
            bool appInitialized = true;

            my_delay_time = 5;
            if (appInitialized)
            {
                appData.state = APP_STATE_WAIT;
            }
            break;
        }

        case APP_STATE_WAIT:
            if (my_delay_time == 0) {
                appData.state = APP_STATE_SERVICE_TASKS;
            }
            break;

        case APP_STATE_SERVICE_TASKS:
        {
            TCPIP_NET_HANDLE eth0_net_hd = TCPIP_STACK_IndexToNet(0);
            TCPIP_STACK_PacketHandlerRegister(eth0_net_hd, pktEth0Handler, MyEth0HandlerParam);
            TCPIP_NET_HANDLE eth1_net_hd = TCPIP_STACK_IndexToNet(1);
            TCPIP_STACK_PacketHandlerRegister(eth1_net_hd, pktEth1Handler, MyEth1HandlerParam);

            /* Say plainly which interfaces actually came up, and which part is
             * fitted on each. The stack already prints "DRV PHY init failed: -1"
             * when a PHY is not physically there, but that says nothing about
             * what the board can still do. With the tcpip_manager power-state
             * fix, a missing PHY no longer aborts the whole stack - the board
             * keeps running on whatever interface survived, and this report is
             * what tells the operator so.
             *
             * The build timestamp goes out here and now; the interface lines
             * cannot, because identifying the two parts means reading registers
             * over SPI and MDIO. banner_tasks() finishes that over the next few
             * main-loop passes and prints them - see its section above. */
            SYS_CONSOLE_PRINT("Build Timestamp   : "__DATE__" "__TIME__"\n\r");
            CRASHLOG_PrintIfPresent();   /* if the previous boot ended in a HardFault, show it now */
            banner_start(TCPIP_STACK_NetIsUp(eth0_net_hd),
                         TCPIP_STACK_NetIsUp(eth1_net_hd));
            env_apply();   /* push the persisted network config into the stack (once, stack is up) */
            MIRROR_Initialize();  /* deferred from APP_Initialize() - see comment there; stack/heap are up here */
            {
                /* Deferred from APP_Initialize() - see comment there; the telnet
                 * module's own init has actually run by now, so this registration
                 * survives instead of being silently reset to NULL. */
                TCPIP_TELNET_HANDLE telnetAuthHandle = TCPIP_TELNET_AuthenticationRegister(TelnetAuthenticationHandler, &TelnetHandlerParam);
                SYS_CONSOLE_PRINT("Telnet auth handler registration: %s\n\r", (telnetAuthHandle != NULL) ? "OK" : "FAILED (slot already taken)");
            }
            appData.state = APP_STATE_IDLE;
            break;
        }

        case APP_STATE_IDLE:
        {
            static uint64_t ticks_per_ms  = 0u;
            if (ticks_per_ms == 0u) {
                ticks_per_ms = (uint64_t)SYS_TIME_FrequencyGet() / 1000ULL;
            }

            s_idle_cycle_count++;

            /* Register access / test modes / PLCA - see lan865x_diag.c */
            LAN865X_DIAG_Tasks();

            /* Boot-time chip identification. Runs after LAN865X_DIAG_Tasks() so
             * a register read that just completed is visible in the same pass,
             * and does nothing at all once the banner has been printed. */
            banner_tasks();

            /* Sniffer's pending T1SPMACTL.TXD write: retries it and evaluates
             * the readback LAN865X_DIAG_Tasks() just produced, so it has to run
             * after it. See the SNIFFER_TXD_* block in port_mirror.c. */
            MIRROR_Tasks();

            /* TCP echo test server - see testserver.c */
            TESTSERVER_Tasks();

            /* === Deferred packet log output (max 10 entries per APP_Tasks iteration) === */
            if (ticks_per_ms > 0u) {
                PKT_LOG_ENTRY log_e;
                uint32_t max_print = 10u;
                while (max_print-- > 0u && PktLog_Read(&log_e)) {
                    uint64_t ts_ms = log_e.timestamp / ticks_per_ms;
                    switch (log_e.log_type) {
                        case PKT_LOG_NOIP:
                            NOIP_PrintRxLine(log_e.pkt_counter, log_e.noip_seq,
                                             log_e.mac_src, log_e.length, ts_ms);
                            if (log_e.data_len > 0u) {
                                DumpMem((uint32_t)&frame_data_pool.pool[log_e.data_offset], log_e.data_len);
                            }
                            break;
                        case PKT_LOG_ETH0:
                            SYS_CONSOLE_PRINT("E0:%u len=%u ts=%llu ms%s\r\n",
                                (unsigned)log_e.pkt_counter, (unsigned)log_e.length,
                                (unsigned long long)ts_ms,
                                log_e.truncated ? " [TRUNC]" : "");
                            if (log_e.data_len > 0u) {
                                DumpMem((uint32_t)&frame_data_pool.pool[log_e.data_offset], log_e.data_len);
                            }
                            break;
                        case PKT_LOG_ETH1:
                            SYS_CONSOLE_PRINT("E1:%u len=%u ts=%llu ms%s\r\n",
                                (unsigned)log_e.pkt_counter, (unsigned)log_e.length,
                                (unsigned long long)ts_ms,
                                log_e.truncated ? " [TRUNC]" : "");
                            if (log_e.data_len > 0u) {
                                DumpMem((uint32_t)&frame_data_pool.pool[log_e.data_offset], log_e.data_len);
                            }
                            break;
                        default:
                            break;
                    }
                }
            }
            break;
        }

        /* The default state should never be executed. */
        default:
        {
            /* TODO: Handle error in application's state machine. */
            break;
        }
    }
}

bool pktEth0Handler(TCPIP_NET_HANDLE hNet, TCPIP_MAC_PACKET* rxPkt, uint16_t frameType, const void* hParam) {
    static uint32_t packet_counter = 0;
    (void)hNet; (void)hParam;

    packet_counter++;

    /* Port mirror (SPAN) for Wireshark - see port_mirror.c. Checks the enable
     * flag and the own-MAC filter itself. */
    MIRROR_Eth0Rx(rxPkt);

    /* NoIP raw test frame: the module owns the EtherType, the frame layout and
     * the counters. The deferred log ring buffer stays here because ipdump shares
     * it, so the printing happens later in the drain loop (see PKT_LOG_NOIP). */
    if (NOIP_IsNoIpFrame(frameType)) {
        const uint8_t *p = rxPkt->pMacLayer;
        PKT_LOG_ENTRY log_e = {0};
        log_e.timestamp   = SYS_TIME_Counter64Get();
        log_e.pkt_counter = NOIP_CountRx();
        log_e.noip_seq    = NOIP_SeqFromFrame(p);
        log_e.frame_type  = frameType;
        log_e.length      = rxPkt->pDSeg->segLen;
        log_e.iface       = 0u;
        log_e.log_type    = PKT_LOG_NOIP;
        memcpy(log_e.mac_src, &p[6], 6u);
        PktLog_Write(&log_e, rxPkt->pMacLayer, rxPkt->pDSeg->segLen);
        TCPIP_PKT_PacketAcknowledge(rxPkt, TCPIP_MAC_PKT_ACK_RX_OK);
        return true;
    }

    if (ipdump_mode == 1 || ipdump_mode == 3) {
        PKT_LOG_ENTRY log_e = {0};
        log_e.timestamp   = SYS_TIME_Counter64Get();
        log_e.pkt_counter = packet_counter;
        log_e.frame_type  = frameType;
        log_e.length      = rxPkt->pDSeg->segLen;
        log_e.iface       = 0u;
        log_e.log_type    = PKT_LOG_ETH0;
        memcpy(log_e.mac_src, &rxPkt->pMacLayer[6], 6u);
        PktLog_Write(&log_e, rxPkt->pMacLayer, rxPkt->pDSeg->segLen);
    }

    /* eth0<->eth1 L2 bridging is done by the Harmony MAC bridge, not here.
     * Return false so the frame goes to normal stack/bridge processing. */
    return false;
}

bool pktEth1Handler(TCPIP_NET_HANDLE hNet, TCPIP_MAC_PACKET* rxPkt, uint16_t frameType, const void* hParam) {
    static uint32_t packet_counter = 0;
    (void)hNet; (void)hParam;

    packet_counter++;

    if (ipdump_mode == 2 || ipdump_mode == 3) {
        PKT_LOG_ENTRY log_e = {0};
        log_e.timestamp   = SYS_TIME_Counter64Get();
        log_e.pkt_counter = packet_counter;
        log_e.frame_type  = frameType;
        log_e.length      = rxPkt->pDSeg->segLen;
        log_e.iface       = 1u;
        log_e.log_type    = PKT_LOG_ETH1;
        memcpy(log_e.mac_src, &rxPkt->pDSeg->segLoad[6], 6u);
        PktLog_Write(&log_e, rxPkt->pDSeg->segLoad, rxPkt->pDSeg->segLen);
    }
    return false;
}


/*******************************************************************************
 End of File
 */
