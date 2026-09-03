/*******************************************************************************
  Onboard LED1 on/off control - interface

  See leds.c for the pin (PC21) and polarity (active-low). LEDS_Initialize()
  must run once from APP_Initialize() before either LEDS_Led1Set() or the
  "led" Test-group command in app.c is used.
*******************************************************************************/
#ifndef LEDS_H
#define LEDS_H

#include <stdbool.h>

void LEDS_Initialize(void);
void LEDS_Led1Set(bool on);

#endif /* LEDS_H */
