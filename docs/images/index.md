# Screenshot Index

MCC/Harmony configurator and MPLAB X IDE screenshots for the `tcpip_iperf_lan865x` app
(dual network interface: NETCONFIG-0 on LAN865x/10BASE-T1S, NETCONFIG-1 on GMAC/LAN8742A).
Descriptions below are intended to let an agent pick the right image without re-opening
and visually re-analyzing every file.

## tcpip-configurator-overview.png
TCP/IP Configurator, "Overview" page, "Data Link Layer" row expanded and selected.
Shows the full stack-layer overview: Application (DNS Client, IPERF), Presentation (empty),
Transport (TCP, UDP), Network (ARP, ICMPv4, IPv4), Data Link (GMAC, LAN865x-0, LAN8742A
selected/highlighted, NETCONFIG-0, NETCONFIG-1), Basic Configuration (TCPIP CMD, TCPIP CORE).

## tcpip-configurator-config-summary-interfaces.png
TCP/IP Configurator, "Config Summary" > "Interface" tab. Shows the two configured network
interfaces side by side: Interface 0 = LAN865x (Internal Mac: NO, External Interface), and
Interface 1 = GMAC (Internal Mac: YES, PHY Interface: RMII).

## tcpip-configurator-datalink-lan8740.png
TCP/IP Configurator, "Data Link" page. Available Components lists MAC options (ENC28J60,
ENCX24J600, LAN865x, PPP) and PHY options (DP83640, DP83848, Dummy, IP101GR, KSZ80xx/81/91,
KSZ8863, KSZ9031/9131, LAN867x/8700/8720/8740/8742A/8770/8840/9303/9354, VSC8540). Active
Components graph: NETCONFIG-0/NETCONFIG-1 <-> GMAC/LAN865x-0 <-> **LAN8740** PHY + MIIM Driver
(earlier/alternate PHY choice for the GMAC path, before switching to LAN8742A).

## tcpip-configurator-datalink-lan8742a.png
Same Data Link page as above, but PHY component list scrolled/selected with DP83640 focused
and Active Components graph showing GMAC wired to **LAN8742A** PHY + MIIM Driver (the PHY
actually used for Interface 1/GMAC in this project).

## tcpip-configurator-datalink-lan865x-lan8742a-active.png
Data Link page, full Active Components graph with both interfaces visible at once:
NETCONFIG-0 <-> LAN865x-0 (MAC) and NETCONFIG-1 <-> GMAC (MAC) <-> LAN8742A (PHY). Confirms
the final wiring: LAN865x-0 has no separate PHY box (10BASE-T1S PHY is integrated), GMAC uses
the external LAN8742A PHY.

## tcpip-configurator-netconfig-advanced-settings.png
TCP/IP Configurator, NETCONFIG "Advanced Settings" panel (scrolled view). Shows fields:
Number/Timeout of ARP-cache-like entries (Descriptors in Pool: 16, Descriptors to Replenish: 4,
Timeout for Entry to be Purged: 300, Maximum Transit Delay: 1), checkboxes "Do not Learn Dynamic
Addresses", "Forward Traffic Only if Entry Exists", "Use Interface Names for Initialization",
**"Enable Statistics" (checked, highlighted)**, "Enable Event Notify" (unchecked here),
"Bridge Task Rate (in msec): 333", "Disable the MAC Bridge Ports Glue".

## tcpip-configurator-netconfig-enable-statistics-event-notify.png
Tight crop of the NETCONFIG Advanced Settings panel showing only two checkboxes, both
**checked and highlighted green: "Enable Statistics" and "Enable Event Notify"** — the final
state after enabling both (follow-up to the screenshot above where only Enable Statistics was
checked).

## pin-table-gmac-porta.png
MPLAB X "Pin Table" view, Port A rows PA10-PA20. Shows GMAC RMII pin assignments:
PA12=GMAC_GRX1, PA13=GMAC_GRX0, PA14=GMAC_GTXCK, PA15=GMAC_GRXER, PA17=GMAC_GTXEN,
PA18=GMAC_GTX0, PA19=GMAC_GTX1 (function/digital/direction columns), PA10/PA11/PA16/PA20
still "Available".

