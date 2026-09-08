# Documentation Index

Every Markdown document in `docs\`, what it covers, and when it is the one to
open. The project's entry point is the [`README.md`](../README.md) in the
repository root — it describes what the firmware does, how to build and flash
it, and how to use the CLI. The documents here go one level deeper: how the
project was built, why particular decisions were made, and what was measured.

---

## Start here, by question

| If you want to … | Read |
|---|---|
| use the firmware — features, build, flash, configuration | [`../README.md`](../README.md) |
| look up a CLI command: arguments, output, what it does not do | [`cli-reference.md`](cli-reference.md) |
| turn a single-interface LAN865x project into a bridge yourself, in MCC | [`how-to-bridge.md`](how-to-bridge.md) |
| change or extend the code without breaking it on the next MCC run | [`development-notes.md`](development-notes.md) |
| understand or re-apply a hand-patch to generated code | [`mcc-generated-code-patches.md`](mcc-generated-code-patches.md), then [`../patches/README.md`](../patches/README.md) |
| demo or interpret LAN8651 registers | [`register-tool-demo-examples.md`](register-tool-demo-examples.md) |
| know what throughput to expect, and why | [`iperf_matrix_results.md`](iperf_matrix_results.md) |
| know how much CPU headroom the main loop has, and how to measure it yourself | [`cpuload-profiling-report.md`](cpuload-profiling-report.md) |
| see the firmware running on a board that has no 100BASE-TX PHY | [`three-board-rollout-report.md`](three-board-rollout-report.md) |
| debug a board live with pyOCD (breakpoints, fault postmortems, recovery resets) | [`pyocd-agent-debugging-guide.md`](pyocd-agent-debugging-guide.md) |
| flash a board over the network instead of with a debug probe | [`bootload-agent-flashing-guide.md`](bootload-agent-flashing-guide.md) |
| set up TLS on a fresh checkout, or start a bench over: which certificates exist, which have to be created and pushed | [`pki-clean-start.md`](pki-clean-start.md) |
| know what TLS, certificates and MQTT cost in flash, RAM, heap and CPU | [`resource-cost-tls-mqtt.md`](resource-cost-tls-mqtt.md) |
| find out why something is the way it is | [`session-log.md`](session-log.md) |
| pick a screenshot without opening every file | [`images/index.md`](images/index.md) |

---

## The documents

### [`cli-reference.md`](cli-reference.md) — every CLI command
*The reference for operating a running board.* All 30 commands this firmware
registers, in their six groups, plus the `iperf` and `tcpip` groups the Harmony
stack adds: syntax, arguments, captured example output, and the caveats that
matter — which settings are volatile and which persist, why a command
returning promptly is not proof the hardware took it, and where a capture is
not byte-complete. Ends with a table of what does and does not survive a reset.

### [`how-to-bridge.md`](how-to-bridge.md) — MCC procedure for the bridge
*245 lines.* A technical brief on turning a standard single-interface
Harmony LAN865x application into a transparent Layer-2 bridge on the
ATSAME54P20A: which MCC components to add, how the second interface
(GMAC + external PHY) is wired up, and which per-interface settings actually
make the MAC bridge forward. This is the document to follow if you want to
reproduce the bridge in your own project rather than clone this one.

### [`development-notes.md`](development-notes.md) — rules and workflow for maintainers
*202 lines.* The hard rules this codebase follows — first among them that
MCC-generated code under `firmware\src\config\default\` is never hand-edited
as the actual fix — plus the day-to-day build/flash workflow and a dated list
of MCC-regeneration pitfalls that each cost real debugging time. Read before
changing anything in the project.

### [`mcc-generated-code-patches.md`](mcc-generated-code-patches.md) — every hand-patch, and why
*636 lines, the reference for the patch mechanism.* One entry per patch under
`patches\`: what MCC generates, what is wrong with it, what the patch changes,
and how the symptom presents if the patch goes missing. Covers the silicon
errata workaround in `plib_clock`, the driver lock/unlock race, the Telnet
backpressure fix, the MAC-bridge FCS miscount, `TC6_TX_ETH_MAX_SEGMENTS`, and
the recurring `stdarg.h` code-generation gap. The mechanics of re-applying them
are in [`../patches/README.md`](../patches/README.md).

### [`register-tool-demo-examples.md`](register-tool-demo-examples.md) — LAN8651 registers, worked
*181 lines.* Cheat sheet for the "LAN8651 Registers" tab in `bridge_gui.py`:
how the MMS sub-tabs are laid out, and worked examples with real values —
`DEVID` (why it, and not `OA_PHYID`, identifies the part), `PLCA_CTRL1`
(node count and node ID in one word), `PRSSTS` (observed versus configured
cycle length), the PCS diagnostic counters, and the 48-bit `MAC_TSH`/`MAC_TSL`
timestamp. Addresses and values are checked against `json/lan8651_model.json`.

### [`iperf_matrix_results.md`](iperf_matrix_results.md) — measured throughput
*72 lines.* The full 12-direction `iperf` matrix from
`scripts\iperf_matrix_test.py`, measured on 2026-09-01 across the PC, this
bridge and two T1S follower nodes: UDP rate search plus TCP, both directions
per pair, with loss always read from the receiving side. Includes the
`TC6_TX_ETH_MAX_SEGMENTS` finding — bridge-originated TCP failed outright at
0.00 Mbit/s until the driver was allowed more than one segment per packet.

### [`cpuload-profiling-report.md`](cpuload-profiling-report.md) — per-task CPU load, measured
*2026-09-03.* How the `cpuload` command works (the DWT cycle counter, the six
timed slots, why `median` and `min`/`max`/`mean` cover different windows), the
exact bench procedure used to measure it (including a real firmware bug the
test itself found — `cpuload reset` corrupting an in-flight timestamp — and a
bench IP mismatch against `iperf_matrix_test.py`'s `DEVICES` table that
invalidated the first "worst case" run), and the resulting numbers: quiet-bus
baseline versus the bridge in sniffer mode under a genuine full 10 Mbit/s UDP
flood between the two follower nodes.

### [`three-board-rollout-report.md`](three-board-rollout-report.md) — one image on three boards, measured
*2026-09-02 and 2026-09-03.* The same firmware deployed to all three bench
boards, including one populated with only the T1S MAC-PHY, with the
configuration used and the numbers that came out: what a fully erased board
seeds into its own EEPROM, a seven-address reachability matrix, the full
twelve-direction iperf matrix, and a sniffer capture validated at the segment's
10 Mbit/s ceiling with tshark — checked for lost segments, not just for byte
counts. Records three defects: a board with a missing PHY being stuck on the
compiled default IP for that interface (present), an iperf session that cannot
be killed once its ARP never resolves (no longer triggered), and an assumption
in the matrix script that this bench no longer satisfies (fixed). Ends with an
explicit verification-status table saying which board state is covered by which
test, and what is not verified.

### [`pyocd-agent-debugging-guide.md`](pyocd-agent-debugging-guide.md) — pyOCD recipes for an AI agent
Practical, tested pyOCD recipes for debugging this board live: environment
specifics (target/pack/probe selection on a multi-board bench), quick CLI checks,
`faultlog` + `xc32-addr2line` postmortems with no debugger needed, an interactive
breakpoint-debugging Python-API recipe (with the "halt before arming a breakpoint,
or it silently never fires" gotcha that cost real time the first time), and the
one safety rule that matters most on a networked target: a halted core is a dead
board on the network until resumed.

### [`bootload-agent-flashing-guide.md`](bootload-agent-flashing-guide.md) — flashing over the network
The operational recipe for updating firmware via this project's own dual-bank OTA
mechanism instead of a debug probe: the `bootload.py` command and what a healthy
run looks like, what a wedged board's Telnet-connect failure means and how to
recover it (a plain SWD reset, not a flash), and why this path is preferred over
`flash.bat`/pyOCD whenever a board is network-reachable.

### [`resource-cost-tls-mqtt.md`](resource-cost-tls-mqtt.md) — what TLS actually costs
*The question the TLS branch exists to answer, measured rather than estimated.*
Flash and static RAM broken down per module from the linker map (wolfSSL is
39.7 % of the image; its static RAM is 112 bytes); the 160 KB heap — who takes
it, what the TCP/IP stack's own 96 KB is doing there, live `heapinfo`
watermarks, and per-structure heap demand of a handshake from the build's own
DWARF; and CPU load per main-loop slot, including the measurement that matters
most: one mutual-TLS handshake is ~139 M cycles, of which a single
uninterruptible 745 ms RSA operation stalls the whole round-robin loop, and
the per-byte cost of an established connection, which turns out to be the
console path rather than the cipher. Also names the four crypto blocks the
SAME54 has and this build ignores — AES, TRNG, ICM and the PUKCC public-key
accelerator whose wolfSSL port is already vendored here. Ends with what could
not be measured and how to measure it, plus a ranked list of where to cut.

### [`pki-clean-start.md`](pki-clean-start.md) — from a fresh checkout to a provisioned bench
*Read this before wondering why the TLS tools do not work on a new clone.* Which
certificates the repo actually contains (only the default identity, plus the CA
as documented bring-up material) and which are created locally and never
committed — the operator identity and one private key per board. Then the
seven-step procedure: delete every board identity, flash all targets back onto
the compiled-in default, create the operator identity, discover the boards,
issue one identity each, push them, verify. Includes what flashing alone does
*not* reset (the identity saved in emulated EEPROM) and why the CA private key
is deliberately still tracked.

### [`session-log.md`](session-log.md) — chronological bring-up record
*2406 lines, by far the largest document here.* Every step of the bring-up in
order, with what was tried, what happened and what it turned out to mean.
Not a guide and not meant to be read front to back: it is the place to search
when you need the reasoning behind a decision the other documents only state.
Kept in English, appended after each completed step.

### [`bridge-configuration-manual.md`](bridge-configuration-manual.md) — planned configuration manual
*24 lines. Draft skeleton, mostly unwritten.* Intended as the full
configuration write-up for the bridge; at present it lists the planned
sections and defers to `session-log.md`. Included here so it is not mistaken
for a finished guide — use [`how-to-bridge.md`](how-to-bridge.md) instead.

### [`images/index.md`](images/index.md) — screenshot index
*109 lines.* One description per MCC/Harmony configurator and MPLAB X
screenshot under `docs\images\`, written so the right image can be picked
without opening each file. Separate from this index, which covers documents.

### [`claude-md-template.md`](claude-md-template.md) — starting point for a local `CLAUDE.md`
*55 lines.* `CLAUDE.md` is deliberately not tracked (see `.gitignore`): it is
a local, per-machine file for AI assistants working on this repository. This
template is the content to start it from — it imports the documents above so
that project knowledge is loaded rather than rediscovered each session.

---

## Markdown outside `docs\`

| File | Purpose |
|---|---|
| [`../README.md`](../README.md) | The project itself: features, hardware, architecture, build and flash, CLI reference, configuration, mirror/sniffer, throughput testing, test modes, Telnet console |
| [`../patches/README.md`](../patches/README.md) | How `apply_patches.py` works, and how to regenerate a patch file after a hand-fix changes |
