# Configuring the 10BASE-T1S ↔ 100BASE-T Bridge (tcpip_iperf_lan865x)

Status: **draft, work in progress.** This manual is being built up alongside the bring-up
work recorded in `docs\session-log.md`. Sections below will be filled in as each part of
the configuration is verified working on real hardware — until then, treat this as a
skeleton, not a finished guide.

## Scope

How to configure this MPLAB Harmony v3 / MCC project (`tcpip_iperf_lan865x`, target
ATSAME54P20A) as a Layer-2 bridge between a 10BASE-T1S segment (via the LAN865x SPI MAC-PHY)
and a 100BASE-T segment (via the SAME54's internal GMAC + an external RMII PHY).

## Planned sections

1. Hardware overview (board, PHY daughter board, pin usage)
2. MCC component setup — Data Link layer graph (GMAC, LAN865x, PHY, NETCONFIG interfaces)
3. Pin assignment for the GMAC RMII + MDIO signals
4. Enabling the MAC bridge between the two network interfaces
5. Building, flashing, and using the serial CLI console
6. Verifying the bridge (link status, traffic forwarding, `iperf`)
7. Known pitfalls and their fixes (cross-referenced from `CLAUDE.md`)

<!-- Fill in sections as they are verified on hardware. -->