## pin-table-gmac-portc.png
MPLAB X "Pin Table" view, Port C rows PC19-PC24. Shows remaining GMAC pin assignments:
PC20=GMAC_GRXDV, PC22=GMAC_GMDC, PC23=GMAC_GMDIO; PC19/PC21/PC24 still "Available".

## mcc-project-graph-netconfig-instances.png
Full MPLAB X IDE window (MCC v5.7.0 tab active), Project Graph in "DATA LINK LAYER" view.
Left: Project Resources tree (Libraries > Harmony > System). Center graph: LAN865x MAC Layer
block with "Instance 0" (DRV_SPI) connected via MAC to NETCONFIG block with "Instance 0" and
"Instance 1" (both TCP/IP Library). Right pane: Configuration Options tree collapsed
(System > Device & Project Configuration).

## mcc-netconfig-instance1-config.png
MCC Project Graph (Data Link Layer view) with NETCONFIG **Instance 1** selected/highlighted.
Right-side Configuration Options panel expanded for Instance 1: Network Configurations Index=1,
Interface=GMAC, Host Name=MCHPBOARD_C, Mac Address=00:04:25:1C:A0:0x (partially visible),
IPv4 Static Address=192.168.0.12, IPv4 SubNet Mask=255.255.255.0,
IPv4 Default Gateway Address=192.168.0.1, IPv4 Primary DNS=192.168.0.1, plus collapsed
"Network Configuration Start-up Flags" and "Advanced Settings" nodes.

## mcc-lan865x-instance0-plca-config.png
MCC Project Graph (Data Link Layer view), LAN865x **Instance 0** selected. Right panel shows
its Configuration Options: Number of RX Descriptors=2, RX Descriptor Buffer Size=1536,
SPI Chip Select Pin=SYS_PORT_PIN_PC15, Interrupt Pin=SYS_PORT_PIN_PC14,
Reset Pin=SYS_PORT_PIN_PC18, **10BASE-T1S Operation Mode=PLCA** with PLCA Settings
(Local Node Id=5, Node Count=8, Max Burst Count=0, Burst Timer=128 -> 12.8us), and Advanced
Settings: TC6 chunk size=64, TC6 chunks per SPI Transaction (XACT)=31,
**Promiscuous=checked**, **TX cut through=checked**, RX cut through=unchecked.

## mcc-gmac-rx-filters-promiscuous.png
MCC Project Graph (Data Link Layer view), GMAC MAC Layer block selected (wired to LAN8742A
PHY and to SPI/SERCOM0 driver instance shown at bottom-left for the LAN865x SPI path). Right
panel shows GMAC Configuration Options: Size of RX Buffer=1536, Number of additional Rx
buffers=2, Minimum Threshold for Rx Buffer replenish=1, Rx Buffer allocate count=2, and
Ethernet RX Filters Selection: Accept Broadcast/Multicast/Unicast Packets all checked, plus
**"Accept All Packets (Promiscuous Mode)" checked and highlighted green**.

## mcc-linker-heap-size-before-44960.png
Full MPLAB X IDE window, MCC Project Graph (Data Link Layer view) plus right-side
Configuration Options tree expanded to System > Project Configuration > Tool Chain Selections
> Linker > General, showing **Heap Size (bytes) = 44,960** (highlighted) before it was
increased — Initialize Data checked.

## mcc-linker-heap-size-after-163840.png
Same Linker > General settings location as above, in the full IDE window, after the change:
**Heap Size (bytes) = 163,840** (highlighted). Project tree on the left also visible
(Header/Important/Library/Linker/Source Files, wolfcrypt, etc.).

## mcc-tcpip-core-dynamic-ram-size-65535.png
Full MPLAB X IDE window, MCC Project Graph in "TRANSPORT LAYER" view (TCP/UDP blocks), right
panel expanded to TCPIP CORE > Heap Configuration: TCP/IP Stack State Machine Tick Rate=1ms,
Use Heap Config=TCPIP_STACK_HEAP_TYPE, **TCP/IP Stack Dynamic RAM Size = 65,535** (highlighted),
Stack allocation=malloc/calloc style, deallocation=free style, Dynamic RAM Lower Limit=2048,
TCP/IP Heap Size Estimate=55 KB (worst-case estimate note visible at bottom).
