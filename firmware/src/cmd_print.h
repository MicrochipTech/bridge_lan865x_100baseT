/* CMD_PRINT/CMD_MSG - reply to whichever device actually invoked a command
 * (the serial console or a Telnet session), instead of unconditionally to
 * the serial console the way SYS_CONSOLE_PRINT() does.
 *
 * Every SYS_CMD_FNC command handler is called with a SYS_CMD_DEVICE_NODE*
 * (pCmdIO) for exactly this reason - pCmdIO->pCmdApi->print/msg routes to
 * pCmdIO->cmdIoParam, the specific connection that typed the command.
 * SYS_CONSOLE_PRINT always targets SYS_CONSOLE_DEFAULT_INSTANCE (the fixed
 * serial console) regardless of who asked - using it inside a command's own
 * reply means a Telnet user sees nothing while the answer goes out the
 * serial port instead. Root-caused 2026-08-31 (see docs/session-log.md)
 * after "showenv" and other commands produced no output at all over Telnet.
 *
 * Use CMD_PRINT/CMD_MSG for anything that IS a reply to the command that is
 * executing. Leave boot-time, periodic-timer, and interrupt-adjacent
 * diagnostic prints (nothing to do with any specific command invocation, no
 * pCmdIO available anyway) as plain SYS_CONSOLE_PRINT - they are correct as
 * they are. */
#ifndef CMD_PRINT_H
#define CMD_PRINT_H

#include "system/command/sys_command.h"

#define CMD_PRINT(pCmdIO, ...) (*(pCmdIO)->pCmdApi->print)((pCmdIO)->cmdIoParam, __VA_ARGS__)
#define CMD_MSG(pCmdIO, str)   (*(pCmdIO)->pCmdApi->msg)((pCmdIO)->cmdIoParam, (str))

/* For a result that completes asynchronously (e.g. a queued SPI register
 * transaction whose "OK"/value line prints later, from a background task,
 * not synchronously inside the command handler that started it): the module
 * remembers which pCmdIO (if any) started the pending operation and uses
 * this to print the eventual result there. NULL means "nobody asked" (the
 * operation was started from boot/background code, not a command) - falls
 * back to the serial console, same as before this pattern existed. */
#define CMD_PRINT_OR_CONSOLE(pCmdIO, ...) \
    do { \
        if ((pCmdIO) != NULL) { CMD_PRINT((pCmdIO), __VA_ARGS__); } \
        else { SYS_CONSOLE_PRINT(__VA_ARGS__); } \
    } while (0)

#endif /* CMD_PRINT_H */
