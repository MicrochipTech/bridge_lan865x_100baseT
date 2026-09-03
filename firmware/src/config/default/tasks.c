/*******************************************************************************
 System Tasks File

  File Name:
    tasks.c

  Summary:
    This file contains source code necessary to maintain system's polled tasks.

  Description:
    This file contains source code necessary to maintain system's polled tasks.
    It implements the "SYS_Tasks" function that calls the individual "Tasks"
    functions for all polled MPLAB Harmony modules in the system.

  Remarks:
    This file requires access to the systemObjects global data structure that
    contains the object handles to all MPLAB Harmony module objects executing
    polled in the system.  These handles are passed into the individual module
    "Tasks" functions to identify the instance of the module to maintain.
 *******************************************************************************/

// DOM-IGNORE-BEGIN
/*******************************************************************************
* Copyright (C) 2018 Microchip Technology Inc. and its subsidiaries.
*
* Subject to your compliance with these terms, you may use Microchip software
* and any derivatives exclusively with Microchip products. It is your
* responsibility to comply with third party license terms applicable to your
* use of third party software (including open source software) that may
* accompany Microchip software.
*
* THIS SOFTWARE IS SUPPLIED BY MICROCHIP "AS IS". NO WARRANTIES, WHETHER
* EXPRESS, IMPLIED OR STATUTORY, APPLY TO THIS SOFTWARE, INCLUDING ANY IMPLIED
* WARRANTIES OF NON-INFRINGEMENT, MERCHANTABILITY, AND FITNESS FOR A
* PARTICULAR PURPOSE.
*
* IN NO EVENT WILL MICROCHIP BE LIABLE FOR ANY INDIRECT, SPECIAL, PUNITIVE,
* INCIDENTAL OR CONSEQUENTIAL LOSS, DAMAGE, COST OR EXPENSE OF ANY KIND
* WHATSOEVER RELATED TO THE SOFTWARE, HOWEVER CAUSED, EVEN IF MICROCHIP HAS
* BEEN ADVISED OF THE POSSIBILITY OR THE DAMAGES ARE FORESEEABLE. TO THE
* FULLEST EXTENT ALLOWED BY LAW, MICROCHIP'S TOTAL LIABILITY ON ALL CLAIMS IN
* ANY WAY RELATED TO THIS SOFTWARE WILL NOT EXCEED THE AMOUNT OF FEES, IF ANY,
* THAT YOU HAVE PAID DIRECTLY TO MICROCHIP FOR THIS SOFTWARE.
 *******************************************************************************/
// DOM-IGNORE-END

// *****************************************************************************
// *****************************************************************************
// Section: Included Files
// *****************************************************************************
// *****************************************************************************

#include "configuration.h"
#include "definitions.h"
#include "sys_tasks.h"
#include "cpuload.h"          /* HAND-PATCH to MCC-generated code, documented exception (CLAUDE.md section 3) */




// *****************************************************************************
// *****************************************************************************
// Section: System "Tasks" Routine
// *****************************************************************************
// *****************************************************************************

/*******************************************************************************
  Function:
    void SYS_Tasks ( void )

  Remarks:
    See prototype in system/common/sys_module.h.
*/
void SYS_Tasks ( void )
{
    /* HAND-PATCH to MCC-generated code, documented exception (CLAUDE.md section 3):
       CPULOAD_Enter()/CPULOAD_Exit() bracket each call below and the whole pass
       (see docs/mcc-generated-code-patches.md item 12) - no-ops unless 'cpuload on'.
       CPULOAD_LivePoll() must run first, before SYS_CMD_Tasks() and before the
       CPULOAD_Enter() below - see its own comment in cpuload.h for why. */
    CPULOAD_LivePoll();
    CPULOAD_Enter(CPULOAD_SLOT_TOTAL);

    /* Maintain system services */


CPULOAD_Enter(CPULOAD_SLOT_SYS_CMD);
SYS_CMD_Tasks();
CPULOAD_Exit(CPULOAD_SLOT_SYS_CMD);




    /* Maintain Device Drivers */
CPULOAD_Enter(CPULOAD_SLOT_MIIM);
       DRV_MIIM_OBJECT_BASE_Default.miim_Tasks(sysObj.drvMiim_0);
CPULOAD_Exit(CPULOAD_SLOT_MIIM);




    /* Maintain Middleware & Other Libraries */

CPULOAD_Enter(CPULOAD_SLOT_TCPIP);
   TCPIP_STACK_Task(sysObj.tcpip);
CPULOAD_Exit(CPULOAD_SLOT_TCPIP);



CPULOAD_Enter(CPULOAD_SLOT_NET_PRES);
NET_PRES_Tasks(sysObj.netPres);
CPULOAD_Exit(CPULOAD_SLOT_NET_PRES);




    /* Maintain the application's state machine. */
        /* Call Application task APP. */
CPULOAD_Enter(CPULOAD_SLOT_APP);
    APP_Tasks();
CPULOAD_Exit(CPULOAD_SLOT_APP);

    CPULOAD_Exit(CPULOAD_SLOT_TOTAL);
}

/*******************************************************************************
 End of File
 */

