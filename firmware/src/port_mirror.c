/*******************************************************************************
  eth0 (10BASE-T1S) -> eth1 (100BASE-T) port mirror / SPAN

  File Name:
    port_mirror.c

  Summary:
    Implementation of the port mirror described in port_mirror.h.

  Description:
    Both mirror directions funnel into mirror_ethpkt_to_eth1(), which allocates a
    stack packet, copies the complete Ethernet frame into it and hands it to the
    GMAC. The own-MAC filters live in the two entry points, because the two
    directions look at opposite ends of the frame: the RX path compares the
    DESTINATION MAC (frames addressed to the bridge), the TX path the SOURCE MAC
    (frames the bridge originated).

    Mirror copies are best-effort: if the packet pool is busy the copy is dropped
    rather than blocking or retrying. A dropped mirror frame costs a gap in the
    capture, never a stalled data path.
 *******************************************************************************/

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>                                          /* strtoul() */
#include <string.h>                                          /* memcpy/memcmp */

#include "definitions.h"
#include "config/default/system/console/sys_console.h"
#include "config/default/library/tcpip/tcpip.h"
#include "config/default/library/tcpip/src/tcpip_private.h"
#define TCPIP_THIS_MODULE_ID    TCPIP_MODULE_MANAGER
#include "config/default/library/tcpip/src/tcpip_packet.h"
#include "config/default/library/tcpip/src/link_list.h"      /* PROTECTED_SINGLE_LIST for the mirror pool */
#include "config/default/driver/gmac/drv_gmac.h"
#include "system/command/sys_command.h"
#include "tcpip_manager_control.h"                           /* TCPIP_NET_IF */
#include "port_mirror.h"
#include "env.h"                                             /* env_mirror(): persisted start state */
#include "lan865x_diag.h"                                    /* LAN865X_DIAG_Rmw(): T1SPMACTL.TXD    */
#include "cmd_print.h"                                        /* CMD_PRINT/CMD_MSG - reply to pCmdIO, not always the serial console */

/* Interface indices: source of the mirrored traffic, and where the copies go. */
#define MIRROR_SRC_IF   0u    /* eth0, the 10BASE-T1S MAC-PHY */
#define MIRROR_DST_IF   1u    /* eth1, the 100BASE-T GMAC     */

#define MIRROR_MAX_FRAME  1518u

/* Mitigation, not a bridge-side bug fix (investigated 2026-08-27):
 * frames mirrored to eth1 above this length made the PC's own
 * USB-Ethernet adapter/Npcap capture silently stop receiving anything for a
 * while (no Windows PnP/link event, no error anywhere on the bridge itself -
 * ack_ok/ack_fail proved the GMAC always completed the TX). 1514 = the
 * standard max Ethernet frame without FCS (14-byte header + 1500 payload) -
 * confirmed by bisection as the exact still-good TOTAL FRAME length (not
 * payload: iperf -u -l 1468 -> 1514-byte frame passes, -l 1469 -> 1515-byte
 * frame fails; the raw noip_send path bisects to the identical 1514/1515
 * boundary directly in total frame length). Since the mirror is a
 * diagnostic tap, not part of the real data path, truncating is safe: the
 * real T1S traffic between the actual endpoints is untouched, only what
 * reaches Wireshark is capped. */
#define MIRROR_SAFE_FRAME_LEN  1514u

/* Own small, fixed-size packet pool instead of TCPIP_PKT_PacketAlloc()'ing
 * straight from the general TCPIP heap on every frame. Root-caused
 * empirically (docs/FALLSTRICKE.md, 2026-08-27): at iperf-rate T1S traffic
 * (~500 fps) the shared heap gets exhausted by fragmentation long before its
 * nominal ~65 KB capacity is used up - many differently-sized, differently-
 * lived allocations (TCP, DHCP, ARP, ... and now mirror copies) compete for
 * the same heap, and TCPIP_PKT_PacketAlloc() returns NULL well before that
 * capacity is reached. A pool of same-sized, pre-allocated buffers cannot
 * fragment: freeing one always makes room for exactly one more allocation.
 * Same pattern already proven in this codebase for exactly this reason -
 * TCPIP_MAC_BRIDGE_PACKET_POOL_SIZE (8) for the bridge's own forwarding path,
 * and DRV_LAN865X_SetMacCtrlInfo()'s rxFreePackets for eth0 RX buffers. Sized
 * the same (8) as the bridge's own pool, which already handles this traffic
 * rate on the forwarding path today.
 *
 * TCPIP_MAC_PKT_FLAG_STATIC does NOT protect a packet from
 * TCPIP_PKT_PacketFree() - _TCPIP_PKT_PacketAllocInt() strips that flag
 * unconditionally at allocation time (tcpip_packet.c). The only actual
 * protection is discipline: never call TCPIP_PKT_PacketFree() on a pool
 * packet, only ever recycle it through mirror_pkt_ack() back onto
 * s_mirror_free_pkts - the same contract DRV_LAN865X_SetMacCtrlInfo()'s
 * _RxPacketAck() already relies on for its own static RX packets. */
