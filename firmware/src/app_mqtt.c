/*
 * app_mqtt.c - see app_mqtt.h for the design and why this hand-rolls a
 * non-blocking CONNECT/PUBLISH state machine on the pure MQTTPacket
 * serializers instead of using paho.mqtt.embedded-c's blocking MQTTClient-C.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "definitions.h"
#include "configuration.h"
#include "config/default/system/time/sys_time.h"
#include "system/command/sys_command.h"
#include "app_mqtt.h"
#include "env.h"
#include "cmd_print.h"
#include "net_pres/pres/net_pres_enc_glue_client.h"

#include "MQTTPacket.h"

// *****************************************************************************
// Section: Configuration
// *****************************************************************************

/* Compiled-in default broker - override at runtime with 'mqtt_broker <ip>
 * [port]' (RAM-only, does not survive a reset - this project's persistent
 * config, env.c's Emulated EEPROM record, is already a tightly-packed fixed
 * layout; adding a broker address to it is out of scope here, same
 * reasoning as net_pres_enc_glue_client.c's identity-reuse decision). */
#define MQTT_BROKER_IP_DEFAULT   "0.0.0.0"     /* unset - MQTT_Tasks() stays idle until configured */
#define MQTT_BROKER_PORT_DEFAULT 8883u

#define MQTT_PUBLISH_INTERVAL_MS   5000u
#define MQTT_KEEPALIVE_S             60u
#define MQTT_TCP_TLS_TIMEOUT_MS   20000u   /* TCP connect + TLS+mTLS handshake, combined */
#define MQTT_CONNACK_TIMEOUT_MS   10000u
#define MQTT_RETRY_INTERVAL_MS    10000u   /* how long to wait after any failure before redialing */

#define MQTT_RX_BUF_LEN   32   /* CONNACK (4B) / PINGRESP (2B) only - this client never subscribes */
#define MQTT_TX_BUF_LEN  192   /* CONNECT and the small JSON PUBLISH below both fit comfortably */

// *****************************************************************************
// Section: State
// *****************************************************************************

typedef enum {
    MQTT_ST_IDLE = 0,       /* no broker configured, or waiting out a retry deadline */
    MQTT_ST_WAIT_TLS,       /* TCP+TLS(+mTLS) handshake in progress */
    MQTT_ST_WAIT_CONNACK,   /* MQTT CONNECT sent, waiting for CONNACK */
    MQTT_ST_CONNECTED       /* publishing every MQTT_PUBLISH_INTERVAL_MS */
} mqtt_state_t;

static mqtt_state_t          s_state = MQTT_ST_IDLE;
static NET_PRES_SKT_HANDLE_T s_sock  = INVALID_SOCKET;

static IPV4_ADDR            s_broker_ip;
static NET_PRES_SKT_PORT_T  s_broker_port = (NET_PRES_SKT_PORT_T)MQTT_BROKER_PORT_DEFAULT;
static bool                 s_broker_configured = false;

static char     s_client_id[24];
static char     s_topic[40];
static uint32_t s_seq = 0u;

static uint64_t s_deadline;        /* meaning depends on s_state - see each case in MQTT_Tasks() */
static uint64_t s_last_activity;   /* last time anything was sent on s_sock - MQTT_ST_CONNECTED only */
static uint64_t s_ticks_per_ms;

/* Last failure reason, so 'mqtt_status' can show it over Telnet -
 * SYS_CONSOLE_PRINT (mqtt_fail()'s own logging) goes to the serial/EDBG
 * console, not whichever console actually ran 'mqtt_broker'/'mqtt_status',
 * so without this a Telnet-only session had no way to see why a connect
 * attempt failed. */
static char s_last_fail_reason[64] = "(none yet)";

static MQTTTransport  s_transport;
static unsigned char  s_rx_buf[MQTT_RX_BUF_LEN];

