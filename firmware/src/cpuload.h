/*******************************************************************************
  Per-task CPU-load profiling for the round-robin main loop

  File Name:
    cpuload.h

  Summary:
    Opt-in, cycle-accurate timing of each call SYS_Tasks() makes per pass
    (SYS_CMD_Tasks, the MIIM driver, TCPIP_STACK_Task, NET_PRES_Tasks,
    APP_Tasks) plus the whole pass itself, using the Cortex-M4 DWT cycle
    counter. Disabled by default - CPULOAD_Enter()/CPULOAD_Exit() are a
    single flag check each when off, so leaving this compiled in costs
    nothing until 'cpuload on' arms it.

  Description:
    SYS_Tasks() (firmware/src/config/default/tasks.c) is a plain, unconditional,
    non-blocking round-robin loop, not an RTOS - there is no separate idle
    task to measure against, so "CPU load" here means "how many cycles does
    each of the 5 polled calls actually take, and how does that change under
    traffic" rather than a classic busy/idle percentage.

    Call CPULOAD_Enter(slot)/CPULOAD_Exit(slot) around each timed region -
    already wired into tasks.c's hand-patch, see docs/mcc-generated-code-patches.md
    item 12. Query/reset from the console via the 'cpuload' command in app.c.

    CPULOAD_LivePoll() is wired into that same hand-patch, as the very first
    statement in SYS_Tasks() - before CPULOAD_Enter(CPULOAD_SLOT_TOTAL) and
    before SYS_CMD_Tasks(). It has to run there, not from APP_Tasks() or
    anywhere later in the pass: while a console is live, it steals that
    console's pending bytes itself so 'r'/'q'/'t'/'c' work without Enter, and
    it can only win that race by reading before sys_command.c's own
    RunCmdTask() gets a turn on the same pass.

    Interrupts get the same treatment via a different mechanism: the six
    CPULOAD_ISR_*() wrappers below are what interrupts.c's own hand-patch
    (item 13) points its vector table at instead of the real handlers - see
    the comment above each wrapper in cpuload.c. One shared 'cpuload on/off'
    controls both the main-loop and the interrupt slots together.
 *******************************************************************************/

#ifndef CPULOAD_H
#define CPULOAD_H

#include <stdbool.h>
#include <stdint.h>
#include "system/command/sys_command.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    /* main loop - one per SYS_Tasks() call, plus TOTAL for the whole pass */
    CPULOAD_SLOT_SYS_CMD = 0,
    CPULOAD_SLOT_MIIM,
    CPULOAD_SLOT_TCPIP,
    CPULOAD_SLOT_NET_PRES,
    CPULOAD_SLOT_APP,
    CPULOAD_SLOT_TOTAL,        /* whole SYS_Tasks() pass, not a 6th call */

    /* interrupts - every ISR this board actually has wired, see
       interrupts.c's hand-patch (docs/mcc-generated-code-patches.md item 13).
       DWT->CYCCNT never stops for an interrupt, so time spent in one of these
       while a main-loop slot's bracket is open is still also counted inside
       that main-loop slot - these numbers are not subtracted out of it. */
    CPULOAD_SLOT_ISR_DMAC0,
    CPULOAD_SLOT_ISR_DMAC1,
    CPULOAD_SLOT_ISR_SPI,      /* SERCOM0 - LAN865x/TC6 SPI driver, eth0 */
    CPULOAD_SLOT_ISR_USART,    /* SERCOM1 - console UART */
    CPULOAD_SLOT_ISR_GMAC,     /* eth1 */
    CPULOAD_SLOT_ISR_TC0,      /* SYS_TIME tick */

    CPULOAD_SLOT_COUNT
} CPULOAD_SLOT;

/* on: clears all stats, arms the DWT cycle counter, starts recording.
   off: CPULOAD_Enter()/CPULOAD_Exit() become no-ops; the stats accumulated
   so far stay queryable via CPULOAD_PrintStats(). Default is off. */
void CPULOAD_Enable(bool on);
bool CPULOAD_IsEnabled(void);

/* Clears accumulated stats without changing the enabled state. */
void CPULOAD_Reset(void);

/* Prints per-slot count/min/max/mean/median (cycles and microseconds) plus
   the average loop rate derived from the TOTAL slot's mean. */
void CPULOAD_PrintStats(SYS_CMD_DEVICE_NODE *pCmdIO);

/* Call in matching pairs around each timed region. No-ops while disabled. */
void CPULOAD_Enter(uint8_t slot);
void CPULOAD_Exit(uint8_t slot);

/* Live view: the 'cpuload stats' table, redrawn in place once a second on
   the console pCmdIO came from, until that console sends 'q'. Auto-enables
   sampling (as CPULOAD_Enable(true) would) if it wasn't already on. Only one
   console can be live at a time - a second CPULOAD_LiveStart() while one is
   already active declines instead of taking over. See cpuload.c for the
   known limitation around a Telnet console disconnecting mid-session. */
void CPULOAD_LiveStart(SYS_CMD_DEVICE_NODE *pCmdIO);

/* Drives live mode: reads any pending keystroke from the live console ('r'
   resets, 'q' stops, 't'/'c' switch the displayed units, anything else is
   discarded) and redraws once a second. A no-op when no console is live.
   MUST be called before SYS_CMD_Tasks() in SYS_Tasks() - see the file header
   - so it can consume keystrokes before the normal command-line reader gets
   a chance to. */
void CPULOAD_LivePoll(void);

/* Pass-through wrappers around this board's 6 real interrupt handlers -
   Enter(ISR slot) / the real handler / Exit(ISR slot), same no-op-when-off
   behaviour as CPULOAD_Enter()/CPULOAD_Exit(). Not meant to be called
   directly: interrupts.c's vector table hand-patch (item 13) points at
   these instead of the real handlers, which stay completely unmodified. */
void CPULOAD_ISR_DMAC0(void);
void CPULOAD_ISR_DMAC1(void);
void CPULOAD_ISR_SPI(void);
void CPULOAD_ISR_USART(void);
void CPULOAD_ISR_GMAC(void);
void CPULOAD_ISR_TC0(void);

#ifdef __cplusplus
}
#endif

#endif /* CPULOAD_H */