#define MIRROR_POOL_SIZE  8u

static bool s_mirror_on = false;
static bool s_sniffer_on = false;
static PROTECTED_SINGLE_LIST s_mirror_free_pkts;

/* Debug counters - temporary, to find exactly where frames are lost between
 * "eth0 received it" and "eth1 transmitted it". Printed by 'mirror'/'sniffer'
 * with no argument. */
static uint32_t s_dbg_rx_hook_calls = 0u;    /* MIRROR_Eth0Rx() entered with mirror/sniffer on */
static uint32_t s_dbg_rx_passed_filter = 0u; /* survived the dest-MAC filter (or sniffer skipped it) */
static uint32_t s_dbg_pool_empty = 0u;       /* s_mirror_free_pkts was empty */
static uint32_t s_dbg_no_eth1 = 0u;          /* TCPIP_STACK_IndexToNet(MIRROR_DST_IF) was NULL */
static uint32_t s_dbg_tx_submitted = 0u;     /* handed to DRV_GMAC_PacketTx */
/* Added 2026-08-27: tx_submitted only proves the
 * frame was HANDED to DRV_GMAC_PacketTx(), not that the GMAC actually
 * finished sending it - the whole point of this investigation is that the
 * firmware side looks "fine" while the PC never receives large frames.
 * pkt->ackRes is what the MAC driver itself sets before calling ackFunc on
 * TX completion (TCPIP_MAC_PKT_ACK_TX_OK vs anything else, see
 * tcpip_mac.h) - a real, driver-reported completion status instead of just
 * "we called the API". Tracked separately from the mirror's normal
 * best-effort drop counters above. */
static uint32_t s_dbg_ack_ok = 0u;           /* ackFunc fired with TCPIP_MAC_PKT_ACK_TX_OK */
static uint32_t s_dbg_ack_fail = 0u;         /* ackFunc fired with anything else (real TX attempted) */
static int8_t   s_dbg_last_ack_res = 0;      /* last non-OK ackRes seen, for diagnosis */
static uint16_t s_dbg_max_len_submitted = 0u;/* largest frame length ever handed to DRV_GMAC_PacketTx */
static uint16_t s_dbg_max_len_ok = 0u;       /* largest frame length that got TCPIP_MAC_PKT_ACK_TX_OK */
static uint32_t s_dbg_truncated = 0u;        /* frames cut down to MIRROR_SAFE_FRAME_LEN before mirroring */

bool MIRROR_IsEnabled(void)   { return s_mirror_on; }
void MIRROR_Set(bool enable)  { s_mirror_on = enable; }

bool SNIFFER_IsEnabled(void)  { return s_sniffer_on; }

/* --- sniffer: making the transmitter state provable, not merely requested ---
 * sniffer's entire purpose is electrical silence on the T1S bus, and that rests
 * on a single bit: T1SPMACTL.TXD. LAN865X_DIAG_Rmw() is asynchronous and
 * REJECTS a request outright while the diag state machine is busy (one slot, no
 * queue). The original fire-and-forget call therefore had exactly the failure
 * mode a passive tap must not have: 'sniffer 1' answered ON while the
 * transmitter kept running, and nothing on the console said otherwise.
 *
 * The write is now retried from MIRROR_Tasks() until the diag module accepts it
 * AND its readback confirms the bit, or until SNIFFER_TXD_TIMEOUT_MS has passed
 * - in which case it reports a hard error instead of staying silent about it.
 * s_txd_confirmed is what SNIFFER_TxdConfirmed() exposes and what the 'sniffer'
 * command prints; s_sniffer_on on its own only ever meant "requested". */
