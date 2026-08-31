/*******************************************************************************
  TCP echo test server - implementation

  See testserver.h for the why. Polled state machine, driven from
  TESTSERVER_Tasks() - no signal handlers, to keep this as simple/inspectable
  as the rest of this project's small diagnostic modules.
*******************************************************************************/

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>                                          /* strtoul() */
#include <string.h>                                          /* strcmp() */

#include "config/default/system/console/sys_console.h"
#include "config/default/library/tcpip/tcpip.h"
#include "config/default/library/tcpip/tcp.h"
#include "system/command/sys_command.h"
#include "testserver.h"
#include "cmd_print.h"                                        /* CMD_PRINT/CMD_MSG - reply to pCmdIO, not always the serial console */

#define TESTSERVER_DEFAULT_PORT   5566u
#define TESTSERVER_CHUNK          512u    /* bytes per ArrayGet/ArrayPut round */
#define TESTSERVER_BUDGET_PER_CALL 8192u  /* max bytes drained per TESTSERVER_Tasks() call */

typedef enum {
    TESTSERVER_IDLE = 0,     /* not started */
    TESTSERVER_LISTEN,       /* socket open, waiting for a connection */
    TESTSERVER_ECHO          /* connected, echoing */
} testserver_state_t;

static testserver_state_t s_state = TESTSERVER_IDLE;
static TCP_SOCKET s_sock = INVALID_SOCKET;
static TCP_PORT s_port = TESTSERVER_DEFAULT_PORT;
static uint32_t s_rx_bytes = 0u;
static uint32_t s_tx_bytes = 0u;
static uint8_t s_buf[TESTSERVER_CHUNK];
static uint16_t s_pending_len = 0u;   /* bytes in s_buf awaiting TCPIP_TCP_ArrayPut() */
static uint16_t s_pending_off = 0u;   /* how much of that has been queued so far */

static void testserver_start(SYS_CMD_DEVICE_NODE* pCmdIO, TCP_PORT port)
{
    if (s_state != TESTSERVER_IDLE) {
        CMD_PRINT(pCmdIO, "testserver: already running on port %u\n\r", (unsigned)s_port);
        return;
    }
    s_sock = TCPIP_TCP_ServerOpen(IP_ADDRESS_TYPE_IPV4, port, 0);
    if (s_sock == INVALID_SOCKET) {
        CMD_PRINT(pCmdIO, "testserver: TCPIP_TCP_ServerOpen failed\n\r");
        return;
    }
    s_port = port;
    s_rx_bytes = 0u;
    s_tx_bytes = 0u;
    s_state = TESTSERVER_LISTEN;
    CMD_PRINT(pCmdIO, "testserver: listening on port %u\n\r", (unsigned)s_port);
}

static void testserver_stop(SYS_CMD_DEVICE_NODE* pCmdIO)
{
    if (s_state == TESTSERVER_IDLE) {
        CMD_PRINT(pCmdIO, "testserver: not running\n\r");
        return;
    }
    TCPIP_TCP_Close(s_sock);
    s_sock = INVALID_SOCKET;
    s_state = TESTSERVER_IDLE;
    CMD_PRINT(pCmdIO, "testserver: stopped (rx=%lu tx=%lu bytes)\n\r",
                       (unsigned long)s_rx_bytes, (unsigned long)s_tx_bytes);
}