// *****************************************************************************
// Section: Non-blocking transport glue for MQTTPacket_readnb()
// *****************************************************************************

/* Contract (MQTTPacket.h): -1 = error, 0 = call again, >0 = bytes read.
 * NET_PRES_SocketRead() never blocks and returns 0 when nothing is
 * available yet, which already matches "call again" - no translation
 * needed beyond checking for a dead connection first. */
static int mqtt_transport_getfn(void *sck, unsigned char *buf, int count)
{
    NET_PRES_SKT_HANDLE_T sock = (NET_PRES_SKT_HANDLE_T)(uintptr_t)sck;

    if (NET_PRES_SocketWasReset(sock) || NET_PRES_SocketWasDisconnected(sock)) {
        return -1;
    }
    return (int)NET_PRES_SocketRead(sock, buf, (uint16_t)count);
}

// *****************************************************************************
// Section: Connection lifecycle
// *****************************************************************************

static uint64_t mqtt_now(void)
{
    return SYS_TIME_Counter64Get();
}

static void mqtt_close_socket(void)
{
    if (s_sock != INVALID_SOCKET) {
        NET_PRES_SocketClose(s_sock);
        s_sock = INVALID_SOCKET;
    }
}

/* Every failure path funnels through here: closes the socket (if open),
 * goes back to idle, and arms the retry deadline - never leaves the state
 * machine stuck waiting on a connection that is already gone. */
static void mqtt_fail(const char *why)
{
    mqtt_close_socket();
    s_state = MQTT_ST_IDLE;
    s_deadline = mqtt_now() + (uint64_t)MQTT_RETRY_INTERVAL_MS * s_ticks_per_ms;
    (void)snprintf(s_last_fail_reason, sizeof s_last_fail_reason, "%s", why);
    SYS_CONSOLE_PRINT("MQTT: %s - retrying in %lus\r\n", why,
                       (unsigned long)(MQTT_RETRY_INTERVAL_MS / 1000u));
}