#define SNIFFER_TXD_TIMEOUT_MS  3000u

static bool     s_txd_want      = false; /* TXD state the requested mode needs        */
static bool     s_txd_req       = false; /* a write is outstanding (retry or awaited) */
static bool     s_txd_issued    = false; /* Rmw accepted, its readback still pending  */
static bool     s_txd_confirmed = true;  /* readback proved it. True at boot: TXD is
                                            clear out of reset and sniffer starts off */
static uint64_t s_txd_deadline  = 0u;
static SYS_CMD_DEVICE_NODE *s_sniffer_pCmdIO = NULL; /* console that asked, so the
                                            deferred result goes back to it */

static void sniffer_txd_try(void)
{
    if (LAN865X_DIAG_Busy()) return;                  /* retry next iteration */
    if (LAN865X_DIAG_Rmw(LAN865X_T1SPMACTL, LAN865X_PMACTL_TXD,
                         s_txd_want ? LAN865X_PMACTL_TXD : 0u)) {
        s_txd_issued = true;
    }
}

bool SNIFFER_TxdConfirmed(void) { return s_txd_confirmed; }

void MIRROR_Tasks(void)
{
    if (!s_txd_req) return;

    if (s_txd_issued) {
        LAN865X_VERIFY_RESULT r = LAN865X_DIAG_VerifyResult();
        if (r == LAN865X_VERIFY_PASS) {
            s_txd_confirmed = true;
            s_txd_req       = false;
            CMD_PRINT_OR_CONSOLE(s_sniffer_pCmdIO,
                "[SNIFFER] T1S transmitter %s - CONFIRMED by readback of T1SPMACTL.TXD\n\r",
                s_txd_want ? "disabled" : "enabled");
            return;
        }
        if (r != LAN865X_VERIFY_FAIL) return;          /* still pending - wait */
        s_txd_issued = false;                          /* failed - retry below */
    }

    if (SYS_TIME_Counter64Get() >= s_txd_deadline) {
        s_txd_req = false;
        CMD_PRINT_OR_CONSOLE(s_sniffer_pCmdIO,
            "[SNIFFER] ERROR: could not %s the T1S transmitter (T1SPMACTL.TXD).\n\r",
            s_txd_want ? "disable" : "enable");
        CMD_PRINT_OR_CONSOLE(s_sniffer_pCmdIO,
            "          The bridge is NOT known to be silent - check with 'lan_read 0x000308F9'\n\r");
        CMD_PRINT_OR_CONSOLE(s_sniffer_pCmdIO,
            "          (TXD is bit 14, mask 0x00004000) and repeat the command.\n\r");
        return;
    }

    sniffer_txd_try();
}

/* Passive tap: sniffer also disables the LAN8651's own transmitter
 * (T1SPMACTL.TXD) while it is on, so the bridge never talks on the bus
 * itself - invisible to the other nodes, listen-only. TXD needs no PMA
 * reset to toggle back (unlike LBE/loopback, see CLAUDE.md section 4), so a
 * plain RMW both ways is enough. Side effect the caller must know: normal
 * PC<->T1S forwarding (and the bridge's own ARP/ping/iperf) stops working
 * for as long as sniffer is on, since eth0 TX is physically disabled.
 * Fire-and-forget: LAN865X_DIAG_Rmw() is async (see lan865x_diag.h) and
 * reports its own result on the console; a rejection ("previous operation
 * still in progress") just leaves TXD as it was, same as any other
 * lan865x_diag command. */
void SNIFFER_Set(bool enable) {
    s_sniffer_on    = enable;      /* RX mirroring may follow the request at once */
    s_txd_want      = enable;
    s_txd_req       = true;        /* ... but the transmitter bit has to be proven */
    s_txd_issued    = false;
    s_txd_confirmed = false;
    s_txd_deadline  = SYS_TIME_Counter64Get()
                    + ((uint64_t)SYS_TIME_FrequencyGet() * (uint64_t)SNIFFER_TXD_TIMEOUT_MS) / 1000ULL;
    sniffer_txd_try();             /* first attempt now, the rest in MIRROR_Tasks() */
}

