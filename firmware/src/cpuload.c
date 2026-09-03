/*******************************************************************************
  Per-task CPU-load profiling for the round-robin main loop - implementation

  See cpuload.h for the design rationale. Timing source is the Cortex-M4 DWT
  free-running cycle counter (DWT->CYCCNT), armed only while enabled via
  CoreDebug->DEMCR.TRCENA + DWT->CTRL.CYCCNTENA - the same CMSIS core header
  (core_cm4.h, reached through definitions.h) crashlog.c already uses for
  SCB. 32-bit cycle-count subtraction wraps correctly across a single
  DWT->CYCCNT rollover (~35.8s at the project's 120MHz core clock), which is
  all CPULOAD_Enter()/CPULOAD_Exit() ever need since each pair brackets one
  call within a single main-loop pass.
*******************************************************************************/

#include <stdlib.h>              /* qsort */
#include <string.h>              /* memset, memcpy */
#include "configuration.h"       /* SYS_TIME_CPU_CLOCK_FREQUENCY */
#include "definitions.h"         /* CoreDebug, DWT */
#include "system/time/sys_time.h"/* SYS_TIME_Counter64Get/FrequencyGet */
#include "interrupts.h"          /* the 6 real ISR handlers the CPULOAD_ISR_*() wrappers call through to */
#include "cpuload.h"
#include "cmd_print.h"           /* CMD_PRINT */

#define CPULOAD_RING_SIZE       128u   /* power of two - see ring index mask below */
#define CPULOAD_CYCLES_PER_US   (SYS_TIME_CPU_CLOCK_FREQUENCY / 1000000u)

typedef struct {
    uint32_t min;
    uint32_t max;
    uint64_t sum;
    uint32_t count;
    uint32_t ring[CPULOAD_RING_SIZE];
    uint32_t ringIdx;              /* free-running; wrap into the ring via & (SIZE-1) */
} cpuload_slot_t;

static const char* const s_slotName[CPULOAD_SLOT_COUNT] = {
    "sys_cmd", "miim", "tcpip", "net_pres", "app", "TOTAL",
    "isr_dmac0", "isr_dmac1", "isr_spi", "isr_usart", "isr_gmac", "isr_tc0"
};

#define CPULOAD_ISR_SLOT_FIRST  CPULOAD_SLOT_ISR_DMAC0

static cpuload_slot_t s_slots[CPULOAD_SLOT_COUNT];
/* Kept separate from s_slots/ResetStats() on purpose: the 'cpuload reset' and
   'cpuload on' commands are themselves console commands, so they run from
   inside CPULOAD_SLOT_SYS_CMD's own open Enter/Exit bracket (see tasks.c). A
   plain ResetStats() that also zeroed entryCycle would wipe out the SYS_CMD
   slot's (and TOTAL's) in-flight entry timestamp while that very bracket is
   still open, making the following Exit() compute DWT->CYCCNT - 0 - a huge,
   bogus elapsed time. Confirmed on hardware 2026-09-03: 'cpuload reset' sent
   over the console produced a ~3.7e9-cycle sys_cmd/TOTAL sample that then
   skewed mean and max until the next reset. */
static uint32_t s_entry[CPULOAD_SLOT_COUNT];
/* Whether s_entry[slot] was actually (re)recorded for the bracket that is
   currently open, i.e. whether Enter() ran while enabled. Needed because
   'cpuload on'/'reset' are themselves console commands that run from inside
   CPULOAD_SLOT_SYS_CMD's (and TOTAL's) own open bracket: if that bracket's
   own Enter() ran while still disabled (no entry recorded), but the command
   it brackets flips s_enabled true, Exit() must NOT fire for real - it would
   have nothing genuine to subtract from. Confirmed on hardware 2026-09-03: a
   second 'cpuload on' (after an earlier 'off') produced a ~2.7e9-cycle
   sys_cmd/TOTAL sample before this existed. Exit() now only trusts an entry
   actually recorded for the currently-open bracket, not just "enabled right
   now". (A related, now separately fixed cause of the same symptom: 'on'
   used to also reset DWT->CYCCNT, which could zero the counter out from
   under a bracket whose entry WAS validly recorded moments earlier in the
   same pass - see CPULOAD_Enable()'s comment for why that reset is gone.) */
static bool s_entryValid[CPULOAD_SLOT_COUNT];
static bool s_enabled = false;
static uint32_t s_scratch[CPULOAD_RING_SIZE];   /* PrintOneSlot()/SlotMedian() only, not re-entrant */

