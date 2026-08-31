/*******************************************************************************
  Raw Ethernet frame test on eth0, bypassing the TCP/IP stack

  File Name:
    noip_test.h

  Summary:
    Sends and counts raw Ethernet frames with EtherType 0x88B5 straight through
    the LAN865x driver, with no IP, ARP, DHCP or socket involved.

  Description:
    "NoIP" is the deliberate absence of the IP stack. A frame goes from this
    module directly into DRV_LAN865X_SendRawEthFrame(), so what appears on the
    two-wire bus is exactly what was assembled here - fixed length, fixed
    payload, a monotonic sequence number, and a caller-chosen inter-frame gap.

    That determinism is the point. It makes the module useful for:

      - reproducible oscilloscope captures: a known frame every N milliseconds,
        with none of the ARP/DHCP/retransmit traffic a socket would add
      - separating "the bus works" from "the IP configuration works" when
        something does not come through
      - loss counting over long runs, since TX and RX counters are independent
        of any protocol state

    EtherType 0x88B5 is reserved by IEEE 802 for local experimental use, so the
    frames are guaranteed not to collide with a real protocol, and Wireshark
    shows them without trying to dissect them as something else.

  What is here and what is not:
    This module owns everything specific to the test: the EtherType, the frame
    layout, the counters, the console commands and the wording of its output.

    It does NOT own the deferred packet log. Printing from a receive handler is
    not safe, so received frames are queued in the application's log ring buffer
    (shared with the ipdump paths) and printed later from the main loop. The
    application therefore keeps two small responsibilities: recognising a NoIP
    frame on receive and enqueueing it, and calling NOIP_PrintRxLine() when the
    entry comes back out. See pktEth0Handler() and the log drain in app.c.
 *******************************************************************************/

#ifndef NOIP_TEST_H
#define NOIP_TEST_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* IEEE 802 Local Experimental EtherType used for these frames. */
#define NOIP_ETHERTYPE  0x88B5u

/* Register the console command group ('noip_send', 'noip_stat'). Call once,
   after SYS_CMD is up. */
void NOIP_Initialize(void);

/* --- receive path helpers, for the interface's packet handler --------------- */

/* True if this frame type is one of ours. */
bool NOIP_IsNoIpFrame(uint16_t frameType);

/* Count one received frame and return the new RX total, which doubles as the
   per-frame index used in the printed line. */
uint32_t NOIP_CountRx(void);

/* Extract the 32-bit sequence number the sender put at the start of the
   payload. `frame` points at the start of the Ethernet header. */
uint32_t NOIP_SeqFromFrame(const uint8_t *frame);

/* Format one received frame for the console. Called from the main loop when the
   queued log entry is drained - never from the receive handler itself. */
void NOIP_PrintRxLine(uint32_t index, uint32_t seq, const uint8_t *mac_src,
                      uint16_t length, uint64_t ts_ms);

/* --- counters --------------------------------------------------------------- */

uint32_t NOIP_TxCount(void);
uint32_t NOIP_RxCount(void);

#ifdef __cplusplus
}
#endif

#endif /* NOIP_TEST_H */