/* Recycle a sent (or never-submitted) pool packet - never TCPIP_PKT_PacketFree()
 * it, see the pool comment above. Also the one place that can tell a real
 * driver-confirmed TX success from "we called the API" - see the counter
 * comments above. TCPIP_MAC_PKT_ACK_NONE (0) means this packet never
 * actually went through DRV_GMAC_PacketTx() (the "no eth1" fallback in
 * mirror_ethpkt_to_eth1() recycles it directly) - not counted either way. */
static void mirror_pkt_ack(TCPIP_MAC_PACKET *pkt, const void *param)
{
    (void)param;
    if (pkt->ackRes == TCPIP_MAC_PKT_ACK_TX_OK) {
        s_dbg_ack_ok++;
        if (pkt->pDSeg->segLen > s_dbg_max_len_ok) { s_dbg_max_len_ok = pkt->pDSeg->segLen; }
    } else if (pkt->ackRes != TCPIP_MAC_PKT_ACK_NONE) {
        s_dbg_ack_fail++;
        s_dbg_last_ack_res = pkt->ackRes;
    }
    TCPIP_Helper_ProtSglListTailAdd(&s_mirror_free_pkts, (SGL_LIST_NODE*) pkt);
}

/* Pre-allocate the pool once, from the TCPIP heap while it still has room to
 * spare (called from MIRROR_Initialize(), well after TCPIP_STACK_Init() has
 * set the heap up - see initialization.c). A short pool at startup is not
 * fatal - mirror_ethpkt_to_eth1() already treats "pool empty" as a normal
 * drop, same as it always has. */
static void mirror_pool_init(void)
{
    uint8_t i;
    TCPIP_MAC_PACKET *pkt;

    (void)TCPIP_Helper_ProtSglListInitialize(&s_mirror_free_pkts);
    for (i = 0u; i < MIRROR_POOL_SIZE; i++) {
        pkt = TCPIP_PKT_PacketAlloc(sizeof(TCPIP_MAC_PACKET), MIRROR_MAX_FRAME, TCPIP_MAC_PKT_FLAG_STATIC);
        if (pkt == NULL) {
            SYS_CONSOLE_PRINT("MIRROR: pool init got only %u/%u buffers\n\r", (unsigned)i, (unsigned)MIRROR_POOL_SIZE);
            break;
        }
        pkt->ackFunc = mirror_pkt_ack;
        pkt->ackParam = NULL;
        TCPIP_Helper_ProtSglListTailAdd(&s_mirror_free_pkts, (SGL_LIST_NODE*) pkt);
    }
}

/* eth0 (T1S) interface MAC - the filter reference for both mirror directions. */
static const uint8_t *eth0_own_mac(void)
{
    TCPIP_NET_HANDLE eth0 = TCPIP_STACK_IndexToNet(MIRROR_SRC_IF);
    return (eth0 != NULL) ? TCPIP_STACK_NetAddressMac(eth0) : NULL;
}

/* Clone a complete Ethernet frame onto eth1 for the PC-side capture. The caller
 * has already applied the own-MAC filter. Single-segment copy (bridge/stack
 * frames are single-segment); empty frames are dropped, oversize ones
 * (> MIRROR_MAX_FRAME, should not happen on a real Ethernet segment) too.
 * Frames above MIRROR_SAFE_FRAME_LEN but still <= MIRROR_MAX_FRAME are
 * TRUNCATED, not dropped - see the constant's comment: full-size frames
 * reliably wedge the PC-side USB-Ethernet capture, a snaplen-style cut
 * (header + start of payload still visible in Wireshark) avoids that while
 * leaving the real T1S traffic between the actual endpoints untouched -
 * this is a diagnostic tap, not part of the data path. */