static void ResetStats(void)
{
    memset(s_slots, 0, sizeof s_slots);
    for (uint32_t i = 0u; i < (uint32_t)CPULOAD_SLOT_COUNT; i++) {
        s_slots[i].min = UINT32_MAX;
    }
}

void CPULOAD_Enable(bool on)
{
    if (on) {
        ResetStats();
        /* Deliberately does NOT reset DWT->CYCCNT (arming it below is
           idempotent and enough) - see the comment on s_entryValid for why
           a third, real corruption case came from doing that. Enter()/
           Exit() only ever use the DELTA between two DWT reads, which is
           correct via unsigned wraparound regardless of the counter's
           absolute value, so there was never a correctness reason to zero
           it - only a cosmetic one (keeping the printed numbers small),
           not worth the risk. */
        CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
        DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    }
    s_enabled = on;
}

bool CPULOAD_IsEnabled(void)
{
    return s_enabled;
}

void CPULOAD_Reset(void)
{
    ResetStats();
}

void CPULOAD_Enter(uint8_t slot)
{
    if (!s_enabled) { s_entryValid[slot] = false; return; }
    s_entry[slot] = DWT->CYCCNT;
    s_entryValid[slot] = true;
}

void CPULOAD_Exit(uint8_t slot)
{
    if (!s_entryValid[slot]) { return; }   /* no matching Enter() this bracket - see s_entryValid's comment */
    s_entryValid[slot] = false;
    cpuload_slot_t *s = &s_slots[slot];
    uint32_t elapsed = DWT->CYCCNT - s_entry[slot];   /* wraps correctly, see file header */

    if (elapsed < s->min) { s->min = elapsed; }
    if (elapsed > s->max) { s->max = elapsed; }
    s->sum += elapsed;
    s->count++;
    s->ring[s->ringIdx & (CPULOAD_RING_SIZE - 1u)] = elapsed;
    s->ringIdx++;
}

/* --- ISR wrappers ------------------------------------------------------
 *
 * interrupts.c's hand-patch (docs/mcc-generated-code-patches.md item 13)
 * points its vector table at these instead of the 6 real handlers, so every
 * one of this board's actual interrupts is timed without touching a single
 * line inside the peripheral drivers that implement them - no need to find
 * or preserve each handler's own early-return paths. CPULOAD_Enter()/Exit()
 * are the same no-op-when-disabled calls the main loop uses; the one thing
 * genuinely different here is that the wrapper's own call indirection is
 * NOT free when 'cpuload' is off, unlike the main loop's inlined hooks -
 * the vector table always points here, so that one extra function call
 * happens on every interrupt regardless of on/off state. Negligible next to
 * the handler's own cost, but real - see cpuload.h/docs for the same note.
 */
void CPULOAD_ISR_DMAC0(void)
{
    CPULOAD_Enter(CPULOAD_SLOT_ISR_DMAC0);
    DMAC_0_InterruptHandler();
    CPULOAD_Exit(CPULOAD_SLOT_ISR_DMAC0);
}

void CPULOAD_ISR_DMAC1(void)
{
    CPULOAD_Enter(CPULOAD_SLOT_ISR_DMAC1);
    DMAC_1_InterruptHandler();
    CPULOAD_Exit(CPULOAD_SLOT_ISR_DMAC1);
}

/* isr_spi legitimately stays at "no samples yet" on this board: SERCOM0's
   DRV_SPI instance is configured with real DMA channels (initialization.c),
   so drv_spi.c always takes its DMA transfer path here - SERCOM0_SPI_
   InterruptHandler() below is linked in and correctly wired (confirmed with
   a hardware breakpoint, docs/cpuload-profiling-report.md item 13/§9) but
   genuinely never runs for this SPI bus. The real transfer-complete
   signalling shows up as isr_dmac0/isr_dmac1 instead. Not a cpuload bug. */
void CPULOAD_ISR_SPI(void)
{
    CPULOAD_Enter(CPULOAD_SLOT_ISR_SPI);
    SERCOM0_SPI_InterruptHandler();
    CPULOAD_Exit(CPULOAD_SLOT_ISR_SPI);
}

void CPULOAD_ISR_USART(void)
{
    CPULOAD_Enter(CPULOAD_SLOT_ISR_USART);
    SERCOM1_USART_InterruptHandler();
    CPULOAD_Exit(CPULOAD_SLOT_ISR_USART);
}

