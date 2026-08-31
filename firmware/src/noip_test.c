/*******************************************************************************
  Raw Ethernet frame test on eth0, bypassing the TCP/IP stack

  File Name:
    noip_test.c

  Summary:
    Implementation of the NoIP raw-frame test described in noip_test.h.

  Description:
    The transmit path is deliberately blunt: one static 60-byte buffer, filled
    once per command with a broadcast destination, this interface's own MAC as
    source, EtherType 0x88B5 and a 0xAA fill, then handed to
    DRV_LAN865X_SendRawEthFrame() once per frame with only the sequence number
    changing. 60 bytes is the minimum legal Ethernet frame, so nothing pads it
    behind our back and what the oscilloscope shows is what is written here.

    The gap between frames is a busy-wait on the SYS_TIME counter rather than a
    timer callback. For a diagnostic command issued from the console that is the
    honest trade: it blocks the caller for the requested time, which is exactly
    what "send 20 frames 50 ms apart" is asking for, and it keeps this module
    free of any scheduler state.
 *******************************************************************************/

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>                                          /* strtoul() */
#include <string.h>                                          /* memcpy/memset */

#include "definitions.h"
#include "config/default/system/console/sys_console.h"
#include "config/default/library/tcpip/tcpip.h"
#include "config/default/library/tcpip/src/tcpip_private.h"
#define TCPIP_THIS_MODULE_ID    TCPIP_MODULE_MANAGER
#include "config/default/library/tcpip/src/tcpip_packet.h"
#include "config/default/system/time/sys_time.h"
#include "config/default/driver/lan865x/drv_lan865x.h"
#include "system/command/sys_command.h"
#include "tcpip_manager_control.h"                           /* TCPIP_NET_IF, ->hIfMac */
#include "noip_test.h"
#include "cmd_print.h"                                        /* CMD_PRINT/CMD_MSG - reply to pCmdIO, not always the serial console */

/* Interface the frames go out of: 0 = eth0, the 10BASE-T1S MAC-PHY. */
#define NOIP_IF          0u

#define NOIP_FRAME_LEN   60u     /* default/minimum legal Ethernet frame length */
#define NOIP_MAX_FRAME_LEN 1518u /* standard max Ethernet frame (no FCS) - see the
                                   * optional [size] arg of noip_send, added for the
                                   * sniffer/mirror large-frame investigation
                                   * (BANDWIDTH/docs/FALLSTRICKE.md, 2026-08-27): lets a
                                   * follower push a controlled-size, controlled-rate
                                   * raw frame straight at the bridge's real RX mirror
                                   * path (MIRROR_Eth0Rx()), instead of relying on
                                   * iperf's own pacing/segmentation. */
#define NOIP_MAX_COUNT   1000u
#define NOIP_MAX_GAP_MS  1000u

static uint32_t s_tx_cnt = 0u;
static uint32_t s_rx_cnt = 0u;
static uint8_t  s_frame[NOIP_MAX_FRAME_LEN];

uint32_t NOIP_TxCount(void) { return s_tx_cnt; }
uint32_t NOIP_RxCount(void) { return s_rx_cnt; }

bool NOIP_IsNoIpFrame(uint16_t frameType) {
    return (frameType == (uint16_t)NOIP_ETHERTYPE);
}

uint32_t NOIP_CountRx(void) {
    s_rx_cnt++;
    return s_rx_cnt;
}

uint32_t NOIP_SeqFromFrame(const uint8_t *frame) {
    if (frame == NULL) {
        return 0u;
    }
    /* Sequence number occupies the first four payload bytes, i.e. right after
     * the 14-byte Ethernet header. */
    return ((uint32_t)frame[14] << 24) | ((uint32_t)frame[15] << 16)
         | ((uint32_t)frame[16] <<  8) |  (uint32_t)frame[17];
}