static void mirror_ethpkt_to_eth1(const uint8_t *frame, uint16_t flen)
{
    TCPIP_MAC_PACKET *pTx;
    TCPIP_NET_HANDLE  eth1;

    if (frame == NULL || flen == 0u || flen > MIRROR_MAX_FRAME) return;
    if (flen > MIRROR_SAFE_FRAME_LEN) {
        flen = MIRROR_SAFE_FRAME_LEN;
        s_dbg_truncated++;
    }
    pTx = (TCPIP_MAC_PACKET*) TCPIP_Helper_ProtSglListHeadRemove(&s_mirror_free_pkts);
    if (pTx == NULL) { s_dbg_pool_empty++; return; }  /* pool empty right now: drop the mirror copy */

    pTx->pMacLayer = pTx->pDSeg->segLoad;
    memcpy(pTx->pMacLayer, frame, flen);             /* full Ethernet frame (header + payload) */
    pTx->pDSeg->segLen = flen;
    pTx->pNetLayer = pTx->pMacLayer + sizeof(TCPIP_MAC_ETHERNET_HEADER);
    pTx->ackFunc   = mirror_pkt_ack;                 /* recycled back onto the pool after TX, never freed */
    pTx->ackParam  = NULL;
    pTx->ackRes    = TCPIP_MAC_PKT_ACK_NONE;         /* cleared so a stale value from this slot's
                                                       * previous use can't be misread as a real
                                                       * completion by mirror_pkt_ack() */

    eth1 = TCPIP_STACK_IndexToNet(MIRROR_DST_IF);
    if (eth1 != NULL) {
        s_dbg_tx_submitted++;
        if (flen > s_dbg_max_len_submitted) { s_dbg_max_len_submitted = flen; }
        (void)DRV_GMAC_PacketTx(((TCPIP_NET_IF*)eth1)->hIfMac, pTx);
    } else {
        /* No eth1 right now - DRV_GMAC_PacketTx() never got it, so its ackFunc
         * never fires on its own; recycle it back to the pool ourselves. */
        s_dbg_no_eth1++;
        mirror_pkt_ack(pTx, NULL);
    }
}

/* RX mirror: a frame just arrived on eth0. With sniffer off (the original
 * behaviour), mirror it only if it is addressed to the bridge itself (dst MAC
 * == eth0 MAC) - PC-bound unicast and broadcast/multicast are forwarded to
 * eth1 by the MAC bridge already, so mirroring them would duplicate them at
 * the PC. With sniffer on, that destination filter is skipped entirely: every
 * frame eth0 receives is mirrored, including traffic between two OTHER nodes
 * that never involves the bridge - see the sniffer description in
 * port_mirror.h for why that traffic reaches this hook at all. */
void MIRROR_Eth0Rx(TCPIP_MAC_PACKET *rxPkt)
{
    const uint8_t *frame;
    const uint8_t *mac;

    if (!s_mirror_on && !s_sniffer_on) return;
    s_dbg_rx_hook_calls++;
    if (rxPkt == NULL || rxPkt->pDSeg == NULL) return;
    frame = rxPkt->pMacLayer;
    if (frame == NULL) return;
    if (!s_sniffer_on) {
        mac = eth0_own_mac();
        if (mac == NULL) return;
        if (memcmp(frame, mac, 6) != 0) return;      /* dst MAC != eth0 -> not for us, skip */
    }
    s_dbg_rx_passed_filter++;
    /* rxPkt->pDSeg->segLen is NOT the full frame length here: the generic stack RX
     * path (tcpip_manager.c, TCPIP_STACK_ProcessRxPkt-equivalent) already subtracted
     * sizeof(TCPIP_MAC_ETHERNET_HEADER) from it before this hook ever runs, per the
     * documented RX contract in tcpip_mac.h ("segLen is updated by each stack layer
     * in turn") - by this point it means "payload after the MAC header", not "bytes
     * starting at pMacLayer". frame == pMacLayer still points at the frame's start
     * (header included), so the byte count actually worth copying from there is
     * segLen + the header this hook's own copy needs to include.
     * Root-caused 2026-08-31 (see docs/session-log.md): using segLen directly here
     * made every mirrored/sniffed frame exactly sizeof(TCPIP_MAC_ETHERNET_HEADER)
     * (14) bytes short - invisible on frames under MIRROR_SAFE_FRAME_LEN's later
     * clamp only by luck, but real, reproducible payload truncation on anything
     * that needed more than one TC6 SPI chunk (confirmed via tshark: a 1514-byte
     * frame arrived at the PC as 1504 bytes, corrupting Wireshark's TCP reassembly
     * with spurious "Previous segment not captured" on every large frame). */
    mirror_ethpkt_to_eth1(frame, (uint16_t)(rxPkt->pDSeg->segLen + sizeof(TCPIP_MAC_ETHERNET_HEADER)));
}

/* TX mirror: called from DRV_LAN865X_PacketTx (the single eth0 egress point) for
 * every frame about to leave on eth0. Mirror it only if the bridge ITSELF
 * originated it (src MAC == eth0 MAC) - the firmware's own ping/ARP.
 * Frames forwarded from eth1 keep their original (PC) src MAC and are skipped;
 * the PC already has them. The driver transmits from pDSeg->segLoad.
 *
 * Name fixed by the driver patch - see the comment in port_mirror.h. */