void CPULOAD_ISR_GMAC(void)
{
    CPULOAD_Enter(CPULOAD_SLOT_ISR_GMAC);
    GMAC_InterruptHandler();
    CPULOAD_Exit(CPULOAD_SLOT_ISR_GMAC);
}

void CPULOAD_ISR_TC0(void)
{
    CPULOAD_Enter(CPULOAD_SLOT_ISR_TC0);
    TC0_TimerInterruptHandler();
    CPULOAD_Exit(CPULOAD_SLOT_ISR_TC0);
}

static int CompareU32(const void *a, const void *b)
{
    uint32_t ua = *(const uint32_t*)a;
    uint32_t ub = *(const uint32_t*)b;
    return (ua > ub) - (ua < ub);
}

/* Median over the most recent up-to-CPULOAD_RING_SIZE samples - see the
   "median" note in cpuload.h. Sorts a scratch copy; only runs when a human
   asks for 'cpuload stats', so an O(n log n) sort of <=128 elements is free. */
static uint32_t SlotMedian(const cpuload_slot_t *s)
{
    uint32_t n = (s->count < CPULOAD_RING_SIZE) ? s->count : CPULOAD_RING_SIZE;
    if (n == 0u) { return 0u; }

    memcpy(s_scratch, s->ring, n * sizeof(uint32_t));
    qsort(s_scratch, n, sizeof(uint32_t), CompareU32);

    if ((n & 1u) != 0u) {
        return s_scratch[n / 2u];
    }
    return (uint32_t)(((uint64_t)s_scratch[n / 2u - 1u] + s_scratch[n / 2u]) / 2u);
}

/* One row. cycles/us conversion happens here, not by scaling the stored
   values - min/max/mean/median stay exact in cycles in s_slots regardless
   of what a caller chooses to display. */
static void PrintOneSlot(SYS_CMD_DEVICE_NODE *pCmdIO, uint32_t slot, bool showUs)
{
    const cpuload_slot_t *s = &s_slots[slot];
    if (s->count == 0u) {
        CMD_PRINT(pCmdIO, "  %-10s  (no samples yet)\n\r", s_slotName[slot]);
        return;
    }

    uint32_t mn   = s->min;
    uint32_t mx   = s->max;
    uint32_t mean = (uint32_t)(s->sum / s->count);
    uint32_t med  = SlotMedian(s);
    if (showUs) {
        mn   /= CPULOAD_CYCLES_PER_US;
        mx   /= CPULOAD_CYCLES_PER_US;
        mean /= CPULOAD_CYCLES_PER_US;
        med  /= CPULOAD_CYCLES_PER_US;
    }
    CMD_PRINT(pCmdIO, "  %-10s  %-9lu  %-9lu  %-8lu  %-7lu  %-7lu\n\r",
        s_slotName[slot], (unsigned long)s->count, (unsigned long)mn,
        (unsigned long)mx, (unsigned long)mean, (unsigned long)med);
}

/* Fixed number of lines PrintStatsTable() always prints - every section
   (both loop counts, both "--"/header lines, both summary lines) is now
   unconditional, specifically so this stays a compile-time constant and
   CPULOAD_LivePoll()'s redraw can move the cursor up by exactly this many
   lines without having to track a variable frame height. See CPULOAD_LivePoll(). */
#define CPULOAD_LIVE_FRAME_LINES  ((uint32_t)CPULOAD_SLOT_COUNT + 7u)

/* Shared by CPULOAD_PrintStats() (always cycles) and the live redraw
   (cycles or microseconds, per 't'/'c' - see CPULOAD_LivePoll()). TOTAL
   still means only the whole SYS_Tasks() pass - the interrupt slots are a
   separate section, not folded into it (see cpuload.h). */
