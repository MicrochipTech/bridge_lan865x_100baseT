/*******************************************************************************
  eth0 (10BASE-T1S) -> eth1 (100BASE-T) port mirror / SPAN

  File Name:
    port_mirror.h

  Summary:
    Clones the bridge's own T1S conversation onto eth1 so a PC running Wireshark
    on the 100BASE-T side can capture the two-wire bus.

  Description:
    A 10BASE-T1S segment cannot be tapped with an ordinary NIC, and the MAC
    bridge only carries frames that belong on the other port. That leaves the
    interesting part invisible: the traffic between the bridge firmware itself
    and the nodes on the bus (its own ARP/ICMP and the replies to it). This
    module copies exactly that traffic onto eth1.

    Both directions are mirrored, each filtered against the bridge's OWN eth0
    MAC so the capture stays duplicate-free:

      - RX (bus -> bridge): only frames addressed TO the bridge.
      - TX (bridge -> bus): only frames the bridge ITSELF originated.

    Frames the MAC bridge merely forwards between the PC and the bus keep their
    original MAC addresses and already reach eth1 natively; mirroring them would
    show them twice at the PC.

    A second, independent mode - "sniffer" - drops the RX destination-MAC
    filter above: with sniffer on, EVERY frame eth0 receives is mirrored,
    including traffic between two OTHER nodes on the bus that never involves
    the bridge at all (only possible because the LAN865x driver already runs
    promiscuous - see DRV_LAN865X_PROMISCUOUS_IDX0 in configuration.h - so the
    frames reach this module's RX hook in the first place). The mirror's TX
    side (what this module copies to eth1) is unchanged in sniffer mode: it
    still only mirrors what the bridge itself sends. Mirror and sniffer are
    independent toggles and may be on at the same time; sniffer's RX filter
    is the strict superset.

    Sniffer ALSO disables the LAN8651's own transmitter (T1SPMACTL.TXD, see
    SNIFFER_Set()) for as long as it is on: a passive bus tap should not put
    anything of its own on the wire, including the bridge's own ARP/ping and
    - the whole reason this exists - normal PC<->T1S forwarding. That stops
    working while sniffer is on; turn it back off to resume forwarding.

    Frames above MIRROR_SAFE_FRAME_LEN (1514, standard max Ethernet frame
    without FCS) are TRUNCATED before mirroring, not passed through whole -
    see the constant's comment in port_mirror.c and docs/SNIFFER_4_ERGEBNISSE.md.
    Root-caused there: full-size (>1514) frames mirrored to eth1 reliably
    wedge the PC's own USB-Ethernet adapter/Npcap capture for several
    seconds, confirmed to be outside this firmware (the GMAC driver reports
    every one of those transmissions as completed successfully - ackRes,
    tracked in ack_ok/ack_fail - and no Windows PnP/link event is ever
    logged) - so the fix has to live here, capping what reaches eth1, since
    nothing in this codebase can fix the PC side. The real T1S traffic
    between the actual endpoints is completely unaffected; only what the
    mirror copies to Wireshark is capped - the mirrored frame itself shows
    up there as exactly MIRROR_SAFE_FRAME_LEN bytes long, not its original
    length, with the tail of the payload simply missing (and, for TCP,
    its checksum consequently invalid - expected and harmless, since this
    copy is never actually processed by anything, only displayed).

  Dependencies:
    Unlike lan865x_diag.c, this module is NOT free-standing. It needs:

      - the Harmony TCP/IP stack (packet allocation, TCPIP_NET_IF internals via
        tcpip_manager_control.h)
      - DRV_GMAC_PacketTx, i.e. specifically a GMAC as the mirror destination
      - the LAN865x driver patched to call mirror_eth0_tx_hook() from its TX path
      - a call to MIRROR_Eth0Rx() from the eth0 RX packet handler

    It is reusable in another Harmony two-port bridge, not in an arbitrary
    project. Adapting it means replacing the two interface indices (0 = source,
    1 = mirror destination) and the destination MAC driver.
 *******************************************************************************/

#ifndef PORT_MIRROR_H
#define PORT_MIRROR_H

#include <stdbool.h>
#include <stdint.h>

#include "config/default/library/tcpip/tcpip.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Register the console command group ('mirror'). Call once, after SYS_CMD is up. */
void MIRROR_Initialize(void);

/* Current state. */
bool MIRROR_IsEnabled(void);

/* Turn mirroring on or off without going through the console. */
void MIRROR_Set(bool enable);

/* Sniffer mode: RX mirrors every eth0 frame regardless of destination MAC,
   not just frames addressed to the bridge - see the module description. */
bool SNIFFER_IsEnabled(void);
void SNIFFER_Set(bool enable);

/* true once a readback has PROVEN that T1SPMACTL.TXD matches the current
   sniffer mode. False while the write is still being retried, and false for
   good if it could not be placed - see the SNIFFER_TXD_* block in
   port_mirror.c. Anything that depends on the bridge being electrically silent
   must test this, not SNIFFER_IsEnabled(), which only reports what was asked
   for. */
bool SNIFFER_TxdConfirmed(void);

/* Service the pending TXD write: retry it, evaluate its readback, time out.
   Call from the main loop AFTER LAN865X_DIAG_Tasks(), whose readback result
   this consumes. */
void MIRROR_Tasks(void);

/* RX path: call for every frame received on eth0, from the interface's packet
   handler. Does nothing when mirroring is off, and applies the own-MAC filter
   itself - the caller does not need to check anything. */
void MIRROR_Eth0Rx(TCPIP_MAC_PACKET *rxPkt);

/* TX path: called by the LAN865x driver for every frame about to leave on eth0.
 *
 * THE NAME OF THIS FUNCTION IS NOT FREE. The generated driver declares it with a
 * local `extern void mirror_eth0_tx_hook(TCPIP_MAC_PACKET *txPkt);` and calls it
 * directly - see DRV_LAN865X_PacketTx() in
 * src/config/default/driver/lan865x/src/dynamic/drv_lan865x_api.c. That call is a
 * hand-patch to MCC-generated code: renaming this function breaks the link, and
 * re-running MCC code generation removes the call site. If a build suddenly
 * mirrors nothing on the TX side, check that the patch is still in place. */
void mirror_eth0_tx_hook(TCPIP_MAC_PACKET *txPkt);

#ifdef __cplusplus
}
#endif

#endif /* PORT_MIRROR_H */