void mirror_eth0_tx_hook(TCPIP_MAC_PACKET *txPkt)
{
    const uint8_t *frame;
    const uint8_t *mac;
    if (!s_mirror_on) return;
    if (txPkt == NULL || txPkt->pDSeg == NULL) return;
    frame = txPkt->pDSeg->segLoad;
    if (frame == NULL) return;
    mac = eth0_own_mac();
    if (mac == NULL) return;
    if (memcmp(frame + 6, mac, 6) != 0) return;      /* src MAC != eth0 -> forwarded, skip mirror */
    mirror_ethpkt_to_eth1(frame, txPkt->pDSeg->segLen);
}

/* Temporary debug helper - see the counter declarations above. */
static void mirror_print_dbg_counters(SYS_CMD_DEVICE_NODE* pCmdIO) {
    CMD_PRINT(pCmdIO, "  dbg: rx_hook=%lu passed_filter=%lu pool_empty=%lu no_eth1=%lu tx_submitted=%lu\n\r",
                       (unsigned long)s_dbg_rx_hook_calls, (unsigned long)s_dbg_rx_passed_filter,
                       (unsigned long)s_dbg_pool_empty, (unsigned long)s_dbg_no_eth1,
                       (unsigned long)s_dbg_tx_submitted);
    CMD_PRINT(pCmdIO, "  dbg: ack_ok=%lu ack_fail=%lu last_ack_res=%d max_len_submitted=%u max_len_ok=%u\n\r",
                       (unsigned long)s_dbg_ack_ok, (unsigned long)s_dbg_ack_fail,
                       (int)s_dbg_last_ack_res, (unsigned)s_dbg_max_len_submitted,
                       (unsigned)s_dbg_max_len_ok);
    CMD_PRINT(pCmdIO, "  dbg: truncated=%lu (frames cut to %u bytes before mirroring)\n\r",
                       (unsigned long)s_dbg_truncated, (unsigned)MIRROR_SAFE_FRAME_LEN);
}

/* mirror [0|1] - copy every eth0 (T1S) RX frame out eth1 so a PC on eth1 can
 * sniff the T1S bus with Wireshark (e.g. the endpoint's replies to a firmware
 * CLI command). No argument shows the current state. */
static void cmd_mirror(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc >= 2) {
        bool want = (strtoul(argv[1], NULL, 0) != 0u);
        /* mirror and sniffer are alternative capture modes, never a combination:
         * sniffer is the broader of the two on RX and disables the T1S
         * transmitter, which makes mirror's TX half meaningless. Refuse instead
         * of silently producing a mode nobody documented. */
        if (want && s_sniffer_on) {
            CMD_PRINT(pCmdIO, "Refused: sniffer is ON. mirror and sniffer are alternative capture modes.\n\r");
            CMD_PRINT(pCmdIO, "Run 'sniffer 0' first, then 'mirror 1'.\n\r");
            return;
        }
        s_mirror_on = want;
    }
    CMD_PRINT(pCmdIO, "eth0(T1S)->eth1 mirror: %s\n\r", s_mirror_on ? "ON" : "OFF");
    if (s_mirror_on) {
        CMD_PRINT(pCmdIO, "  Capture on the PC (eth1) in Wireshark to see the T1S bus traffic:\n\r");
        CMD_PRINT(pCmdIO, "  RX (endpoint -> bridge: replies/ARP) AND the bridge's own TX.\n\r");
    }
    mirror_print_dbg_counters(pCmdIO);
}

/* sniffer [0|1] - like 'mirror', but without the destination-MAC filter on RX:
 * every frame eth0 receives is copied to eth1, including traffic between two
 * OTHER nodes on the bus that never involves this bridge. Also disables the
 * LAN8651's own transmitter for as long as it is on (see SNIFFER_Set()) -
 * a passive tap, invisible on the bus, but normal PC<->T1S forwarding stops
 * working meanwhile. No argument shows the current state. */
