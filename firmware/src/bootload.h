/*******************************************************************************
  Firmware self-update into the inactive flash bank (dual-bank bootloader)

  File Name:
    bootload.h

  Summary:
    Receives a new firmware image over its own TCP port and programs it into
    the flash bank the CPU is NOT executing from, while the application keeps
    running. Control (arm/abort/status/verify) goes over the normal console -
    serial or Telnet - the image itself over a separate binary socket.

  Description:
    The SAME54's NVM controller implements dual banks natively: 2 x 512 KiB,
    the active one always mapped at 0x00000000 and the other one at
    0x00080000, with a single atomic command (BKSWRST) to swap them and reset.
    The firmware is therefore always linked at 0 - one build, one HEX, no
    per-bank link address - and programming the other bank does not stall code
    fetch from this one (Read-While-Write, datasheet 25.6.6.3).

    Concept and the reasoning behind every constant here:
    docs/dual-bank-bootloader-concept.md, then docs/dual-bank-bootloader-plan.md.

    Two things have to be right for a remote update not to lose the board:

    1. The image never reaches the top 16 KiB of the target bank (0xFC000),
       because that window holds the LIVE emulated EEPROM - the network
       configuration the board is reachable under while the update runs. The
       image size limit (0x7C000, the same value as the project's ROM_LENGTH)
       is what guarantees this structurally.
    2. 'commit' copies that window into the other bank before it swaps, so the
       new firmware finds the environment where it looks for it. Without that
       step a successful update comes back at the compiled-in default IP. See
       bl_commit() in bootload.c and docs/dual-bank-bootloader-plan.md section 4.

    A failed or abandoned transfer changes nothing: only the inactive bank is
    written, and nothing boots from there until BKSWRST.

  Dependencies:
    TCPIP_STACK_USE_TCP (already on - iperf and testserver use it) and the
    MCC-generated NVMCTRL PLIB (already generated for the emulated EEPROM).
*******************************************************************************/

#ifndef BOOTLOAD_H
#define BOOTLOAD_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Register the console commands ('bootload'). Call once, after SYS_CMD is up. */
void BOOTLOAD_Initialize(void);

/* Drive the state machine. Call every APP_Tasks() cycle. */
void BOOTLOAD_Tasks(void);

/* True from 'bootload arm' until the transfer has finished, failed or been
 * aborted. For anything that might want to keep its own flash writes out of
 * the way while an update is in flight - the NVM page buffer is shared between
 * both banks (datasheet 25.6.6.2), so two writers must not interleave. */
bool BOOTLOAD_IsActive(void);

#ifdef __cplusplus
}
#endif

#endif /* BOOTLOAD_H */
