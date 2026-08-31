/*
 * env.h - persistent network "environment" for the bridge firmware.
 *
 * A versioned, CRC-protected network config (per-interface IP/mask/gateway/DNS)
 * stored in the Emulated EEPROM. Compiled defaults come from configuration.h; on
 * first boot (blank EEPROM) they are seeded. CLI: showenv/setenv/saveenv/readenv/resetenv.
 */
#ifndef ENV_H
#define ENV_H

/* Load the config from the Emulated EEPROM (or seed it from the compiled defaults
 * on first boot) and register the "env" CLI command group. Called once from
 * SYS_Initialize (initialization.c), right after EMU_EEPROM_Initialize() and
 * before TCPIP_STACK_Init() - so the persistent MAC is ready before the stack
 * binds it. */
void ENV_Init(void);

/* Push the current config into the TCP/IP stack (TCPIP_STACK_NetAddressSet/...) and
 * the LAN865x PLCA (via LAN865X_DIAG_ApplyPlca). Call once the stack is up
 * (APP_STATE_SERVICE_TASKS). */
void env_apply(void);

/* Current PLCA node id / node count from the env (eth0 / LAN865x). */
#include <stdint.h>
#include <stdbool.h>
uint8_t env_plca_id(void);
uint8_t env_plca_cnt(void);

/* Should the eth0->eth1 port mirror come up enabled? Read by MIRROR_Initialize(),
 * which runs after ENV_Init(). Persisted with 'setenv mirror 1' + 'saveenv'; the
 * 'mirror' command changes the live state only. */
bool env_mirror(void);

/* Should this board come up as a permanent T1S sniffer - PHY transmitter (T1SPMACTL.TXD)
 * suppressed from the very first LAN865x init step, before NETWORK_CONTROL (TXEN) is
 * ever written? Read in SYS_Initialize() (initialization.c), right after ENV_Init() and
 * before TCPIP_STACK_Init(), to feed drvLan865xInitData[0].suppressTx - same pattern as
 * env_plca_id()/env_plca_cnt(). Also read by MIRROR_Initialize() to set the RAM-only
 * s_sniffer_on flag (port_mirror.c) that drives the RX-hook filtering; the 'sniffer'
 * command changes the live state (and the hardware TXD bit) only. */
bool env_sniffer(void);

/* Format the env MAC for interface 0/1 as "XX:XX:XX:XX:XX:XX" into buf (>= 18 bytes).
 * Call after ENV_Init(), before TCPIP_STACK_Init(), to fill the stack's MAC strings. */
void env_mac_str(int iface, char *buf);

#endif /* ENV_H */
