/*
 * cert_provision.h - remote TLS identity replacement for this board.
 *
 * Lets an already-authenticated operator (someone who already holds a valid
 * client certificate for the CA this board currently trusts - see
 * net_pres_enc_glue.c) push a NEW server certificate/key (and optionally a
 * new trusted CA) onto this board over the network, without a firmware
 * rebuild or a physical connection. The existing mTLS session IS the
 * authorization: only the 'cert' console commands, reachable only over the
 * already-encrypted, already-client-certificate-gated Telnet console, can
 * arm this at all.
 *
 * Modeled directly on bootload.c's split (short commands over the console,
 * the actual binary payload over its own port, TCP/5568) - the same
 * reasoning applies: an 80-byte Telnet line buffer
 * (TCPIP_TELNET_LINE_BUFF_SIZE) cannot carry a ~1 KB certificate as hex text
 * in one command.
 *
 * Safety property this is built around, same as bootload.c's own: a failed,
 * aborted, or interrupted operation changes nothing live. New items are
 * CRC-verified into a RAM staging copy; 'cert save' is the only thing that
 * writes to the Emulated EEPROM, and it writes a complete, freshly-CRC'd
 * record in one call. On boot, net_pres_enc_glue.c only trusts that EEPROM
 * record if its own CRC checks out - anything else (blank, corrupt, or a
 * board that has never run this) falls back to the compiled-in
 * bridge_certs.h identity. There is no state this can reach where TLS
 * cannot start at all.
 */
#ifndef CERT_PROVISION_H
#define CERT_PROVISION_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Register the 'cert' console command group and load whatever is currently
 * in the Emulated EEPROM (if valid) into the active-identity cache. Call
 * once, after SYS_CMD and the Emulated EEPROM are both up - same place as
 * ENV_Init(). */
void CERT_PROVISION_Initialize(void);

/* Drive the state machine (the data-port transfer). Call every APP_Tasks()
 * cycle, same as BOOTLOAD_Tasks(). */
void CERT_PROVISION_Tasks(void);

/* The identity net_pres_enc_glue.c should actually load into the WOLFSSL_CTX
 * at EncGlue_Init() time: the persisted EEPROM override if one validated at
 * boot, otherwise the compiled-in bridge_certs.h arrays (fromEeprom is set
 * to false in that case, only for diagnostics/'cert show'). Never returns a
 * NULL cert/key pointer or a zero length - the compiled-in fallback always
 * backs this up. */
void CERT_PROVISION_ActiveServerIdentity(const uint8_t **certDer, uint32_t *certLen,
                                          const uint8_t **keyDer, uint32_t *keyLen,
                                          bool *fromEeprom);

/* Same for the CA used to verify client certificates. */
void CERT_PROVISION_ActiveCa(const uint8_t **caDer, uint32_t *caLen, bool *fromEeprom);

#ifdef __cplusplus
}
#endif

#endif /* CERT_PROVISION_H */
