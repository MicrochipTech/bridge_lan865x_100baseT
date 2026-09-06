/*******************************************************************************
  Onboard LED1 on/off control

  LED1/LED2 (PC21/PA16) are now assigned through MCC's Pin Configurator
  (pin_configurations.csv), so this just wraps the generated LED1_Set()/
  LED1_Clear() macros (config/default/peripheral/port/plib_port.h) - no more
  raw PORT_REGS access. MCC's own PORT_Initialize() (SYS_Initialize(), before
  APP_Initialize() ever runs) already sets PC21 as an output, initially HIGH;
  LEDS_Initialize() below is therefore redundant defensiveness, not what
  actually establishes that state.

  Net names and polarity (from "SAM E54 Curiosity Ultra_R3_Design_
  Documentation.PDF", "Target MCU" schematic sheet, confirmed against the
  render at 6x zoom): LED1 (D400, yellow) = net USER_LED0 = PC21; LED2 (D401,
  yellow) = net USER_LED2 = PA16. This file drives LED1/PC21.

  Polarity: the schematic shows VCC_3P3V -> R467 (330R) -> D400 (LED1) ->
  PC21, i.e. the GPIO sinks current to light the LED - active-low, not
  active-high. LEDS_Led1Set(true) therefore calls LED1_Clear() (drives LOW),
  and LEDS_Initialize() calls LED1_Set() (drives HIGH, off).
*******************************************************************************/
#include "leds.h"
#include "definitions.h"   /* LED1_Set()/LED1_Clear()/LED1_OutputEnable() */

void LEDS_Initialize(void) {
    LED1_Set();             /* off (active-low) */
    LED1_OutputEnable();
}

void LEDS_Led1Set(bool on) {
    if (on) {
        LED1_Clear();
    } else {
        LED1_Set();
    }
}