void TESTSERVER_Tasks(void)
{
    uint16_t ready, chunk, got, put;
    uint16_t budget_left;

    switch (s_state) {
    case TESTSERVER_IDLE:
        break;

    case TESTSERVER_LISTEN:
        if (TCPIP_TCP_IsConnected(s_sock)) {
            (void)TCPIP_TCP_WasReset(s_sock);   /* clear the stack's reset flag, as iperf.c does */
            s_rx_bytes = 0u;
            s_tx_bytes = 0u;
            s_state = TESTSERVER_ECHO;
            SYS_CONSOLE_PRINT("testserver: client connected\n\r");
        }
        break;

    case TESTSERVER_ECHO:
        if (TCPIP_TCP_WasReset(s_sock) || TCPIP_TCP_WasDisconnected(s_sock)) {
            SYS_CONSOLE_PRINT("testserver: client disconnected (rx=%lu tx=%lu bytes)\n\r",
                               (unsigned long)s_rx_bytes, (unsigned long)s_tx_bytes);
            /* TCPIP_TCP_Disconnect() on a SERVER socket returns it to listen state
             * (tcp.h's own doc comment says so explicitly) - without this call the
             * socket stays wedged after the first client and never accepts a
             * second one, even though IsConnected()/WasDisconnected() look fine.
             * Found empirically: every second connection attempt timed out until
             * this was added - see docs/FALLSTRICKE.md, 2026-08-27. */
            (void)TCPIP_TCP_Disconnect(s_sock);
            s_pending_len = 0u;
            s_pending_off = 0u;
            s_state = TESTSERVER_LISTEN;   /* socket stays open, ready for the next client */
            break;
        }
        /* Drain up to TESTSERVER_BUDGET_PER_CALL bytes THIS call instead of one
         * TESTSERVER_CHUNK and stopping - a single chunk per call made the app
         * task-loop's own cycle rate (not the 100 Mbps eth1 link, not the T1S
         * segment) the throughput ceiling: measured at ~4.5 Mbps talking
         * straight to the bridge with nothing else in the path, which is far
         * below both what eth1 can carry and what iperf already showed this
         * same forwarding path sustaining one-way (~5.8 Mbps over T1S). Found
         * by the user's own back-of-envelope check against that iperf number -
         * see docs/FALLSTRICKE.md, 2026-08-27. The budget still bounds the loop so
         * a fast client can't starve LAN865X_DIAG_Tasks()/the rest of
         * APP_STATE_IDLE for an entire call. */
        budget_left = TESTSERVER_BUDGET_PER_CALL;
        while (budget_left > 0u) {
            /* Finish queuing a previous chunk before reading more. TCPIP_TCP_ArrayPut()
             * can accept FEWER bytes than given (TX buffer momentarily full) - its
             * return value is not just a byte count to log, it is how much actually
             * got queued. Treating the rest as "sent" and moving on silently drops
             * it: found empirically as real data loss (a C reference client with no
             * threads at all showed the identical loss talking straight to the
             * bridge, no T1S/PLCA involved - see docs/FALLSTRICKE.md, 2026-08-27), not
             * just a throughput number that looked low. Retrying here provides the
             * backpressure ArrayPut's return value implies but does not enforce by
             * itself. */
            if (s_pending_len > s_pending_off) {
                put = TCPIP_TCP_ArrayPut(s_sock, s_buf + s_pending_off, s_pending_len - s_pending_off);
                s_tx_bytes += put;
                s_pending_off += put;
                budget_left = (put < budget_left) ? (budget_left - put) : 0u;
                if (s_pending_off < s_pending_len) {
                    (void)TCPIP_TCP_Flush(s_sock);
                    break;   /* TX buffer still full - stop for this call, try again next */
                }
                s_pending_len = 0u;
                s_pending_off = 0u;
                continue;
            }

            ready = TCPIP_TCP_GetIsReady(s_sock);
            if (ready == 0u) {
                break;   /* nothing more waiting right now */
            }
            chunk = (ready < (uint16_t)sizeof(s_buf)) ? ready : (uint16_t)sizeof(s_buf);
            chunk = (chunk < budget_left) ? chunk : budget_left;
            got = TCPIP_TCP_ArrayGet(s_sock, s_buf, chunk);
            s_rx_bytes += got;
            budget_left = (got < budget_left) ? (budget_left - got) : 0u;
            if (got > 0u) {
                put = TCPIP_TCP_ArrayPut(s_sock, s_buf, got);
                s_tx_bytes += put;
                if (put < got) {
                    s_pending_len = got;
                    s_pending_off = put;
                }
            }
        }
        (void)TCPIP_TCP_Flush(s_sock);   /* one flush at the end of the drain, not one per chunk */
        break;

    default:
        break;
    }
}

/* testserver [start [port]|stop] - no argument shows the current state. */
static void cmd_testserver(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc >= 2) {
        if (strcmp(argv[1], "start") == 0) {
            TCP_PORT port = (argc >= 3) ? (TCP_PORT)strtoul(argv[2], NULL, 0) : TESTSERVER_DEFAULT_PORT;
            testserver_start(pCmdIO, port);
            return;
        }
        if (strcmp(argv[1], "stop") == 0) {
            testserver_stop(pCmdIO);
            return;
        }
        CMD_PRINT(pCmdIO, "usage: testserver [start [port]|stop]\n\r");
        return;
    }

    switch (s_state) {
    case TESTSERVER_IDLE:
        CMD_PRINT(pCmdIO, "testserver: idle\n\r");
        break;
    case TESTSERVER_LISTEN:
        CMD_PRINT(pCmdIO, "testserver: listening on port %u, no client yet\n\r", (unsigned)s_port);
        break;
    case TESTSERVER_ECHO:
        CMD_PRINT(pCmdIO, "testserver: echoing on port %u (rx=%lu tx=%lu bytes)\n\r",
                           (unsigned)s_port, (unsigned long)s_rx_bytes, (unsigned long)s_tx_bytes);
        break;
    default:
        break;
    }
}

static const SYS_CMD_DESCRIPTOR testserver_cmd_tbl[] = {
    {"testserver", (SYS_CMD_FNC) cmd_testserver, ": TCP echo test server (testserver [start [port]|stop])"},
};

void TESTSERVER_Initialize(void) {
    if (!SYS_CMD_ADDGRP(testserver_cmd_tbl, (int)(sizeof testserver_cmd_tbl / sizeof *testserver_cmd_tbl),
                        "testserver", ": TCP echo test server for bandwidth-ramp testing")) {
        SYS_CONSOLE_PRINT("TESTSERVER: SYS_CMD_ADDGRP failed\n\r");
    }
}
