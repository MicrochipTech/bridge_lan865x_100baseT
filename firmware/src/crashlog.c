/*******************************************************************************
  Fault self-analysis (HardFault/MemManage/BusFault/UsageFault) - implementation

  Captures the offending PC plus the ARMv7-M fault status registers directly
  in the fault handler, without any debugger attached (recipe: see
  D/Debugging/FIRMWARE_SELF_DEBUGGING.md section 4 - the file this module was
  built from), formats them into a human-readable report, and keeps that
  report across the reset that follows. All four ARMv7-M fault vectors are
  covered, not just HardFault - see the comment on FaultName() below for why.

  Persistence mechanism: the report lives in a plain __attribute__((persistent))
  variable, which XC32 places in the ".pbss" section (Compiler User Guide
  9.11.9). This project's MCC-generated firmware/src/config/default/ATSAME54P20A.ld
  already maps ".pbss"/".bkupram_bss" into the "bkupram" MEMORY region
  (0x47000000, the SAME54's Backup RAM) - a region entirely separate from the
  "ram" region the C-runtime startup clears on every reset. No linker-script
  change was needed for this: the mapping was already there, unused, before
  this module existed.
*******************************************************************************/

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>                          /* snprintf */
#include <string.h>                         /* strlen */

#include "config/default/system/console/sys_console.h"
#include "system/command/sys_command.h"
#include "system/reset/sys_reset.h"         /* SYS_RESET_SoftwareReset() - same reset path as the "reset" CLI command */
#include "definitions.h"                    /* SCB, NVIC_SystemReset (via sys_reset.h too) */
#include "crashlog.h"
#include "cmd_print.h"                      /* CMD_PRINT - reply to pCmdIO, not always the serial console */

#define CRASHLOG_MAGIC     0x48464C54u      /* 'HFLT' */
#define CRASHLOG_TEXT_SIZE 512u

typedef struct {
    uint32_t magic;
    bool     shown;                         /* already printed at boot this power-up */
    char     text[CRASHLOG_TEXT_SIZE];
} crashlog_record_t;

/* Lands in Backup RAM, not ordinary SRAM - see file header comment. Must
 * stay without an initializer: the persistent attribute excludes it from
 * C-runtime startup entirely, so a '= {0}' here would only be silently
 * ignored (and warned about) by the compiler, never actually applied at
 * reset. On a genuinely first-ever power-up, Backup RAM content is
 * undefined - that is exactly what the magic check below guards against. */
static crashlog_record_t s_crashlog __attribute__((persistent));

static void append_flag(char* buf, size_t bufsize, uint32_t reg, uint32_t mask, const char* name)
{
    if ((reg & mask) != 0u) {
        size_t used = strlen(buf);
        if (used < bufsize) {
            (void)snprintf(buf + used, bufsize - used, "%s ", name);
        }
    }
}

/* This project's own init already has SCB->SHCSR.MEMFAULTENA/BUSFAULTENA/
 * USGFAULTENA all set (confirmed on hardware: SHCSR=0x00070008 while stuck
 * in the still-weak UsageFault_Handler after a deliberate "udf" test) - so
 * MemManage/Bus/UsageFault conditions do NOT escalate to HardFault here, they
 * go straight to their own dedicated handler. Capturing only HardFault would
 * silently miss most real faults, each landing in one of exceptions.c's
 * other three weak while(true){} loops instead. All four vectors therefore
 * share this one capture routine; the actual fault type comes from the
 * LIVE IPSR special register (__get_IPSR(), read while still inside the
 * handler) - NOT from the stacked frame's xPSR, which instead reflects
 * whatever context this exception interrupted (almost always Thread mode,
 * IPSR=0, for a fault taken from ordinary application code) and so cannot
 * tell the four vectors apart. Confirmed on hardware: frame[7] read back
 * 0x01000000 (T-bit only, IPSR=0) for a UsageFault triggered from a plain
 * command handler - the live IPSR is 6 (UsageFault) at that same moment. */
static const char* FaultName(uint32_t ipsr)
{
    switch (ipsr & 0xFFu) {
        case 3:  return "HardFault";
        case 4:  return "MemManageFault";
        case 5:  return "BusFault";
        case 6:  return "UsageFault";
        default: return "Fault";
    }
}

/* Exception frame pushed by the hardware on entry to any exception, offsets
 * per FIRMWARE_SELF_DEBUGGING.md section 3:
 *   [0]=R0 [1]=R1 [2]=R2 [3]=R3 [4]=R12 [5]=LR [6]=PC [7]=xPSR
 * A plain C handler would move the stack pointer in its own prologue before
 * this frame could be read back out, so each *_Handler stays a naked stub
 * (below) that hands the raw frame pointer to this C handler untouched. */