static void mqtt_attempt_connect(void)
{
    NET_PRES_SKT_ERROR_T err = NET_PRES_SKT_OK;
    /* NET_PRES_SocketOpen()'s address parameter is NET_PRES_ADDRESS (a
     * generic 16-byte buffer, net_pres_socketapi.h), not IP_MULTI_ADDRESS -
     * the registered transport (TCPIP_TCP_ClientOpen, initialization.c's
     * netPresTransObject0SC) is reached through a function-pointer cast, so
     * NET_PRES only ever moves these bytes around, never interprets them.
     * An IPv4 address is the first 4 bytes, matching IP_MULTI_ADDRESS.v4Add's
     * own layout - zero the rest so nothing reads uninitialized stack past
     * that as if it were an IPv6 address. */
    NET_PRES_ADDRESS addr;

    memset(&addr, 0, sizeof addr);
    memcpy(addr.addr, &s_broker_ip, sizeof s_broker_ip);

    /* Open UNCONNECTED (addr=NULL) rather than handing the remote address
     * straight to SocketOpen: this board has TWO interfaces on the SAME
     * 192.168.0.0/24 subnet (eth0/T1S, eth1/100BASE-TX - see env.c), so
     * "which interface is this destination on" is genuinely ambiguous to
     * the stack's own routing, exactly like 'ping <ip>' needs an explicit
     * 'i eth1' to disambiguate. Confirmed live 2026-09-06 with a pktmon
     * capture: TCPIP_TCP_ClientOpen()'s automatic interface selection sent
     * the SYN out eth0 (T1S) instead, where no capture on the PC's NIC
     * could ever have seen it - not a network/firewall problem at all.
     * Pin the raw TCP_SOCKET to eth1 explicitly before dialing, same
     * mechanism the 'ping'/'iperf' console commands use
     * (TCPIP_STACK_NetHandleGet("eth1") + TCPIP_TCP_SocketNetSet()). */
    s_sock = NET_PRES_SocketOpen(0, NET_PRES_SKT_ENCRYPTED_STREAM_CLIENT,
                                  (NET_PRES_SKT_ADDR_T)IP_ADDRESS_TYPE_IPV4,
                                  s_broker_port, NULL, &err);
    if (s_sock == INVALID_SOCKET) {
        s_deadline = mqtt_now() + (uint64_t)MQTT_RETRY_INTERVAL_MS * s_ticks_per_ms;
        (void)snprintf(s_last_fail_reason, sizeof s_last_fail_reason,
                        "SocketOpen failed (err=%d)", (int)err);
        return;
    }

    /* Pin to eth1 ONLY long enough to get the SYN transmitted out the
     * correct physical wire, then clear it again - confirmed live
     * 2026-09-06/07:
     *   - Without pinning at all: the SYN never appears on the PC-facing
     *     NIC ("Ethernet 8") - it goes out eth0/T1S instead, a completely
     *     different physical medium a PC NIC cannot see. This board has
     *     two interfaces on the identical 192.168.0.0/24 subnet (eth0/T1S,
     *     eth1/100BASE-TX - see env.c), exactly like 'ping <ip>' needing an
     *     explicit 'i eth1' to disambiguate.
     *   - Pinned and LEFT pinned: the SYN correctly reaches the peer and
     *     the peer's SYN-ACK correctly reaches the board back on the wire -
     *     but the board never completes the handshake (no ACK, no RST,
     *     just silence). Root cause found by reading tcp.c's own RX demux,
     *     F_TcpFindMatchingSocket() (~line 4567):
     *         (pSkt->pSktNet == NULL || pSkt->pSktNet == pPktIf)
     *     TCPIP_TCP_SocketNetSet() sets pSkt->pSktNet to whatever
     *     TCPIP_STACK_NetHandleGet("eth1") returns; if that handle isn't
     *     bit-identical to whatever this project's bridging plumbing
     *     stamps into pRxPkt->pktIf for a frame that physically arrived on
     *     eth1, every reply for this connection silently fails this match
     *     and is dropped before it ever reaches the SYN_SENT-state code -
     *     with no RST, since "no matching socket for a bare ACK" is a
     *     silent-ignore case, not an error case, in this stack.
     *   - Fix: pSktNet only needs to steer the OUTBOUND SYN. Clearing it
     *     back to NULL right after Connect() (persistent=false, so nothing
     *     server-related is touched) makes F_TcpFindMatchingSocket()'s
     *     interface check always pass (NULL matches any) for every reply
     *     on this connection from here on, without giving up the interface
     *     control that got the SYN out the right wire in the first place. */
    {
        TCPIP_NET_HANDLE eth1 = TCPIP_STACK_NetHandleGet("eth1");
        TCP_SOCKET tcpSkt = (TCP_SOCKET)(intptr_t)NET_PRES_SocketGetTransportHandle(s_sock);
        if (eth1 == NULL || !TCPIP_TCP_SocketNetSet(tcpSkt, eth1, false)) {
            mqtt_close_socket();
            s_deadline = mqtt_now() + (uint64_t)MQTT_RETRY_INTERVAL_MS * s_ticks_per_ms;
            (void)snprintf(s_last_fail_reason, sizeof s_last_fail_reason,
                            "eth1 SocketNetSet failed");
            return;
        }

        /* Matches iperf.c's own already-working TCP-client sequence:
         * Open(port) -> RemoteBind(0, addr) -> Connect() - port 0 here
         * because the port was already given to SocketOpen above. */
        if (!NET_PRES_SocketRemoteBind(s_sock, (NET_PRES_SKT_ADDR_T)IP_ADDRESS_TYPE_IPV4,
                                        0, &addr) ||
            !NET_PRES_SocketConnect(s_sock)) {
            mqtt_close_socket();
            s_deadline = mqtt_now() + (uint64_t)MQTT_RETRY_INTERVAL_MS * s_ticks_per_ms;
            (void)snprintf(s_last_fail_reason, sizeof s_last_fail_reason,
                            "RemoteBind/Connect failed");
            return;
        }

        (void)TCPIP_TCP_SocketNetSet(tcpSkt, NULL, false);
    }
    s_state = MQTT_ST_WAIT_TLS;
    s_deadline = mqtt_now() + (uint64_t)MQTT_TCP_TLS_TIMEOUT_MS * s_ticks_per_ms;
}

