/*******************************************************************************
  eth1 (LAN867x) MDIO register access - the LAN867x counterpart to
  lan865x_diag.c's lan_read/lan_write, for the T1S<->T1S branch where eth1 was
  switched from LAN8742A/LAN8740A (100BASE-TX) to LAN867x (10BASE-T1S).

  Summary:
    Implementation of the diagnostics layer described in lan867x_diag.h.

  Description:
    eth0's LAN865x is a SPI MAC-PHY with its own async register API
    (DRV_LAN865X_ReadRegister/WriteRegister, see lan865x_diag.c). eth1's
    LAN867x is a plain external PHY reached over MDIO/MIIM instead, via the
    clause-22/clause-45 helpers the LAN867x driver itself already exports
    (LAN867x_Read_Register/LAN867x_Write_Register, drv_extphy_lan867x.h) - this
    module is a thin, single-slot command wrapper around exactly those, the
    same shape as lan865x_diag.c but without the LAN865x-specific PLCA/SQI/
    test-mode machinery (eth1's PLCA/SQI registers exist too, reachable at the
    same addresses documented in drv_extphy_lan867x.h's ePHY_VENDOR_REG, but
    no CLI beyond plain read/write is built for them here).
 *******************************************************************************/

#ifndef LAN867X_DIAG_H
#define LAN867X_DIAG_H

/* Register access state machine service - call once per main-loop pass,
   alongside LAN865X_DIAG_Tasks(). Does nothing while idle. */
void LAN867X_DIAG_Tasks(void);

/* Registers the eth1_read/eth1_write/eth1help console commands. Call once
   from APP_Initialize()/APP_STATE_INIT, alongside LAN865X_DIAG_Initialize(). */
void LAN867X_DIAG_Initialize(void);

#endif /* LAN867X_DIAG_H */