static void PrintStatsTable(SYS_CMD_DEVICE_NODE *pCmdIO, bool showUs)
{
    CMD_PRINT(pCmdIO, "cpuload: %s (DWT cycle counter, %u.%uMHz core clock)\n\r",
        s_enabled ? "ENABLED" : "disabled - showing last values, if any",
        (unsigned)(SYS_TIME_CPU_CLOCK_FREQUENCY / 1000000u),
        (unsigned)((SYS_TIME_CPU_CLOCK_FREQUENCY / 100000u) % 10u));
    /* Kept to <=80 columns on purpose, including the widest (microseconds)
       variant - see the identical note on the median line below. */
    CMD_PRINT(pCmdIO, "  slot        n          min        max       mean     median  (%s)\n\r",
        showUs ? "us" : "cycles");

    CMD_PRINT(pCmdIO, "-- main loop --\n\r");
    for (uint32_t i = 0u; i < (uint32_t)CPULOAD_ISR_SLOT_FIRST; i++) {
        PrintOneSlot(pCmdIO, i, showUs);
    }
    CMD_PRINT(pCmdIO, "-- interrupts --\n\r");
    for (uint32_t i = (uint32_t)CPULOAD_ISR_SLOT_FIRST; i < (uint32_t)CPULOAD_SLOT_COUNT; i++) {
        PrintOneSlot(pCmdIO, i, showUs);
    }

    {
        const cpuload_slot_t *total = &s_slots[CPULOAD_SLOT_TOTAL];
        uint64_t wallCycles = total->sum;   /* every measured pass, back to back - see CPU-load comment below */
        if (total->count != 0u) {
            uint32_t meanCycles = (uint32_t)(wallCycles / total->count);
            CMD_PRINT(pCmdIO, "TOTAL: mean %lu cycles (%lu us) per pass -> avg %lu loops/s\n\r",
                (unsigned long)meanCycles, (unsigned long)(meanCycles / CPULOAD_CYCLES_PER_US),
                (meanCycles != 0u) ? (unsigned long)(SYS_TIME_CPU_CLOCK_FREQUENCY / meanCycles) : 0UL);
        } else {
            CMD_PRINT(pCmdIO, "TOTAL: (no samples yet)\n\r");
        }

        /* CPU load: an ISR total %, a tasks (main loop) total %, and their
           sum, always exactly 100 - every cycle this feature has measured is
           in one bucket or the other. There is no idle task here to compare
           against - the bare-metal loop never sleeps, so a classic "busy vs
           idle %" doesn't apply; this is the honest equivalent. wallCycles
           (the sum of every measured SYS_Tasks() pass) already includes any
           interrupt time that happened to preempt those passes, since
           DWT->CYCCNT keeps counting through an interrupt - so it stands in
           for "total elapsed time" here, and isrCycles/wallCycles is the
           fraction of it spent servicing interrupts; the rest, by
           definition, is the main loop's own share - taskPct is derived as
           100-isrPct rather than summed from the 5 task slots separately, so
           the two printed numbers always add up to exactly the third rather
           than drifting apart by a rounding cycle. (CPULOAD_LivePoll()
           itself runs just outside any TOTAL bracket - see tasks.c - so an
           interrupt landing in that narrow gap is not reflected in
           wallCycles; negligible next to thousands of measured passes, but
           the one reason isrPct is clamped to 100 rather than trusted blind.) */
        if (wallCycles != 0u) {
            uint64_t isrCycles = 0u;
            for (uint32_t i = (uint32_t)CPULOAD_ISR_SLOT_FIRST; i < (uint32_t)CPULOAD_SLOT_COUNT; i++) {
                isrCycles += s_slots[i].sum;
            }
            uint32_t isrPct = (uint32_t)((isrCycles * 100u) / wallCycles);
            if (isrPct > 100u) { isrPct = 100u; }
            uint32_t taskPct = 100u - isrPct;
            CMD_PRINT(pCmdIO, "CPU load: interrupts %lu%%, tasks %lu%%, total %lu%%\n\r",
                (unsigned long)isrPct, (unsigned long)taskPct, (unsigned long)(isrPct + taskPct));
        } else {
            CMD_PRINT(pCmdIO, "CPU load: (no samples yet)\n\r");
        }

        /* Kept to <=80 columns on purpose: CPULOAD_LivePoll()'s redraw moves
           the cursor up by a FIXED line count (CPULOAD_LIVE_FRAME_LINES),
           one physical terminal row per line printed here - a line that
           wraps past the terminal width silently eats a second physical row
           the cursor-up math doesn't know about, drifting every later frame
           further down the screen. Confirmed on hardware 2026-09-03: this
           line alone was 85 columns, always wrapping (and reproducing the
           "no repeat" symptom from the very save/restore bug this was
           supposed to have already fixed) on a standard 80-column terminal. */
        CMD_PRINT(pCmdIO, "median: last <=%u samples/slot - min/max/mean: since last reset\n\r",
            (unsigned)CPULOAD_RING_SIZE);
    }
}

void CPULOAD_PrintStats(SYS_CMD_DEVICE_NODE *pCmdIO)
{
    PrintStatsTable(pCmdIO, false);
}