static void mqtt_send_connect(void)
{
    unsigned char buf[MQTT_TX_BUF_LEN];
    MQTTPacket_connectData opts = MQTTPacket_connectData_initializer;
    int len;

    opts.MQTTVersion = 4;
    opts.clientID.cstring = s_client_id;
    opts.keepAliveInterval = MQTT_KEEPALIVE_S;
    opts.cleansession = 1;

    len = MQTTSerialize_connect(buf, (int)sizeof buf, &opts);
    if (len > 0) {
        /* Short, one-shot protocol messages - same "write, flush, don't
         * chase a partial write across Tasks() calls" precedent as
         * cert_provision.c's cp_report()/bootload.c's short replies. */
        (void)NET_PRES_SocketWrite(s_sock, buf, (uint16_t)len);
        (void)NET_PRES_SocketFlush(s_sock);
    }

    memset(&s_transport, 0, sizeof s_transport);
    s_transport.getfn = mqtt_transport_getfn;
    s_transport.sck   = (void *)(uintptr_t)s_sock;
}

static void mqtt_publish_status(void)
{
    unsigned char buf[MQTT_TX_BUF_LEN];
    char payload[96];
    MQTTString topic = MQTTString_initializer;
    int plen, len;
    uint32_t freq = SYS_TIME_FrequencyGet();
    uint32_t uptime_s = (freq != 0u) ? (uint32_t)(mqtt_now() / freq) : 0u;

    plen = snprintf(payload, sizeof payload,
                     "{\"client\":\"%s\",\"seq\":%lu,\"uptime_s\":%lu}",
                     s_client_id, (unsigned long)s_seq, (unsigned long)uptime_s);
    if (plen < 0) {
        return;
    }
    if ((size_t)plen >= sizeof payload) {
        plen = (int)sizeof payload - 1;
    }
    s_seq++;

    topic.cstring = s_topic;
    len = MQTTSerialize_publish(buf, (int)sizeof buf, 0, 0, 0, 0, topic,
                                 (unsigned char *)payload, plen);
    if (len > 0) {
        (void)NET_PRES_SocketWrite(s_sock, buf, (uint16_t)len);
        (void)NET_PRES_SocketFlush(s_sock);
    }
    s_last_activity = mqtt_now();
}

static void mqtt_send_pingreq(void)
{
    unsigned char buf[8];
    int len = MQTTSerialize_pingreq(buf, (int)sizeof buf);
    if (len > 0) {
        (void)NET_PRES_SocketWrite(s_sock, buf, (uint16_t)len);
        (void)NET_PRES_SocketFlush(s_sock);
    }
    s_last_activity = mqtt_now();
}

// *****************************************************************************
// Section: Console commands
// *****************************************************************************

static void cmd_mqtt_broker(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    IPV4_ADDR addr;

    if (argc < 2 || argc > 3) {
        CMD_PRINT(pCmdIO, "Usage: mqtt_broker <ip> [port]\r\n");
        return;
    }
    if (!TCPIP_Helper_StringToIPAddress(argv[1], &addr)) {
        CMD_PRINT(pCmdIO, "MQTT: bad IP '%s'\r\n", argv[1]);
        return;
    }
    s_broker_ip = addr;
    if (argc == 3) {
        s_broker_port = (NET_PRES_SKT_PORT_T)strtoul(argv[2], NULL, 0);
    }
    s_broker_configured = true;

    /* Re-dial immediately against the new address rather than waiting out
     * whatever retry/connect deadline was already running. */
    mqtt_close_socket();
    s_state = MQTT_ST_IDLE;
    s_deadline = mqtt_now();

    CMD_PRINT(pCmdIO, "MQTT: broker set to %s:%u - connecting\r\n",
              argv[1], (unsigned)s_broker_port);
}

