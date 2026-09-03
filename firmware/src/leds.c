/*******************************************************************************
  Onboard LED1 on/off control

  This is a plain user file, not MCC-generated code - the SAM E54 Curiosity
  Ultra's onboard LEDs were never assigned through MCC's Pin Configurator in
  this project (pin_configurations.csv marks every pin used by this firmware;
  PC21 is not one of them). Direct PORT_REGS register access, same pattern as
  MCC's own generated LAN865x_RESET_Set()/_Clear() macros in
  config/default/peripheral/port/plib_port.h - just for a pin MCC doesn't
  know about.

  Which pin, and how this was pinned down:
  The board's silkscreen labels its two onboard LEDs "LED1"/"LED2" (visible
  in docs/images/eval-board-sam-e54-curiosity-t1s-click.jpg, next to SW2/
  DAC0/DAC1/PCC INTERFACE). Guessing PC18/PC19 from memory was wrong - PC18
  turned out to be the X32 header's own Reset line (which is why this
  project already uses it for LAN865x_RESET; nothing to do with an LED), and
  driving PC19 as tested produced no visible LED. The actual net names and
  pins are only in the schematic PDF
  ("SAM E54 Curiosity Ultra_R3_Design_Documentation.PDF", "MCU" schematic
  sheet): LED1 (D400, yellow) = net USER_LED0 = PC21; LED2 (D401, yellow) =
  net USER_LED2 = PA16. This file drives LED1/PC21.

  Polarity: the schematic shows VCC_3P3V -> R467 (330R) -> D400 (LED1) ->
  PC21, i.e. the GPIO sinks current to light the LED - active-low, not
  active-high. LEDS_Led1Set(true) therefore drives the pin LOW, and
  LEDS_Initialize() leaves it HIGH (off) at power-up.
*******************************************************************************/
#include "leds.h"
#include "definitions.h"   /* PORT_REGS */

#define LED1_PORT_GROUP   2U    /* Port C */
#define LED1_PIN_BIT      21U   /* PC21 */

void LEDS_Initialize(void) {
    PORT_REGS->GROUP[LED1_PORT_GROUP].PORT_OUTSET = ((uint32_t)1U << LED1_PIN_BIT);  /* off (active-low) */
    PORT_REGS->GROUP[LED1_PORT_GROUP].PORT_DIRSET = ((uint32_t)1U << LED1_PIN_BIT);
}

void LEDS_Led1Set(bool on) {
    if (on) {
        PORT_REGS->GROUP[LED1_PORT_GROUP].PORT_OUTCLR = ((uint32_t)1U << LED1_PIN_BIT);
    } else {
        PORT_REGS->GROUP[LED1_PORT_GROUP].PORT_OUTSET = ((uint32_t)1U << LED1_PIN_BIT);
    }
}