/* --- Live mode --------------------------------------------------------
 *
 * NULL when no console is live. Not a lifecycle-tracked handle: if a Telnet
 * session starts 'cpuload live' and then disconnects without typing 'q',
 * this keeps pointing at that now-stale SYS_CMD_DEVICE_NODE until something
 * calls CPULOAD_LiveStart() again (which only replaces it once a console
 * types 'q' on the current one) or the board resets. Not chasing full
 * Telnet session-lifecycle tracking for this - noted here and in
 * docs/cli-reference.md rather than silently ignored. In practice this
 * feature is meant for the one interactive session watching it.
 */
static SYS_CMD_DEVICE_NODE *s_livePCmdIO = NULL;
static uint64_t s_liveNextTick = 0u;
static bool s_liveShowUs = false;    /* 't' sets, 'c' clears; CPULOAD_PrintStats() itself is unaffected */
static bool s_liveFirstFrame = false;/* true until the first frame has been drawn - see CPULOAD_LivePoll() */

void CPULOAD_LiveStart(SYS_CMD_DEVICE_NODE *pCmdIO)
{
    if (s_livePCmdIO != NULL) {
        CMD_PRINT(pCmdIO, "cpuload: already live on another console - 'q' there first\n\r");
        return;
    }
    if (!s_enabled) {
        CPULOAD_Enable(true);
    }
    CMD_PRINT(pCmdIO, "cpuload live: 'r' resets, 't'/'c' switch us/cycles, 'q' quits\n\r");
    s_livePCmdIO = pCmdIO;
    s_liveShowUs = false;
    s_liveFirstFrame = true;
    s_liveNextTick = SYS_TIME_Counter64Get();   /* draw on the very next poll, not a second from now */
}

void CPULOAD_LivePoll(void)
{
    if (s_livePCmdIO == NULL) { return; }

    const SYS_CMD_API *api = s_livePCmdIO->pCmdApi;
    const void *param = s_livePCmdIO->cmdIoParam;
    bool redrawNow = false;

    /* Drains every byte pending for this one console before sys_command.c's
       own RunCmdTask() gets a turn later this same pass - see cpuload.h and
       tasks.c for why this function has to run first. Anything typed that
       isn't r/q/t/c (either case) is simply discarded; there is no line
       buffer while live. */
    while ((*api->isRdy)(param) != 0) {
        char c = (*api->getc_t)(param);
        if ((c == 'q') || (c == 'Q')) {
            CMD_PRINT(s_livePCmdIO, "\n\rcpuload live: stopped\n\r");
            s_livePCmdIO = NULL;
            return;
        }
        if ((c == 'r') || (c == 'R')) {
            CPULOAD_Reset();
            redrawNow = true;
        }
        if ((c == 't') || (c == 'T')) {
            s_liveShowUs = true;
            redrawNow = true;
        }
        if ((c == 'c') || (c == 'C')) {
            s_liveShowUs = false;
            redrawNow = true;
        }
    }

    if (redrawNow) {
        s_liveNextTick = SYS_TIME_Counter64Get();   /* don't wait out the rest of this second */
    } else if ((int64_t)(SYS_TIME_Counter64Get() - s_liveNextTick) < 0) {
        return;   /* not due yet */
    }
    s_liveNextTick += SYS_TIME_FrequencyGet();

    /* Deliberately NOT \x1b[s (save cursor) / \x1b[u (restore) - tried that
       first, and it broke the moment the table grew past one screenful: the
       saved position is an absolute (row, col) on the CURRENT screen, and
       once the terminal has scrolled even once, restoring it lands on
       whatever now happens to be at that row/col instead of the table - the
       symptom was the very first frame drawing fine and every one after it
       going nowhere useful. Confirmed on hardware 2026-09-03 once this was
       actually watched in a real terminal instead of just checked byte-for-
       byte over the wire (which can't detect scrolling at all).

       Cursor-up by a FIXED, known line count instead - relative to wherever
       the cursor already is, so it keeps working after any number of
       scrolls. Every field in PrintStatsTable() is unconditional (a "(no
       samples yet)" placeholder line for slots/summaries with nothing yet
       recorded) specifically so CPULOAD_LIVE_FRAME_LINES is a true compile-
       time constant - no frame is ever a different height than the last. */
    if (!s_liveFirstFrame) {
        CMD_PRINT(s_livePCmdIO, "\x1b[%uA\r\x1b[J", (unsigned)CPULOAD_LIVE_FRAME_LINES);
    }
    s_liveFirstFrame = false;
    PrintStatsTable(s_livePCmdIO, s_liveShowUs);
}