static void cmd_sniffer(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    if (argc >= 2) {
        bool want = (strtoul(argv[1], NULL, 0) != 0u);
        if (want && s_mirror_on) {                 /* see the note in cmd_mirror() */
            CMD_PRINT(pCmdIO, "Refused: mirror is ON. mirror and sniffer are alternative capture modes.\n\r");
            CMD_PRINT(pCmdIO, "Run 'mirror 0' first, then 'sniffer 1'.\n\r");
            return;
        }
        s_sniffer_pCmdIO = pCmdIO;   /* the deferred confirmation goes back here */
        SNIFFER_Set(want);
    }
    CMD_PRINT(pCmdIO, "eth0(T1S)->eth1 sniffer: %s\n\r", s_sniffer_on ? "ON" : "OFF");
    /* The mode flag alone says nothing about the bus - report what the readback
     * actually proved about T1SPMACTL.TXD. */
    CMD_PRINT(pCmdIO, "  T1S transmitter: %s\n\r",
              s_txd_req       ? "not confirmed yet - verifying, watch for [SNIFFER]"
            : s_txd_confirmed ? (s_sniffer_on ? "disabled (confirmed by readback)"
                                              : "enabled (confirmed by readback)")
                              : "NOT CONFIRMED - check with 'lan_read 0x000308F9'");
    if (s_sniffer_on) {
        CMD_PRINT(pCmdIO, "  Capture on the PC (eth1) in Wireshark to see ALL T1S bus traffic,\n\r");
        CMD_PRINT(pCmdIO, "  including frames between other nodes that do not involve this bridge.\n\r");
        CMD_PRINT(pCmdIO, "  T1S transmitter disabled (passive tap) - forwarding is paused meanwhile.\n\r");
    }
    mirror_print_dbg_counters(pCmdIO);
}

/* bigframe <total_frame_len> - diagnostic only, unrelated to mirror/sniffer
 * as such: builds and sends ONE raw Ethernet frame of the given total length
 * (header+payload, no FCS - matches Wireshark's frame.len minus 4) straight
 * out eth1 via DRV_GMAC_PacketTx(), bypassing the TCPIP stack, T1S, mirror
 * and sniffer entirely. Sole purpose: isolate whether an oversized frame on
 * eth1 alone reproduces the PC-side USB-NIC "adapter no longer attached"
 * glitch found in this investigation (BANDWIDTH/sniffer sessions,
 * 2026-08-27) - independent of anything happening on the T1S side, so a
 * repro here rules T1S/mirror/iperf out entirely.
 * dst = broadcast, src = eth1's own MAC, EtherType 0xFEED (a deliberately
 * unregistered value, so the frame is unambiguous in a capture), payload =
 * an incrementing byte pattern so length/content are easy to verify. */
#define BIGFRAME_ETHERTYPE   0xFEEDu
#define BIGFRAME_MIN_LEN     60u
#define BIGFRAME_MAX_LEN     9000u

/* TCPIP_PKT_PacketAlloc() does NOT set ackFunc (verified in tcpip_packet.c -
 * both the debug and non-debug _TCPIP_PKT_PacketAllocInt() just memset the
 * packet to 0) - a NULL ackFunc means DRV_GMAC_PacketTx() never frees the
 * packet once it's done with it. Every bigframe call without this leaked
 * one packet permanently, which is why repeated calls eventually hit
 * "packet alloc failed" once the TCPIP heap ran dry. */
static void bigframe_pkt_ack(TCPIP_MAC_PACKET *pkt, const void *param) {
    (void) param;
    TCPIP_PKT_PacketFree(pkt);
}