void NOIP_PrintRxLine(uint32_t index, uint32_t seq, const uint8_t *mac_src,
                      uint16_t length, uint64_t ts_ms) {
    static const uint8_t zero_mac[6] = {0};
    const uint8_t *m = (mac_src != NULL) ? mac_src : zero_mac;
    SYS_CONSOLE_PRINT("[NoIP-RX] #%u seq=%u from %02X:%02X:%02X:%02X:%02X:%02X len=%d ts=%llu ms\r\n",
                      (unsigned)index, (unsigned)seq,
                      m[0], m[1], m[2], m[3], m[4], m[5],
                      (int)length, (unsigned long long)ts_ms);
}

/* Blocking millisecond wait on the SYS_TIME counter - see the file header for
 * why a busy-wait is the right shape here. */
static void noip_wait_ms(uint32_t ms)
{
    uint64_t start = SYS_TIME_Counter64Get();
    uint64_t ticks = ((uint64_t)SYS_TIME_FrequencyGet() * (uint64_t)ms) / 1000ULL;
    while ((SYS_TIME_Counter64Get() - start) < ticks) {
    }
}

/* TCPIP_PKT_PacketAlloc() does NOT set ackFunc (same reasoning as
 * port_mirror.c's bigframe_pkt_ack()) - without one, DRV_LAN865X_PacketTx()
 * never frees the packet once it is done with it. */
static void noip_pkt_ack(TCPIP_MAC_PACKET *pkt, const void *param) {
    (void) param;
    TCPIP_PKT_PacketFree(pkt);
}

/* Send one raw Ethernet frame straight out eth0 via DRV_LAN865X_PacketTx() -
 * this project's LAN865x driver (newer package than the sister project's)
 * does not export a DRV_LAN865X_SendRawEthFrame() convenience wrapper (that
 * function is itself a hand-patch to the sister project's driver, not a
 * stock Harmony API - see CLAUDE.md section 3), so this reimplements the
 * same raw-TX behavior using the stock allocate/submit path, the same
 * pattern port_mirror.c's cmd_bigframe() already uses for eth1/GMAC.
 * Returns true if the frame was accepted for transmission. */
static bool noip_send_raw_frame(const uint8_t *frame, uint16_t len)
{
    TCPIP_MAC_PACKET *pTx;
    TCPIP_NET_HANDLE  eth0;

    eth0 = TCPIP_STACK_IndexToNet(NOIP_IF);
    if (eth0 == NULL) {
        return false;
    }

    pTx = TCPIP_PKT_PacketAlloc(sizeof(TCPIP_MAC_PACKET), len, 0);
    if (pTx == NULL) {
        return false;
    }

    pTx->pMacLayer = pTx->pDSeg->segLoad;
    memcpy(pTx->pMacLayer, frame, len);
    pTx->pDSeg->segLen = len;
    pTx->ackFunc  = noip_pkt_ack;
    pTx->ackParam = NULL;

    return (TCPIP_MAC_RES_OK == DRV_LAN865X_PacketTx(((TCPIP_NET_IF*)eth0)->hIfMac, pTx));
}

/* noip_send <n> [gap_ms] [size]  - send N raw Ethernet frames (EtherType
 * 0x88B5) on eth0/T1S. size (total frame length, no FCS) defaults to
 * NOIP_FRAME_LEN (60, the Ethernet minimum) and is clamped to
 * 60..NOIP_MAX_FRAME_LEN - lets a follower push controlled-size,
 * controlled-rate frames straight at another node's real RX path. */