static const char *mqtt_state_name(void)
{
    switch (s_state) {
        case MQTT_ST_IDLE:         return s_broker_configured ? "idle/retrying" : "unconfigured";
        case MQTT_ST_WAIT_TLS:     return "connecting (tcp/tls)";
        case MQTT_ST_WAIT_CONNACK: return "connecting (mqtt connack)";
        case MQTT_ST_CONNECTED:    return "connected";
        default:                   return "?";
    }
}

static void cmd_mqtt_status(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    CMD_PRINT(pCmdIO, "MQTT: state=%s client_id=%s topic=%s port=%u seq=%lu last_fail=%s\r\n",
              mqtt_state_name(), s_client_id, s_topic, (unsigned)s_broker_port,
              (unsigned long)s_seq, s_last_fail_reason);
}

static const SYS_CMD_DESCRIPTOR mqtt_cmd_tbl[] = {
    {"mqtt_broker", (SYS_CMD_FNC)cmd_mqtt_broker, ": set/show the MQTT broker (mqtt_broker <ip> [port], default port 8883)"},
    {"mqtt_status", (SYS_CMD_FNC)cmd_mqtt_status, ": show the MQTT client's current state"},
};

// *****************************************************************************
// Section: Init / Tasks
// *****************************************************************************

void MQTT_Initialize(void)
{
    char mac[18];

    env_mac_str(0, mac);
    /* "AA:BB:CC:DD:EE:FF" -> "bridge-AABBCCDDEEFF" - a valid MQTT client ID
     * (no ':' - some brokers/validators reject it) that is also stable and
     * unique per board without any new provisioning. */
    {
        size_t si = 0, di = 0;
        (void)snprintf(s_client_id, sizeof s_client_id, "bridge-");
        di = strlen(s_client_id);
        for (si = 0; mac[si] != '\0' && di < sizeof(s_client_id) - 1u; si++) {
            if (mac[si] != ':') {
                s_client_id[di++] = mac[si];
            }
        }
        s_client_id[di] = '\0';
    }
    (void)snprintf(s_topic, sizeof s_topic, "bridge/%s/status", s_client_id);

    (void)TCPIP_Helper_StringToIPAddress(MQTT_BROKER_IP_DEFAULT, &s_broker_ip);
    s_broker_configured = (strcmp(MQTT_BROKER_IP_DEFAULT, "0.0.0.0") != 0);

    if (!SYS_CMD_ADDGRP(mqtt_cmd_tbl, (int)(sizeof mqtt_cmd_tbl / sizeof *mqtt_cmd_tbl),
                         "mqtt", ": MQTT-over-mTLS status publisher")) {
        SYS_CONSOLE_PRINT("MQTT: SYS_CMD_ADDGRP failed\r\n");
    }
}