void __attribute__((noreturn)) Fault_C(uint32_t* frame)
{
#if defined(__DEBUG) || defined(__DEBUG_D) && defined(__XC32)
    /* Unchanged from the original weak handler (exceptions.c): with a
     * debugger attached in a debug build, halt right here instead of
     * recording+resetting - the live register/frame state is more useful
     * to a human at the console than the persisted report would be. */
    __builtin_software_breakpoint();
#endif

    uint32_t pc    = frame[6];
    uint32_t lr    = frame[5];
    uint32_t psr   = frame[7];
    uint32_t ipsr  = __get_IPSR();   /* which vector we're actually in - see FaultName()'s comment */
    uint32_t cfsr  = SCB->CFSR;
    uint32_t hfsr  = SCB->HFSR;
    uint32_t bfar  = SCB->BFAR;
    uint32_t mmfar = SCB->MMFAR;

    char flags[160];
    flags[0] = '\0';
    /* UsageFault, CFSR bits 16-31 */
    append_flag(flags, sizeof flags, cfsr, 1u << 16, "UNDEFINSTR");
    append_flag(flags, sizeof flags, cfsr, 1u << 17, "INVSTATE");
    append_flag(flags, sizeof flags, cfsr, 1u << 18, "INVPC");
    append_flag(flags, sizeof flags, cfsr, 1u << 19, "NOCP");
    append_flag(flags, sizeof flags, cfsr, 1u << 24, "UNALIGNED");
    append_flag(flags, sizeof flags, cfsr, 1u << 25, "DIVBYZERO");
    /* BusFault, CFSR bits 8-15 */
    append_flag(flags, sizeof flags, cfsr, 1u << 8,  "IBUSERR");
    append_flag(flags, sizeof flags, cfsr, 1u << 9,  "PRECISERR");
    append_flag(flags, sizeof flags, cfsr, 1u << 10, "IMPRECISERR");
    append_flag(flags, sizeof flags, cfsr, 1u << 12, "STKERR");
    /* MemManage, CFSR bits 0-7 */
    append_flag(flags, sizeof flags, cfsr, 1u << 0,  "IACCVIOL");
    append_flag(flags, sizeof flags, cfsr, 1u << 1,  "DACCVIOL");
    if (flags[0] == '\0') {
        (void)snprintf(flags, sizeof flags, "(none decoded - raw CFSR above is authoritative)");
    }

    bool bfarValid  = (cfsr & (1u << 15)) != 0u;
    bool mmfarValid = (cfsr & (1u << 7))  != 0u;
    bool forced     = (hfsr & (1u << 30)) != 0u;

    (void)snprintf(s_crashlog.text, sizeof s_crashlog.text,
        "\n\r=== Last %s ===\n\r"
        "PC=0x%08lX  LR=0x%08lX  PSR=0x%08lX\n\r"
        "R0=0x%08lX  R1=0x%08lX  R2=0x%08lX  R3=0x%08lX  R12=0x%08lX\n\r"
        "CFSR=0x%08lX  HFSR=0x%08lX%s\n\r"
        "BFAR=0x%08lX (%s)  MMFAR=0x%08lX (%s)\n\r"
        "Flags: %s\n\r"
        "Resolve PC with: xc32-addr2line -f -e <build>.elf 0x%08lX\n\r"
        "======================\n\r",
        FaultName(ipsr),
        (unsigned long)pc, (unsigned long)lr, (unsigned long)psr,
        (unsigned long)frame[0], (unsigned long)frame[1], (unsigned long)frame[2],
        (unsigned long)frame[3], (unsigned long)frame[4],
        (unsigned long)cfsr, (unsigned long)hfsr,
        forced ? " (FORCED - escalated from Mem/Bus/UsageFault)" : "",
        (unsigned long)bfar, bfarValid ? "valid" : "not valid",
        (unsigned long)mmfar, mmfarValid ? "valid" : "not valid",
        flags,
        (unsigned long)pc);

    s_crashlog.magic = CRASHLOG_MAGIC;
    s_crashlog.shown = false;

    SYS_RESET_SoftwareReset();   /* reboots so CRASHLOG_PrintIfPresent() can show the report */
    for (;;) { }                 /* sys_reset.h's prototype carries no 'noreturn' - satisfy the compiler */
}

/* Same naked-stub recipe for all four fault vectors - each just hands its own
 * (untouched) exception frame to the one shared Fault_C() above. */
#define FAULT_HANDLER_STUB(name)                    \
void __attribute__((naked)) name(void)              \
{                                                    \
    __asm volatile (                                \
        "tst   lr, #4            \n"   /* EXC_RETURN bit 2: MSP or PSP? (bare-metal here: always MSP) */ \
        "ite   eq                \n"                \
        "mrseq r0, msp           \n"                \
        "mrsne r0, psp           \n"                \
        "b     Fault_C           \n"                \
    );                                               \
}

FAULT_HANDLER_STUB(HardFault_Handler)
FAULT_HANDLER_STUB(MemoryManagement_Handler)
FAULT_HANDLER_STUB(BusFault_Handler)
FAULT_HANDLER_STUB(UsageFault_Handler)

