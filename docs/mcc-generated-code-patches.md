# Hand-Patches to MCC-Generated Code

This document is the authoritative, exhaustive list of every place this project
hand-edits a file MCC considers its own (anything under `firmware\src\config\default\`).
Per `CLAUDE.md` section 1, that tree is normally touched only through MCC + Generate
Code — every entry below is a **documented exception**, and every one of them will be
silently reverted the next time `Generate Code` regenerates the file it lives in.

Files outside `config\default\` (`app.c`/`.h`, `env.c/h`, `lan865x_diag.c/h`,
`port_mirror.c/h`, `noip_test.c/h`, `testserver.c/h`, everything in `docs\`, `scripts\`)
are plain user files MCC never touches — not covered here, nothing to re-apply.

**Don't re-apply these by hand if you don't have to:** `patches\apply_patches.py`
automates everything below — run `python patches\apply_patches.py --check` after any
`Generate Code` run for a report, or without `--check` to fix whatever it finds
missing. See `patches\README.md` for how it works and how to regenerate a patch file
if a hand-patch itself changes.

**How to find these again by hand:** every real patch below is marked in the source
with a `HAND-PATCH to MCC-generated code, documented exception (CLAUDE.md section 3)`
comment. The two temporary diagnostic blocks are marked `TEMP DIAG` instead (see
[Temporary diagnostic instrumentation](#temporary-diagnostic-instrumentation) at the
end). Both are greppable project-wide:

```
grep -rn "HAND-PATCH" firmware/src/config/default/
grep -rn "TEMP DIAG"   firmware/src/config/default/
```

## At a glance

| # | File | What breaks if lost | Severity |
|---|---|---|---|
| 1 | `peripheral\clock\plib_clock.c` | Board fails to boot at all again (silicon errata) | **Critical — boot blocker** |
| 2 | `driver\lan865x\src\dynamic\drv_lan865x_api.c` — `_Lock`/`_Unlock` | RX chunk-processing race returns (intermittent frame corruption) | High — correctness |
| 3 | `driver\lan865x\drv_lan865x.h` + `drv_lan865x_api.c` (init case) + `initialization.c` — `suppressTx` | Persisted `sniffer=1` stops being silent from boot | Medium — functional regression |
| 4 | `initialization.c` — `TCPIP_HOSTS_CONFIGURATION[].macAddr` / `drvLan865xInitData[]` MAC+PLCA pre-seed | Persisted MAC addresses stop applying; brief wrong-PLCA-identity window returns | Medium — functional regression |
| 5 | `driver\lan865x\src\dynamic\drv_lan865x_api.c` — `mirror_eth0_tx_hook()` call | `mirror`/`sniffer` stop capturing the bridge's own TX traffic | Low — feature regression, no crash |
| 6 | `driver\lan865x\src\dynamic\drv_lan865x_api.c` + `library\tcpip\src\telnet.c` — `#include <stdarg.h>` | Build fails outright (`implicit declaration of function 'va_start'`) | **Critical — build blocker**, but easy to spot and re-add |
| 7 | `system\command\src\sys_command.c` — CR NUL line-ending handling | Every Telnet command after the first one in a session silently fails ("Please type in a command") | High — correctness, Telnet-only |
| 8 | `library\tcpip\src\telnet.c` — `F_Telnet_MSG()` real backpressure | Large command output over Telnet (e.g. `dump`, `netinfo`) truncates instead of completing | Medium — correctness, Telnet-only |
| 9 | `library\tcpip\src\tcpip_mac_bridge.c` — eth0/LAN865x RX length correction | T1S→100BASE-T TCP forwarding stalls after the first near-MTU-size segment; legitimate large frames silently rejected (`failMtu`) | High — correctness, forwarding-path |
| — | `driver\lan865x\src\dynamic\tc6\tc6.c` + `drv_lan865x_api.c` — diagnostic prints | Loses an in-progress debugging aid, nothing else | None (temporary, currently disabled) |

Recommended re-apply order after any `Generate Code` run: **1 first** (nothing else
matters if the board can't boot), then rebuild/flash/confirm it boots at all, then
**2–5, 7, 9** in any order, rebuild/flash/retest once more. **6** will simply fail to
compile if missed, so the build itself catches it — no separate verification needed.

---

## 1. `peripheral\clock\plib_clock.c` — DPLL sync-status timeout

**Why:** Microchip Silicon Errata **DS80000748K** ("SAM D5x/E5x Family Silicon Errata
and Data Sheet Clarification"), item **2.13.2 "FDPLL Ratio in DPLLnRATIO"** — both
silicon revisions this board could be (A and D) are affected. `OSCCTRL_DPLLSYNCBUSY
.DPLLRATIO` never clears even though the DPLL locks and runs correctly (verified by
reading `DPLLRATIO`/`DPLLSTATUS` directly after boot: the ratio is applied and
`LOCK|CLKRDY` are set). MCC's generated `FDPLL0_Initialize()` polls that bit in an
unbounded `while` loop — on this silicon, that loop can spin forever, hanging the
board before `main()` ever reaches application code. Reproducibly triggered by
otherwise-unrelated flash content/address shifts elsewhere in the image during this
project's port work (see `session-log.md`, 2026-08-30/31).

**What:** all three wait loops inside `FDPLL0_Initialize()` get a bounded iteration
count (`CLOCK_DPLL0_SYNC_TIMEOUT = 2000`) instead of looping forever:

```c
#define CLOCK_DPLL0_SYNC_TIMEOUT   2000U

static void FDPLL0_Initialize(void)
{
    uint32_t timeout;
    ...
    timeout = 0U;
    while ((GCLK_REGS->GCLK_PCHCTRL[1] & GCLK_PCHCTRL_CHEN_Msk) != GCLK_PCHCTRL_CHEN_Msk)
    {
        if (++timeout >= CLOCK_DPLL0_SYNC_TIMEOUT) { break; }
    }
    ...
    timeout = 0U;
    while((OSCCTRL_REGS->DPLL[0].OSCCTRL_DPLLSYNCBUSY & OSCCTRL_DPLLSYNCBUSY_DPLLRATIO_Msk) == OSCCTRL_DPLLSYNCBUSY_DPLLRATIO_Msk)
    {
        if (++timeout >= CLOCK_DPLL0_SYNC_TIMEOUT) { break; }
    }
    ...
    timeout = 0U;
    while((OSCCTRL_REGS->DPLL[0].OSCCTRL_DPLLSYNCBUSY & OSCCTRL_DPLLSYNCBUSY_ENABLE_Msk) == OSCCTRL_DPLLSYNCBUSY_ENABLE_Msk )
    {
        if (++timeout >= CLOCK_DPLL0_SYNC_TIMEOUT) { break; }
    }

    timeout = 0U;
    while((OSCCTRL_REGS->DPLL[0].OSCCTRL_DPLLSTATUS & (OSCCTRL_DPLLSTATUS_LOCK_Msk | OSCCTRL_DPLLSTATUS_CLKRDY_Msk)) !=
                (OSCCTRL_DPLLSTATUS_LOCK_Msk | OSCCTRL_DPLLSTATUS_CLKRDY_Msk))
    {
        if (++timeout >= CLOCK_DPLL0_SYNC_TIMEOUT) { break; }
    }
}
```

**No MCC field exists for this** — it is unmodified generated code hitting a genuine
silicon limitation, not a configuration gap.

**If lost:** the board fails to boot again, identically to the multi-hour hang this
was root-caused from. This is the single most important patch to re-apply — verify
with a boot test (`cli.py --port COM8 --read 10 "reset"`, expect the normal
`TCP/IP Stack: Initialization Started/Ended - success` banner) before assuming
anything else in this list matters.

---

## 2. `driver\lan865x\src\dynamic\drv_lan865x_api.c` — real interrupt-safe `_Lock`/`_Unlock`

**Why:** `_Lock()`/`_Unlock()` are the only guard around task-context `TC6_Service()`
(which drives RX chunk accumulation, `process_rx()`/`TC6_CB_OnRxEthernetSlice()`), but
they only wrapped `OSAL_MUTEX_Lock()`/`Unlock()` — on this bare-metal ("basic") OSAL
build that is a plain flag check-and-clear, **not** an interrupt-safe critical
section. The SPI transfer-complete callback (`_EventHandlerSPI()` →
`TC6_SpiBufferDone()`, invoked from a genuine hardware interrupt) could therefore
preempt task-context RX processing at any point. Root-caused 2026-08-31 as the cause
of a confirmed, reproducible, non-deterministic RX length corruption (the same
periodic message observed at both 88 and 102 bytes for a true length of 98, at
different points in time) — the same *class* of bug (a task-level "lock" that does
not block the actually-racing ISR) the sister project (`t1s_100baset_bridge`) found
and fixed for its own GMAC RX path (`docs/FALLSTRICKE.md`).

**What:** `_Lock()`/`_Unlock()` now also wrap a genuine `SYS_INT_Disable()`/
`SYS_INT_Restore()` critical section:

```c
static bool s_lockIntState = false;

static inline void _Lock(OSAL_MUTEX_HANDLE_TYPE *drvMutex)
{
    OSAL_RESULT res;
    s_lockIntState = SYS_INT_Disable();
    res = OSAL_MUTEX_Lock(drvMutex, OSAL_WAIT_FOREVER);
    (void)res;
    SYS_ASSERT(res == OSAL_RESULT_TRUE, "Could not lock the driver mutex");
}

static inline void _Unlock(OSAL_MUTEX_HANDLE_TYPE *drvMutex)
{
    OSAL_RESULT res = OSAL_MUTEX_Unlock(drvMutex);
    (void)res;
    SYS_ASSERT(res == OSAL_RESULT_TRUE, "Could not unlock the driver mutex");
    SYS_INT_Restore(s_lockIntState);
}
```

`DRV_LAN865X_INSTANCES_NUMBER == 1` in this project (`configuration.h`), so one
saved-state variable is safe — none of the `_Lock`/`_Unlock` call sites in this file
nest.

**Verified:** before the fix, the same background message showed `segLen` `88` and
`102` at different times (non-deterministic). After: consistently `102` across every
sample, and the full `sniffer_capture_test.py` completeness run showed **zero**
variance in `frame.len`/`ip.len`/`udp.length` across all 3982 captured iperf UDP
datagrams, three separate runs.

**If lost:** the interrupt race returns — expect intermittent, hard-to-reproduce RX
corruption under load again, not a boot failure. Low chance of being *noticed*
immediately after a regenerate; verify with a repeated `sniffer_capture_test.py` run
or the `g_tc6DiagEnable` chunk-trace method described in `session-log.md` if in
doubt.

---

## 3. `suppressTx` — sniffer silent from boot (three files together)

**Why:** the sister project suppresses the T1S transmitter (`T1SPMACTL.TXD`) as its
own driver-init step, via a `suppressTx` field on `DRV_LAN865X_Configuration`, so a
board persisted as a permanent sniffer (`setenv sniffer 1` + `saveenv`) never puts a
signal on the bus — not even for the fraction of a second between
`NETWORK_CONTROL`/TXEN and `app.c`'s later fix-up. This project's driver didn't have
that field. Confirmed missing (2026-08-31): after `setenv sniffer 1` + `saveenv` +
`reset`, `showenv` correctly reported `sniffer ON at boot`, but `lan_read 0x000308F9`
(T1SPMACTL) still read `0x0` — the software believed sniffer mode was active while
the transmitter stayed live. Ported the sister project's mechanism verbatim to close
the gap.

**What — three files, all needed together:**

`driver\lan865x\drv_lan865x.h` — new field on `DRV_LAN865X_Configuration`, right
after `rxCutThrough`:

```c
    bool rxCutThrough;

    bool suppressTx;

} DRV_LAN865X_Configuration;
```

`driver\lan865x\src\dynamic\drv_lan865x_api.c` — `_InitUserSettings()`'s init state
machine gets a new `case 9` that writes `T1SPMACTL` (`0x000308F9`) = `0x00004000`
(TXD) when `drvCfg.suppressTx` is set, **before** the final "Enable Data Traffic"
`NETWORK_CONTROL`/TXEN write — which is renumbered from `case 9` to `case 10`:

```c
        case 9:
            if (true == pDrvInst->drvCfg.suppressTx) {
                if (TC6_WriteRegister(tc, 0x000308F9u /* T1SPMACTL */, 0x00004000u /* TXD */, CONTROL_PROTECTION, _OnInitialRegisterCB, NULL)) {
                    pDrvInst->initSubState++;
                }
            } else {
                pDrvInst->initSubState++;
            }
            break;
        case 10:
            /* Enable Data Traffic */
            if (TC6_WriteRegister(tc, 0x00010000u /* NETWORK_CONTROL */, 0x0000000Cu, CONTROL_PROTECTION, _OnRegisterDoneCB, NULL)) {
                done = true;
            }
            break;
```

`config\default\initialization.c` — a default value in `drvLan865xInitData[]`'s
initializer, plus the live env override (same block as item 4 below, since both come
from `ENV_Init()`):

```c
    .rxCutThrough =         DRV_LAN865X_RX_CUT_THROUGH_IDX0,
    .suppressTx =           false,
},
```
```c
    drvLan865xInitData[0].nodeId    = env_plca_id();
    drvLan865xInitData[0].nodeCount = env_plca_cnt();
    drvLan865xInitData[0].suppressTx = env_sniffer();
```

**Verified:** `lan_read 0x000308F9` now reads `0x00004000` immediately after boot
with `sniffer` persisted on, before any live CLI command. Normal boot with `sniffer`
off is unaffected (register-confirmed `0x0`).

**If lost:** `drv_lan865x.h`'s field disappearing would actually cause a **build
failure** in `initialization.c`/`drv_lan865x_api.c` (referencing a struct member that
no longer exists) — so this one is self-detecting at compile time, unlike most of
this list. If MCC only reverts `drv_lan865x_api.c`/`initialization.c` but somehow
leaves a stale `drv_lan865x.h` in place (shouldn't happen in practice, all three are
generated together), the symptom reverts to the original: `sniffer` persists as a
software-only flag, transmitter stays live until a live `sniffer 1`.

---

## 4. `initialization.c` — persisted MAC addresses + PLCA pre-seed

**Why:** the persistent-config module (`env.c`, a plain user file, not MCC's
concern) needs to run *before* `TCPIP_STACK_Init()` to feed the persisted MAC
addresses and PLCA node identity into the structures MCC's generated init code
already builds — and the PLCA identity specifically needs to land in
`drvLan865xInitData[]` *before* `DRV_LAN865X_Initialize()`'s one-time copy of it,
otherwise the node is briefly live on the bus under the wrong PLCA identity for
however long it takes `app.c`'s `env_apply()` to correct it later.

**What — several pieces in the same function, `SYS_Initialize()`:**

An include, right after the standard ones:
```c
#include "env.h"
```

`const` dropped from the array so it can be written to before `TCPIP_STACK_Init()`
reads it:
```c
DRV_LAN865X_Configuration drvLan865xInitData[] = {
```
(was `const DRV_LAN865X_Configuration drvLan865xInitData[] = {`)

Two writable string buffers, since the `const TCPIP_HOSTS_CONFIGURATION[]` array
below only holds *pointers* to MAC strings — the struct stays const, the strings
underneath get filled at runtime:
```c
static char s_macAddrStr0[18] = TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX0;
static char s_macAddrStr1[18] = TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX1;
```
...and the two `.macAddr` fields in `TCPIP_HOSTS_CONFIGURATION[]` point at them
instead of the compile-time default macros:
```c
        .macAddr = s_macAddrStr0,   /* was TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX0 */
        ...
        .macAddr = s_macAddrStr1,   /* was TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX1 */
```

And the actual load-and-apply block, right after `EMU_EEPROM_Initialize()` and before
`SYS_TIME_Initialize()`:
```c
    ENV_Init();
    env_mac_str(0, s_macAddrStr0);
    env_mac_str(1, s_macAddrStr1);

    drvLan865xInitData[0].nodeId    = env_plca_id();
    drvLan865xInitData[0].nodeCount = env_plca_cnt();
    drvLan865xInitData[0].suppressTx = env_sniffer();   /* see item 3 above */
```

**If lost:** no crash, no boot failure — `TCPIP_HOSTS_CONFIGURATION[]` falls back to
the compile-time default MAC macros (persisted `setenv mac0/mac1` + `saveenv` values
stop being applied at boot, though they're still stored in EEPROM and would come back
the moment this patch is re-applied), and the PLCA node identity is only corrected
later via `env_apply()` in `app.c`'s `APP_STATE_SERVICE_TASKS` — a brief window right
after boot with the wrong PLCA identity on the bus, same as before this patch existed.

---

## 5. `driver\lan865x\src\dynamic\drv_lan865x_api.c` — `mirror_eth0_tx_hook()` call

**Why:** `port_mirror.c`'s mirror/sniffer feature needs to see every frame the bridge
transmits on eth0 (its own ARP/ping replies, not traffic merely forwarded from eth1)
to mirror the bridge's own TX side to eth1 for Wireshark. `DRV_LAN865X_PacketTx()` is
the single egress point for all eth0 traffic.

**What:** in `DRV_LAN865X_PacketTx()`, right after the initial `SYS_ASSERT` calls and
before the driver mutex is taken:
```c
    {
        extern void mirror_eth0_tx_hook(TCPIP_MAC_PACKET *txPkt);
        mirror_eth0_tx_hook(ptrPacket);
    }

    _Lock(&pDrvInst->drvMutex);
```
The hook itself (`port_mirror.c`, a plain user file) is a no-op unless `mirror` is
on, and only clones frames the bridge itself originated (source MAC == eth0 MAC) —
forwarded frames keep their original source MAC and are skipped, since the PC
already has them via the normal forwarding path.

**If lost:** `mirror`/`sniffer` keep working for the RX direction (T1S bus → eth1,
wired via `MIRROR_Eth0Rx()` called directly from `app.c`'s packet handler — a plain
user file, unaffected) but silently stop mirroring the bridge's own TX traffic. No
error message, no crash — just a quieter-than-expected Wireshark capture.

---

## 6. `#include <stdarg.h>` — recurring MCC generator bug

**Why:** a known, recurring MCC code-generation gap (not specific to this port): any
generated file whose code path uses `va_start`/`va_end` needs `<stdarg.h>`, and MCC's
generator sometimes omits the include. No MCC GUI field controls this — it is a pure
generator bug, re-triggered by unrelated regenerates that happen to touch the file.
Seen twice in this project:

- `driver\lan865x\src\dynamic\drv_lan865x_api.c` (`PrintRateLimited()`) — removed by
  an early regenerate during this project's initial bring-up.
- `library\tcpip\src\telnet.c` (`F_Telnet_PRINT()`) — missing immediately after the
  Telnet Server component was first added via MCC.

**What:** a single line at the top of each file's include block:
```c
#include <stdarg.h>
```

**If lost:** the build fails outright —
`implicit declaration of function 'va_start'` (or `'va_end'`) pointing at the
offending file. Impossible to miss; just re-add the include and rebuild. **After any
regenerate that touches either file, check first** — this is the cheapest patch on
this list to verify and the most likely to silently disappear again.

---

## 7. `system\command\src\sys_command.c` — CR NUL line-ending handling

**Why:** RFC 854 allows a telnet client to terminate a line with CR followed by
either LF or NUL, and real clients use both - TeraTerm sends CR NUL for Enter,
confirmed byte-for-byte with a live capture on `tcp port 23`
(`0d 00`, never `0d 0a`). The MCC-generated character-input state machine
(`RunCmdTask()`) only recognizes `'\r'`/`'\n'` as end-of-line; a bare `'\0'`
fell through to the generic "valid char; insert and echo it back" branch and
got silently prepended to the *next* command's `cmdBuff`. Every later
`ParseCmdBuffer()` call then `strncpy()`/tokenized a C string whose first byte
was `'\0'` - looks empty to every string function even though real text
follows it - so the command was always parsed as empty (`argc == 0`,
"Please type in a command"). The very first command in a session, typed into
a still-clean buffer, was unaffected - only every one after it failed. No MCC
GUI field controls telnet line-ending handling.

**What:** one new branch in the character-processing state machine, right
after the existing `\r`/`\n` case and before the generic character-insert
case:
```c
else if(newCh == '\0')
{
    return;   /* CR NUL line ending (RFC 854) - discard, not text */
}
```

**If lost:** Telnet becomes unusable beyond the very first command of each
session - every later command silently fails with "Please type in a command"
even though it was typed and echoed correctly. The serial console is
unaffected (its line editor never sees a NUL byte from a physical terminal).
**Root-caused and fixed 2026-08-31** (see `docs/session-log.md`) - verified
both with a synthetic two-consecutive-commands reproduction over a raw socket
and live in TeraTerm.

---

## 8. `library\tcpip\src\telnet.c` — `F_Telnet_MSG()` real backpressure

**Why:** command output larger than `TCPIP_TELNET_SKT_TX_BUFF_SIZE` truncated over
Telnet (`dump`, `netinfo`), because `F_Telnet_MSG()` called `NET_PRES_SocketWrite()`
unconditionally and ignored how much it actually accepted — same pattern as
`SERCOM1_USART_Write()`'s silent-truncation-on-full-buffer behaviour on the serial
side, but with no equivalent of `DumpMem()`'s `SYS_CONSOLE_WriteFreeBufferCountGet()`
busy-wait available on the Telnet side to throttle against.

A first attempt (2026-08-31) added exactly that: a bounded busy-wait on
`NET_PRES_SocketWriteIsReady()`, later also adding `NET_PRES_SocketFlush()` per call
(matching this file's own login/banner code). Neither helped — measured `dump 800`
at 3075–3093 of the needed ~4011 bytes, taking 6.6s instead of completing near
instantly. Root cause: in this bare-metal, single-superloop build, `SYS_CMD_Tasks()`
— which runs the command handler that calls `F_Telnet_MSG()` — executes *before*
`TCPIP_STACK_Task()`/`NET_PRES_Tasks()` in `SYS_Tasks()`
(`config\default\tasks.c`). Nothing drains a Telnet socket's TX buffer until those
two run, so a bare busy-wait just burns its timeout waiting on a drain that can
never happen from inside it — unlike the serial UART case, where a hardware
interrupt keeps draining the ring buffer regardless of what the main loop is doing.

**What:** two parts, both needed. First, pump the network stack while waiting
instead of just spinning, via a new `APP_PumpNetworkStack()` (`app.c`/`app.h`) that
runs `TCPIP_STACK_Task(sysObj.tcpip)` + `NET_PRES_Tasks(sysObj.netPres)` out of
turn. This is safe from reentrancy because `F_Telnet_MSG()` is only ever reached via
the `SYS_CMD_API` `.msg`/`.print` callback — i.e. only from `SYS_CMD_Tasks()`'s call
chain, which is a *sibling* of `TCPIP_STACK_Task()` in `SYS_Tasks()`, never nested
inside it. `APP_PumpNetworkStack()` is therefore an extra out-of-turn call, not a
recursive one. (A plain call to the whole `SYS_Tasks()` was considered and
rejected: it would re-enter `SYS_CMD_Tasks()` itself — the very frame already on
the stack, with its own static parser state — and `APP_Tasks()`, risking
interleaved app-level state machine execution.)

Second — found only after the first part alone still showed *intermittent*
corruption on large dumps (`dump 0x20000000 32000` over Telnet: a line's ASCII
tail would occasionally go missing and the next line's address glue on directly,
no `\n\r` between them, at a different byte offset each run): a bounded
pre-check on `NET_PRES_SocketWriteIsReady()` does not reliably predict what a
single `NET_PRES_SocketWrite()` call actually accepts. The original code wrote
once and discarded the return value — same "fire and forget" bug as
`SERCOM1_USART_Write()` on the serial side, just less obvious because it usually
(not always) writes everything requested. Fixed by looping on the real return
value instead of trusting the pre-check:
```c
uint16_t sent = 0U;
while(sent < len)
{
    uint16_t remaining = (uint16_t)(len - sent);
    uint16_t n = (uint16_t)NET_PRES_SocketWrite(tSkt, FC_CStr2CVPtr(&str[sent]), remaining);
    if(n > 0U) { sent = (uint16_t)(sent + n); continue; }
    if((int64_t)(SYS_TIME_Counter64Get() - deadline) >= 0) { break; }
    APP_PumpNetworkStack();
}
(void)NET_PRES_SocketFlush(tSkt);
```

**If lost:** large Telnet command output silently truncates again at whatever
`TCPIP_TELNET_SKT_TX_BUFF_SIZE` currently is, and/or intermittently corrupts
(concatenated lines, no separator) on runs large enough to hit the write-retry
race (serial output is unaffected either way). **Root-caused and fixed
2026-08-31** (see `docs/session-log.md`) — verified with `dump` at
800/2000/4000/8000/32000 bytes (all complete: 4011/9938/19813/39554/158065
bytes, ~1.8–5.3s, vs. truncating at ~3072 bytes before) and with `netinfo`;
`dump 0x20000000 32000` re-run 5x back-to-back after the write-retry fix came
back byte-identical (158065 bytes, zero glued lines) every time, vs. varying
every run before it (158020/158029/157993/158065/158212).

---

## 9. `library\tcpip\src\tcpip_mac_bridge.c` — eth0/LAN865x RX length correction

**Why:** a T1S follower node running `iperf -c <PC> -p 5001` (TCP) through this
bridge to a PC would complete the handshake, send one small segment, then one
larger (~1400+ byte) segment — get it ACKed — and then never send anything
more. `bridge stats` showed a nonzero `failMtu` counter. Root cause: at the
point `F_MAC_Bridge_ProcessRxPkt()` computes `fwdDcpt.pktLen =
TCPIP_PKT_PayloadLen(pRxPkt)`, the value for **eth0/LAN865x-sourced
(`inPort == 0`) frames already excludes the 14-byte Ethernet header** (the
generic MCC stack code strips that upstream before this function ever sees the
packet) **but still includes the 4-byte FCS**, which eth1/GMAC-sourced frames
never carry this far. The unpatched code treats `pktLen` as already
correct for both ports, so every eth0-forwarded frame is 4 bytes "too long"
by the time it's compared against `linkMtu` (rejecting frames right at the
MTU boundary — the `failMtu` symptom) and by the time it's retransmitted on
eth1 (the stale 4 bytes get requested as extra TX bytes on top of the real
frame, most consequential for a large frame close to a buffer/threshold
boundary — the stall symptom).

**A first attempt at this fix (2026-08-31, same day, reverted before commit)
was wrong** — ported the assumption from the sister project's own fix
verbatim without checking whether it held for *this* codebase's net stack
version (`v3.14.5` here vs. the sister's `v3.11.1`), and subtracted 18
(header + FCS) instead of 4 (FCS only), undersizing every eth0-forwarded
frame by 14 bytes. Confirmed harmful, not just ineffective: TCP SYNs
arrived with everything past the first 8 header bytes (ports + sequence
number) zeroed, breaking the handshake outright — worse than the original
bug, which at least let small packets and the handshake through. **Root
cause established properly this time** via a temporary
`SYS_CONSOLE_PRINT` of `pktLen`/`segLen` at the computation site, correlated
against a Wireshark capture of the same ping's true wire frame
(`ip.len`): the bridge printed `pktLen=132` for a ping whose actual IP total
length was `128` — a 4-byte difference, matching the FCS exactly, not 18.

**What:** two places, both needed:

```c
    fwdDcpt.pktLen = TCPIP_PKT_PayloadLen(pRxPkt);
    if(inPort == 0)
    {
        fwdDcpt.pktLen -= 4u;   // eth0/LAN865x: header already stripped upstream, FCS is not
    }
```

and, in the shared TX-length fixup applied after either the copy-branch or
the zero-copy branch (`pFwdPkt == pRxPkt` in the zero-copy case, so its
`segLen` is still the raw, uncorrected value):

```c
    pFwdPkt->pDSeg->segLen = fwdDcpt.pktLen + (uint16_t)sizeof(TCPIP_MAC_ETHERNET_HEADER);
```

replacing the original `pFwdPkt->pDSeg->segLen +=
sizeof(TCPIP_MAC_ETHERNET_HEADER);`, which blindly added the header size
back onto whatever `segLen` already held — correct for eth1/GMAC frames,
4 bytes too many for eth0/LAN865x ones. Setting it directly from the
already-corrected `fwdDcpt.pktLen` fixes both the copy and zero-copy paths
uniformly, without needing to know what `F_MAC_Bridge_PacketCopy()` itself
does to the destination's `segLen`.

Deliberately did **not** touch the MTU comparison itself
(`pFDcpt->pktLen <= linkMtu`, in `F_MAC_Bridge_SetPacketForward()`) or the
zero-copy-vs-copy branch structure (`pktRes == TCPIP_MAC_BRIDGE_PKT_RES_HOST_PROCESS`)
that the reverted first attempt also changed — with `pktLen` now correct at
the source, the existing comparison and existing branch structure need no
further adjustment.

**If lost:** T1S→100BASE-T TCP transfers through the bridge stall after the
first large segment again, and `bridge stats` will show a growing `failMtu`
counter under any traffic with segments near the interface MTU.
**Root-caused and fixed 2026-08-31** (see `docs/session-log.md`) — verified
with a full 5-second `iperf -c <PC> -p 5001` TCP run from a T1S follower:
1660/1660 segments, 0% loss, 2.31 MB transferred, matching on both client and
server; `bridge stats` afterward showed `failMtu: 0` despite thousands of
forwarded frames.

---

## Temporary diagnostic instrumentation

Not a fix — left over from the investigation behind items 1 and 2, currently
**disabled** (`g_tc6DiagEnable = 0` at boot). Should be stripped out entirely before
any release build, or re-armed (`poke <addr-from-.map> 1`) if the residual,
deterministic per-frame-size length offset noted in `session-log.md` (2026-08-31) is
picked back up.

- `driver\lan865x\src\dynamic\tc6\tc6.c`: a non-static `uint32_t g_tc6DiagEnable`
  flag, plus a gated `SYS_CONSOLE_PRINT` in `process_rx()` printing every RX chunk's
  `buf_len/sv/sbo/ev/ebo/mfd/twoFrames/offsetRx`.
- `driver\lan865x\src\dynamic\drv_lan865x_api.c`: `TC6_CB_OnRxEthernetPacket()` gated
  on the same flag, prints the final `len`/`segLen` plus the first 48 bytes of the
  received frame.

Toggle live: build, find `g_tc6DiagEnable`'s address in the `.map` file
(`grep g_tc6DiagEnable *.map`), then `poke <addr> 1` / `poke <addr> 0` over the CLI.
No MCC exception documentation needed for these two blocks specifically once removed
— they exist only to be deleted.

---

*See `docs/session-log.md` for the full chronological investigation behind each of
these, `CLAUDE.md` section 3 for the running list this document was assembled from,
and `CLAUDE.md`'s own "MCC Generate Code impact analysis" entry in `session-log.md`
(2026-08-31) for a predictive walkthrough of what an actual `Generate Code` run would
do to each of these, written before item 2/3 existed — items 1, 5 and 6's analysis
there still applies unchanged; this document supersedes it for items 2–4.*