void MQTT_Tasks(void)
{
    uint64_t now;

    if (s_ticks_per_ms == 0u) {
        s_ticks_per_ms = (uint64_t)SYS_TIME_FrequencyGet() / 1000ULL;
    }
    now = mqtt_now();

    switch (s_state) {
    case MQTT_ST_IDLE:
        if (s_broker_configured && (int64_t)(now - s_deadline) >= 0) {
            mqtt_attempt_connect();
        }
        break;

    case MQTT_ST_WAIT_TLS:
        /* Deliberately NOT checking NET_PRES_SocketWasReset()/
         * WasDisconnected() here while still waiting - confirmed live
         * 2026-09-07 (tcp.c's own SYS_CONSOLE_PRINT trace + a tshark
         * capture on the peer) that this board's SYN and the peer's
         * SYN-ACK both arrive correctly, yet WasReset()/WasDisconnected()
         * already reported true on this branch's very first check, before
         * the handshake had any real chance to complete - clearing it once
         * right after Connect() did not stop it recurring. Matches
         * cert_provision.c's own CP_WAIT_CONN, which never checks these
         * flags while still waiting either, only once actually connected
         * (to clear a stale flag - see below) or via the deadline. */
        if (NET_PRES_SocketIsConnected(s_sock) && NET_PRES_SocketIsSecure(s_sock)) {
            /* Same reasoning as cert_provision.c's CP_WAIT_CONN: the stack
             * sets the reset flag as a side effect of the handshake itself,
             * not just on a genuine reset - clear it before trusting the
             * connection is actually healthy. */
            (void)NET_PRES_SocketWasReset(s_sock);
            mqtt_send_connect();
            s_state = MQTT_ST_WAIT_CONNACK;
            s_deadline = now + (uint64_t)MQTT_CONNACK_TIMEOUT_MS * s_ticks_per_ms;
        } else if ((int64_t)(now - s_deadline) >= 0) {
            char reason[64];
            /* err==0 here means the deadline fired while still WANT_READ/
             * WRITE - no TLS byte was ever exchanged, i.e. this is a
             * network/routing problem, not a cert/handshake rejection. */
            (void)snprintf(reason, sizeof reason,
                            "tcp/tls timeout, wolfSSL err=%d", EncGlueClient_LastError());
            mqtt_fail(reason);
        }
        break;

    case MQTT_ST_WAIT_CONNACK: {
        int rc;
        if (NET_PRES_SocketWasReset(s_sock) || NET_PRES_SocketWasDisconnected(s_sock)) {
            mqtt_fail("connection lost while waiting for CONNACK");
            break;
        }
        rc = MQTTPacket_readnb(s_rx_buf, (int)sizeof s_rx_buf, &s_transport);
        if (rc == CONNACK) {
            unsigned char sessionPresent = 0u, connack_rc = 0xFFu;
            if ((MQTTDeserialize_connack(&sessionPresent, &connack_rc, s_rx_buf,
                     (int)sizeof s_rx_buf) == 1) && (connack_rc == 0u)) {
                s_state = MQTT_ST_CONNECTED;
                s_deadline = now;   /* publish right away, then every interval */
                s_last_activity = now;
                SYS_CONSOLE_PRINT("MQTT: connected as %s\r\n", s_client_id);
            } else {
                mqtt_fail("broker rejected CONNECT (bad CONNACK)");
            }
        } else if (rc == -1) {
            mqtt_fail("CONNACK read error");
        } else if ((int64_t)(now - s_deadline) >= 0) {
            mqtt_fail("CONNACK timeout");
        }
        break;
    }

    case MQTT_ST_CONNECTED:
        if (NET_PRES_SocketWasReset(s_sock) || NET_PRES_SocketWasDisconnected(s_sock)) {
            mqtt_fail("broker connection lost");
            break;
        }
        if ((int64_t)(now - s_deadline) >= 0) {
            mqtt_publish_status();
            s_deadline = now + (uint64_t)MQTT_PUBLISH_INTERVAL_MS * s_ticks_per_ms;
        }
        /* MQTT_PUBLISH_INTERVAL_MS (5s) is always well under
         * MQTT_KEEPALIVE_S (60s), so this should never actually fire in
         * normal operation - kept anyway as a correctness backstop (e.g. a
         * publish that silently failed to queue) rather than relying
         * entirely on the broker's own keepalive grace. */
        else if ((now - s_last_activity) >=
                 (uint64_t)(MQTT_KEEPALIVE_S * 1000u / 2u) * s_ticks_per_ms) {
            mqtt_send_pingreq();
        }
        break;

    default:
        break;
    }
}