/* --- Deliberate test trigger -------------------------------------------------
 *
 * Not address-based on purpose: this SoC's bus fabric turned out to tolerate
 * several genuinely invalid poke addresses instead of bus-faulting on them
 * (Flash: write silently dropped by the NVM controller; external memory
 * region: bus access hangs forever instead of erroring, per
 * docs/session-log.md; a reserved PPB gap: also silently accepted) - it
 * routes at least some AHB-APB access errors through its own HPB0-3 status
 * flags (datasheet section 27.7.5) rather than a classic ARM bus fault. A
 * permanently-undefined instruction sidesteps all of that: the ARMv7-M
 * architecture itself guarantees "udf" raises a UsageFault - caught here by
 * UsageFault_Handler (one of the four FAULT_HANDLER_STUB instances above),
 * independent of any vendor bus implementation. */
void __attribute__((noinline)) CRASHLOG_TriggerTestFault(void)
{
    __asm volatile ("udf #0");
}

/* --- Boot-time print + CLI command ----------------------------------------- */

/* CMD_PRINT/SYS_CONSOLE_PRINT both format each call into a fixed buffer
 * before sending - confirmed on hardware that Telnet's is only
 * TCPIP_TELNET_PRINT_BUFF_SIZE (200 bytes, configuration.h): the report,
 * printed as a single ~400-byte "%s", truncated mid-line there. Print it one
 * line at a time instead - every line here is well under that limit, the
 * same way every other multi-line command in this project (dump, netinfo,
 * ...) already prints, rather than building one giant string.
 *
 * Backpressure, so a burst of lines can't silently drop bytes either:
 *   - pCmdIO != NULL (a command session, Telnet included): CMD_PRINT already
 *     goes through F_Telnet_MSG()'s own retry-on-real-write-count loop
 *     (telnet.c hand-patch, mcc-generated-code-patches.md item 8) - nothing
 *     extra needed here.
 *   - pCmdIO == NULL (the boot-time caller, serial console only, no command
 *     session): reuses app.c's DumpMem() idiom instead - busy-wait on
 *     SYS_CONSOLE_WriteFreeBufferCountGet() before each line, since nothing
 *     else guards the SERCOM TX ring buffer on that path. */
static void print_record_lines(SYS_CMD_DEVICE_NODE* pCmdIO)
{
    const char* p = s_crashlog.text;
    char line[130];
    while (*p != '\0') {
        size_t n = 0u;
        while ((p[n] != '\0') && (p[n] != '\n') && (n < (sizeof(line) - 2u))) {
            n++;
        }
        bool hasNewline = (p[n] == '\n');
        (void)memcpy(line, p, n);
        size_t len = n;
        if (hasNewline) {
            line[len++] = '\n';
        }
        line[len] = '\0';

        if (pCmdIO != NULL) {
            CMD_PRINT(pCmdIO, "%s", line);
        } else {
            while (SYS_CONSOLE_WriteFreeBufferCountGet(SYS_CONSOLE_DEFAULT_INSTANCE) < (ssize_t)len) {
                /* wait for the SERCOM TX interrupt to drain the ring buffer */
            }
            SYS_CONSOLE_PRINT("%s", line);
        }

        p += n;
        if (hasNewline) {
            p++;
        }
    }
}

void CRASHLOG_PrintIfPresent(void)
{
    if ((s_crashlog.magic == CRASHLOG_MAGIC) && !s_crashlog.shown) {
        print_record_lines(NULL);
        s_crashlog.shown = true;
    }
}

/* Plain helper, not its own SYS_CMD_ADDGRP group: MAX_CMD_GROUP (sys_command.h)
 * is 8, and this project's env/lan/span/noip/testserver/Test groups plus the
 * Harmony-registered iperf/tcpip groups already fill exactly that many slots.
 * A 9th group here silently starved whichever group registers last
 * (MIRROR_Initialize()'s "span" group, deferred to APP_STATE_SERVICE_TASKS) -
 * confirmed on hardware as "MIRROR: SYS_CMD_ADDGRP failed" at boot. Exposed
 * instead as a function app.c's own Test-group "faultlog" command calls, the
 * same way every other single-purpose Test diagnostic (meminfo, logstat, ...)
 * already lives directly in that one group. */
void CRASHLOG_PrintRecord(SYS_CMD_DEVICE_NODE* pCmdIO)
{
    if (s_crashlog.magic != CRASHLOG_MAGIC) {
        CMD_PRINT(pCmdIO, "faultlog: no fault recorded since Backup RAM was last blank\n\r");
        return;
    }
    print_record_lines(pCmdIO);
}

/* Without this, a fault recorded once stays reported forever - every later
 * boot (for an unrelated reason, or none at all) would keep showing the same
 * stale report, and nobody could tell which reset it actually happened on.
 * Invalidating the magic is enough: CRASHLOG_PrintIfPresent()/PrintRecord()
 * both already treat a non-matching magic as "nothing recorded". */
void CRASHLOG_ClearRecord(SYS_CMD_DEVICE_NODE* pCmdIO)
{
    s_crashlog.magic = 0u;
    s_crashlog.shown = false;
    CMD_PRINT(pCmdIO, "faultlog: cleared\n\r");
}