static void cmd_noip_send(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    uint32_t count = 5u;
    uint32_t gap_ms = 0u;
    uint32_t size = NOIP_FRAME_LEN;
    if (argc >= 2) { count = (uint32_t)strtoul(argv[1], NULL, 10); }
    if (argc >= 3) { gap_ms = (uint32_t)strtoul(argv[2], NULL, 10); }
    if (argc >= 4) { size = (uint32_t)strtoul(argv[3], NULL, 10); }
    if (count == 0u || count > NOIP_MAX_COUNT) {
        CMD_PRINT(pCmdIO, "[NoIP] count must be 1..%u\r\n", (unsigned)NOIP_MAX_COUNT);
        return;
    }
    if (gap_ms > NOIP_MAX_GAP_MS) {
        CMD_PRINT(pCmdIO, "[NoIP] gap_ms must be 0..%u\r\n", (unsigned)NOIP_MAX_GAP_MS);
        return;
    }
    if (size < NOIP_FRAME_LEN || size > NOIP_MAX_FRAME_LEN) {
        CMD_PRINT(pCmdIO, "[NoIP] size must be %u..%u\r\n", (unsigned)NOIP_FRAME_LEN, (unsigned)NOIP_MAX_FRAME_LEN);
        return;
    }

    CMD_PRINT(pCmdIO, "[NoIP-TX] start count=%u gap_ms=%u size=%u\r\n",
        (unsigned)count, (unsigned)gap_ms, (unsigned)size);

    /* Get our MAC from the T1S interface (index 0 = eth0) */
    TCPIP_NET_HANDLE netH = TCPIP_STACK_IndexToNet(NOIP_IF);
    const uint8_t  *pMac  = TCPIP_STACK_NetAddressMac(netH);

    /* DST: Layer-2 broadcast */
    memset(&s_frame[0], 0xFFu, 6u);
    /* SRC: our MAC */
    if (pMac != NULL) { memcpy(&s_frame[6], pMac, 6u); }
    else              { memset(&s_frame[6], 0u,   6u); }
    /* EtherType 0x88B5 */
    s_frame[12] = (uint8_t)((NOIP_ETHERTYPE >> 8u) & 0xFFu);
    s_frame[13] = (uint8_t)( NOIP_ETHERTYPE        & 0xFFu);
    /* Payload: 4-byte sequence + fill to reach the requested frame size */
    memset(&s_frame[14], 0xAAu, size - 14u);

    uint32_t i;
    for (i = 0u; i < count; i++) {
        s_tx_cnt++;
        s_frame[14] = (uint8_t)((s_tx_cnt >> 24u) & 0xFFu);
        s_frame[15] = (uint8_t)((s_tx_cnt >> 16u) & 0xFFu);
        s_frame[16] = (uint8_t)((s_tx_cnt >>  8u) & 0xFFu);
        s_frame[17] = (uint8_t)( s_tx_cnt         & 0xFFu);
        if (!noip_send_raw_frame(s_frame, (uint16_t)size)) {
            CMD_PRINT(pCmdIO, "[NoIP-TX] send failed at seq=%u\r\n", (unsigned)s_tx_cnt);
            s_tx_cnt--;
            break;
        }
        CMD_PRINT(pCmdIO, "[NoIP-TX] sent seq=%u\r\n", (unsigned)s_tx_cnt);
        if (gap_ms > 0u) {
            noip_wait_ms(gap_ms);
        }
    }
}

static void cmd_noip_stat(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    CMD_PRINT(pCmdIO, "[NoIP] TX=%u  RX=%u\r\n", (unsigned)s_tx_cnt, (unsigned)s_rx_cnt);
}

static const SYS_CMD_DESCRIPTOR noip_cmd_tbl[] = {
    {"noip_send", (SYS_CMD_FNC) cmd_noip_send, ": send N raw Ethernet frames bypassing TCP stack (noip_send <n> [gap_ms])"},
    {"noip_stat", (SYS_CMD_FNC) cmd_noip_stat, ": show NoIP TX/RX counters"},
};

void NOIP_Initialize(void) {
    if (!SYS_CMD_ADDGRP(noip_cmd_tbl, (int)(sizeof noip_cmd_tbl / sizeof *noip_cmd_tbl),
                        "noip", ": raw Ethernet frame test (EtherType 0x88B5)")) {
        SYS_CONSOLE_PRINT("NOIP: SYS_CMD_ADDGRP failed\n\r");
    }
}
