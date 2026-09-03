/*******************************************************************************
  Fault self-analysis (HardFault/MemManage/BusFault/UsageFault) - interface

  See crashlog.c for the mechanism (persistent Backup RAM, no debugger
  needed). Two integration points:
    - CRASHLOG_PrintIfPresent() from APP_STATE_SERVICE_TASKS, once the console
      is confirmed up (same point as the "Build Timestamp" banner) - prints
      the last recorded fault once per power-up, if there is one.
    - CRASHLOG_PrintRecord() called from app.c's own "faultlog" Test-group
      command handler, to show the same record on demand. Not its own CLI
      command group - see the comment on CRASHLOG_PrintRecord() in crashlog.c
      for why (MAX_CMD_GROUP is already fully subscribed).
  The four fault handlers themselves need no call from application code -
  they override the weak defaults from exceptions.c by simply existing in
  the link.
*******************************************************************************/
#ifndef CRASHLOG_H
#define CRASHLOG_H

#include "system/command/sys_command.h"   /* SYS_CMD_DEVICE_NODE */

void CRASHLOG_PrintIfPresent(void);
void CRASHLOG_PrintRecord(SYS_CMD_DEVICE_NODE* pCmdIO);
void CRASHLOG_ClearRecord(SYS_CMD_DEVICE_NODE* pCmdIO);

/* Deliberately triggers a HardFault (permanently-undefined instruction) -
 * exists only to test the mechanism above on demand from the "crashtest"
 * Test-group command in app.c. Never returns. */
void CRASHLOG_TriggerTestFault(void);

#endif /* CRASHLOG_H */