static void cmd_bigframe(SYS_CMD_DEVICE_NODE* pCmdIO, int argc, char** argv) {
    uint32_t len;
    uint32_t i;
    uint8_t *frame;
    const uint8_t *srcMac;
    TCPIP_MAC_PACKET *pTx;
    TCPIP_NET_HANDLE  eth1;

    if (argc < 2) {
        CMD_PRINT(pCmdIO, "usage: bigframe <total_frame_len_bytes>  (%u..%u)\n\r",
            (unsigned)BIGFRAME_MIN_LEN, (unsigned)BIGFRAME_MAX_LEN);
        return;
    }
    len = strtoul(argv[1], NULL, 0);
    if (len < BIGFRAME_MIN_LEN || len > BIGFRAME_MAX_LEN) {
        CMD_PRINT(pCmdIO, "bigframe: length must be %u..%u\n\r",
            (unsigned)BIGFRAME_MIN_LEN, (unsigned)BIGFRAME_MAX_LEN);
        return;
    }

    eth1 = TCPIP_STACK_IndexToNet(MIRROR_DST_IF);
    if (eth1 == NULL) {
        CMD_PRINT(pCmdIO, "bigframe: eth1 not up\n\r");
        return;
    }

    pTx = TCPIP_PKT_PacketAlloc(sizeof(TCPIP_MAC_PACKET), (uint16_t)len, 0);
    if (pTx == NULL) {
        CMD_PRINT(pCmdIO, "bigframe: packet alloc failed\n\r");
        return;
    }

    pTx->pMacLayer = pTx->pDSeg->segLoad;
    frame = pTx->pMacLayer;
    memset(frame, 0xFFu, 6u);                        /* dst = broadcast */
    srcMac = TCPIP_STACK_NetAddressMac(eth1);
    if (srcMac != NULL) {
        memcpy(frame + 6, srcMac, 6u);
    }
    frame[12] = (uint8_t)(BIGFRAME_ETHERTYPE >> 8);
    frame[13] = (uint8_t)(BIGFRAME_ETHERTYPE & 0xFFu);
    for (i = 14u; i < len; i++) {
        frame[i] = (uint8_t)(i & 0xFFu);
    }
    pTx->pDSeg->segLen = (uint16_t)len;
    pTx->ackFunc  = bigframe_pkt_ack;
    pTx->ackParam = NULL;

    CMD_PRINT(pCmdIO, "bigframe: sending %u-byte raw frame on eth1 (dst=broadcast, ethertype=0x%04X)\n\r",
        (unsigned)len, (unsigned)BIGFRAME_ETHERTYPE);
    (void) DRV_GMAC_PacketTx(((TCPIP_NET_IF*)eth1)->hIfMac, pTx);
}

static const SYS_CMD_DESCRIPTOR mirror_cmd_tbl[] = {
    {"mirror", (SYS_CMD_FNC) cmd_mirror, ": mirror eth0(T1S) RX+TX to eth1 for Wireshark (mirror [0|1])"},
    {"sniffer", (SYS_CMD_FNC) cmd_sniffer, ": mirror ALL eth0(T1S) RX to eth1, not just this bridge's own traffic (sniffer [0|1])"},
    {"bigframe", (SYS_CMD_FNC) cmd_bigframe, ": send one raw oversized frame straight out eth1 (bigframe <total_len>)"},
};

void MIRROR_Initialize(void) {
    mirror_pool_init();

    if (!SYS_CMD_ADDGRP(mirror_cmd_tbl, (int)(sizeof mirror_cmd_tbl / sizeof *mirror_cmd_tbl),
                        "span", ": eth0->eth1 port mirror for Wireshark")) {
        SYS_CONSOLE_PRINT("MIRROR: SYS_CMD_ADDGRP failed\n\r");
    }

    /* Persisted start state ('setenv mirror 1' + 'saveenv'). Safe to set this early:
     * both hooks bail out while the stack is not up, because eth0_own_mac() returns
     * NULL until the interface exists. ENV_Init() runs in SYS_Initialize, well before
     * APP_Initialize gets here, so the value is loaded by now. */
    s_mirror_on = env_mirror();
    if (s_mirror_on) {
        SYS_CONSOLE_PRINT("MIRROR: eth0(T1S)->eth1 mirror enabled from env\n\r");
    }

    /* Same persisted-start-state idea for sniffer - but unlike mirror, this is a RAM
     * flag ONLY here: the actual hardware bit (T1SPMACTL.TXD) was already suppressed
     * inside the LAN865x driver's own init sequence (see initialization.c/
     * drv_lan865x_api.c, drvCfg.suppressTx), before NETWORK_CONTROL/TXEN was ever
     * written. Deliberately NOT calling SNIFFER_SetEnabled() here: that goes through
     * LAN865X_DIAG_Rmw(), which only works once the driver reaches SYS_STATUS_READY -
     * a second, redundant, and strictly later register write than what the driver
     * already did. This just makes the RX-hook filtering (mirror_pool.c) and
     * SNIFFER_IsEnabled()/showenv agree with what the hardware already is. */
    s_sniffer_on = env_sniffer();
    if (s_sniffer_on) {
        SYS_CONSOLE_PRINT("SNIFFER: eth0(T1S) transmitter suppressed from env (permanent sniffer)\n\r");
    }
}
