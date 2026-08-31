/*******************************************************************************
  TCP echo test server

  File Name:
    testserver.h

  Summary:
    A plain TCP echo server for bandwidth-ramp testing from a PC-side client
    through the normal L2 bridge path (PC eth1 -> bridge -> eth0/T1S -> the
    node running this server) - NOT through the mirror/sniffer path.

  Description:
    Whatever bytes arrive on the connection are written straight back, in the
    order received, with no framing or rate limiting of its own - the pacing
    is entirely up to whichever client connects. That is deliberate: it lets
    a driving client characterize the actual achievable round-trip throughput
    of the L2 bridge itself, independent of the mirror feature this was
    written alongside (see docs/FALLSTRICKE.md, 2026-08-27 - the mirror/sniffer
    investigation that led to wanting a ground-truth throughput ceiling
    unrelated to that feature).

    Self-contained like lan865x_diag.c and port_mirror.c: needs only the
    Harmony TCP/IP stack's TCP API. Runs on whichever node it is started on -
    in the intended setup that is a T1S endpoint ("follower"), not the bridge
    itself, so the test exercises the bridge's normal forwarding path end to
    end from a PC on the 100BASE-T side.

  Dependencies:
    TCPIP_STACK_USE_TCP must be enabled (it already is - iperf uses it).
*******************************************************************************/

#ifndef TESTSERVER_H
#define TESTSERVER_H

#ifdef __cplusplus
extern "C" {
#endif

/* Register the console command ('testserver'). Call once, after SYS_CMD is up. */
void TESTSERVER_Initialize(void);

/* Drive the server's state machine. Call every APP_Tasks() cycle. */
void TESTSERVER_Tasks(void);

#ifdef __cplusplus
}
#endif

#endif /* TESTSERVER_H */
