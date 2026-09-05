/*
 * cert_provision.c - see cert_provision.h for the design and the safety
 * property this is built around.
 */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "definitions.h"
#include "configuration.h"
#include "config/default/system/console/sys_console.h"
#include "config/default/system/time/sys_time.h"
#include "config/default/library/emulated_eeprom/emulated_eeprom.h"
#include "system/command/sys_command.h"
#include "cert_provision.h"
#include "bridge_certs.h"      /* compiled-in fallback identity */
#include "cmd_print.h"

// *****************************************************************************
// Section: Storage layout
// *****************************************************************************

#define CERT_MAGIC        0x54524543u          /* 'CERT' */
#define CERT_VERSION      1u
/* The emulated EEPROM's usable capacity is EEPROM_EMULATOR_NUM_LOGICAL_PAGES
 * (8) * EEPROM_EMULATOR_PAGE_DATA_SIZE (508) = 4064 bytes total - it is NOT
 * arbitrary, it is exactly what fits in bootload.c's BL_ENV_SIZE (16 KiB,
 * 2 flash blocks), the fixed "environment window" at the top of every bank
 * that a firmware image can never touch (WP3) and that bl_commit() shadow-
 * copies across a bank swap. Enlarging that window means growing it
 * consistently in both banks and touching the already-hardened bootloader -
 * out of scope here (see the "can the EEPROM be made bigger" discussion).
 * 1536 (the original guess, "generous for an RSA-2048 cert or key DER") put
 * 3 slots + overhead at 4632 bytes - past both the 4064-byte total AND the
 * old CERT_EE_OFFSET=4096, which was itself already past the valid range
 * (confirmed live: every 'cert_save' failed with "EEPROM write failed").
 * 1280 covers every DER actually observed (879-1192 bytes, RSA-2048
 * cert/key) with real margin and 3*1280+24=3864 bytes fits comfortably
 * after env.c's 72-byte record. */
#define CERT_MAX_DER       1280u
#define CERT_EE_OFFSET     128u                 /* clear of env.c's 72-byte record at offset 0 */
#define CERT_PORT_DEFAULT  5568u
#define CERT_CONN_TIMEOUT_MS   30000u           /* time allowed to sit in WAIT_CONN once armed */
#define CERT_STALL_TIMEOUT_MS  10000u           /* time allowed between bytes once receiving */

typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t server_cert_len;
    uint8_t  server_cert_der[CERT_MAX_DER];
    uint32_t server_key_len;
    uint8_t  server_key_der[CERT_MAX_DER];
    uint32_t ca_cert_len;
    uint8_t  ca_cert_der[CERT_MAX_DER];
    uint32_t crc32;
} cert_store_t;

/* RAM working copy: seeded at Initialize() from either a valid EEPROM record
 * or the compiled-in defaults, then 'cert arm'+the data transfer overwrite
 * one item at a time. 'cert save' is the only thing that writes this back to
 * the EEPROM - arming and receiving alone never touch flash. */
static cert_store_t s_staged;
static bool         s_staged_from_eeprom;   /* for 'cert show' only */

typedef enum { CP_IDLE, CP_WAIT_CONN, CP_RECV } cp_state_t;
static cp_state_t s_state = CP_IDLE;

static NET_PRES_SKT_HANDLE_T s_sock = INVALID_SOCKET;
static NET_PRES_SKT_PORT_T   s_port = (NET_PRES_SKT_PORT_T)CERT_PORT_DEFAULT;

typedef enum { CP_ITEM_NONE, CP_ITEM_SERVER_CERT, CP_ITEM_SERVER_KEY, CP_ITEM_CA_CERT } cp_item_t;
static cp_item_t s_armed_item = CP_ITEM_NONE;
static uint32_t  s_armed_size;
static uint32_t  s_armed_crc;
static uint8_t   s_recv_buf[CERT_MAX_DER];
static uint32_t  s_recv_len;
static uint64_t  s_deadline;
static uint64_t  s_ticks_per_ms;

static SYS_CMD_DEVICE_NODE *s_cp_pCmdIO = NULL;

// *****************************************************************************
// Section: CRC32 (CRC-32/ISO-HDLC, same polynomial zlib.crc32 uses on the
// Python side - scripts/cert_provision.py relies on this matching exactly,
// the same way scripts/bootload.py already relies on bootload.c's own CRC32
// matching zlib's).
// *****************************************************************************

static uint32_t cp_crc32(const uint8_t *data, uint32_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    uint32_t i;
    for (i = 0u; i < len; i++) {
        uint32_t byte = data[i];
        uint32_t j;
        crc ^= byte;
        for (j = 0u; j < 8u; j++) {
            uint32_t mask = (uint32_t)(-(int32_t)(crc & 1u));
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

static uint32_t cp_store_crc(const cert_store_t *s)
{
    /* Every field up to (not including) crc32 itself. */
    return cp_crc32((const uint8_t *)s, (uint32_t)offsetof(cert_store_t, crc32));
}

// *****************************************************************************
// Section: Load / seed
// *****************************************************************************

static void cp_seed_from_compiled_defaults(cert_store_t *s)
{
    memset(s, 0, sizeof *s);
    s->magic   = CERT_MAGIC;
    s->version = CERT_VERSION;
    s->server_cert_len = g_bridge_server_cert_der_len;
    memcpy(s->server_cert_der, g_bridge_server_cert_der, g_bridge_server_cert_der_len);
    s->server_key_len = g_bridge_server_key_der_len;
    memcpy(s->server_key_der, g_bridge_server_key_der, g_bridge_server_key_der_len);
    s->ca_cert_len = g_bridge_ca_cert_der_len;
    memcpy(s->ca_cert_der, g_bridge_ca_cert_der, g_bridge_ca_cert_der_len);
}

// *****************************************************************************
// Section: Active identity (net_pres_enc_glue.c calls these)
// *****************************************************************************

/* Defensive, not just tidy: EncGlue_Init() (net_pres_enc_glue.c) is called by
 * NET_PRES the first time anything opens an encrypted socket, and nothing
 * here controls exactly when that first is relative to
 * CERT_PROVISION_Initialize() running from APP_Initialize(). If it somehow
 * ran first, s_staged would still be its zero-initialized state (magic 0,
 * every length 0) and the WOLFSSL_CTX setup below would fail on a 0-length
 * certificate buffer - silently disabling TLS entirely. Self-seed from the
 * compiled-in default the first time either accessor is actually used,
 * independent of init ordering. */
static void cp_ensure_seeded(void)
{
    if (s_staged.magic != CERT_MAGIC) {
        cp_seed_from_compiled_defaults(&s_staged);
        s_staged_from_eeprom = false;
    }
}

void CERT_PROVISION_ActiveServerIdentity(const uint8_t **certDer, uint32_t *certLen,
                                          const uint8_t **keyDer, uint32_t *keyLen,
                                          bool *fromEeprom)
{
    cp_ensure_seeded();
    *certDer = s_staged.server_cert_der;
    *certLen = s_staged.server_cert_len;
    *keyDer  = s_staged.server_key_der;
    *keyLen  = s_staged.server_key_len;
    *fromEeprom = s_staged_from_eeprom;
}

void CERT_PROVISION_ActiveCa(const uint8_t **caDer, uint32_t *caLen, bool *fromEeprom)
{
    cp_ensure_seeded();
    *caDer = s_staged.ca_cert_der;
    *caLen = s_staged.ca_cert_len;
    *fromEeprom = s_staged_from_eeprom;
}

// *****************************************************************************
// Section: Console commands
// *****************************************************************************

static const char *cp_item_name(cp_item_t item)
{
    switch (item) {
        case CP_ITEM_SERVER_CERT: return "server cert";
        case CP_ITEM_SERVER_KEY:  return "server key";
        case CP_ITEM_CA_CERT:     return "CA cert";
        default:                  return "?";
    }
}

static void cp_close_socket(void)
{
    if (s_sock != INVALID_SOCKET) {
        NET_PRES_SocketClose(s_sock);
        s_sock = INVALID_SOCKET;
    }
}

/* Reports a transfer's outcome on the still-open DATA socket only -
 * scripts/cert_provision.py reads its verdict from there, matching
 * bootload.c's own 'closing' line (read back by scripts/bootload.py).
 *
 * Originally this also echoed the same text to the console via
 * CMD_PRINT_OR_CONSOLE(s_cp_pCmdIO, ...), on the idea of "anyone watching the
 * console live" - but s_cp_pCmdIO is the very Telnet session that issued
 * 'cert_arm' in the first place, and TCPIP_TELNET_MAX_CONNECTIONS is 1, so
 * there is no *other* console to watch from: the only console is the one
 * about to send the *next* command (push_board() arms server_cert, then
 * server_key, then 'cert_save', all on one connection). That console echo
 * landed in the same stream as the next command's reply, so
 * scripts/bootload.py's Console.command() - "every command answers with
 * exactly one line" - misread it as that reply instead (confirmed live:
 * arming server_key failed with the server_cert completion line as its
 * "reply"). Same class of bug as cmd_cert_show's earlier multi-line fix.
 * Call BEFORE cp_close_socket(). */
static void cp_report(const char *fmt, ...)
{
    char buf[160];
    va_list args;
    int n;

    va_start(args, fmt);
    n = vsnprintf(buf, sizeof buf, fmt, args);
    va_end(args);
    if (n <= 0) {
        return;
    }
    if ((size_t)n >= sizeof buf) {
        n = (int)sizeof buf - 1;
    }
    if (s_sock != INVALID_SOCKET) {
        (void)NET_PRES_SocketWrite(s_sock, (const uint8_t *)buf, (uint16_t)n);
        (void)NET_PRES_SocketFlush(s_sock);
    }
}

static void cp_abort(const char *why)
{
    cp_close_socket();
    s_state = CP_IDLE;
    s_armed_item = CP_ITEM_NONE;
    if (why != NULL) {
        CMD_PRINT_OR_CONSOLE(s_cp_pCmdIO, "CERT: aborted (%s)\n\r", why);
    }
}

static void cmd_cert_arm(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    cp_item_t item;

    if (argc != 4) {
        CMD_PRINT(pCmdIO, "Usage: cert arm <server_cert|server_key|ca_cert> <bytes> <crc32hex>\n\r");
        return;
    }
    if (s_state != CP_IDLE) {
        CMD_PRINT(pCmdIO, "CERT: ERR already armed/receiving (state=%d)\n\r", (int)s_state);
        return;
    }
    if (strcmp(argv[1], "server_cert") == 0) {
        item = CP_ITEM_SERVER_CERT;
    } else if (strcmp(argv[1], "server_key") == 0) {
        item = CP_ITEM_SERVER_KEY;
    } else if (strcmp(argv[1], "ca_cert") == 0) {
        item = CP_ITEM_CA_CERT;
    } else {
        CMD_PRINT(pCmdIO, "CERT: ERR unknown item '%s' (server_cert|server_key|ca_cert)\n\r", argv[1]);
        return;
    }

    s_armed_size = (uint32_t)strtoul(argv[2], NULL, 0);
    s_armed_crc  = (uint32_t)strtoul(argv[3], NULL, 16);
    if ((s_armed_size == 0u) || (s_armed_size > CERT_MAX_DER)) {
        CMD_PRINT(pCmdIO, "CERT: ERR size %lu not in 1..%lu\n\r",
                  (unsigned long)s_armed_size, (unsigned long)CERT_MAX_DER);
        return;
    }

    s_sock = NET_PRES_SocketOpen(0, NET_PRES_SKT_ENCRYPTED_STREAM_SERVER,
                                  (NET_PRES_SKT_ADDR_T)IP_ADDRESS_TYPE_IPV4,
                                  s_port, NULL, NULL);
    if (s_sock == INVALID_SOCKET) {
        CMD_PRINT(pCmdIO, "CERT: ERR server-open\n\r");
        return;
    }

    s_armed_item = item;
    s_recv_len   = 0u;
    s_deadline   = SYS_TIME_Counter64Get() + (uint64_t)CERT_CONN_TIMEOUT_MS * s_ticks_per_ms;
    s_state      = CP_WAIT_CONN;
    s_cp_pCmdIO  = pCmdIO;
    CMD_PRINT(pCmdIO, "CERT: READY port=%u item=%s bytes=%lu crc=%08lX\n\r",
              (unsigned)CERT_PORT_DEFAULT, cp_item_name(item),
              (unsigned long)s_armed_size, (unsigned long)s_armed_crc);
}

static void cmd_cert_abort(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    if (s_state == CP_IDLE) {
        CMD_PRINT(pCmdIO, "CERT: nothing armed\n\r");
        return;
    }
    s_cp_pCmdIO = pCmdIO;
    cp_abort("by operator");
}

/* ONE line, not four: scripts/bootload.py's Console.command() (reused as-is
 * by scripts/cert_provision.py) assumes exactly one line starting with the
 * marker per command - see its own docstring. A multi-line reply here left
 * lines 2-4 sitting unread in the socket, where the NEXT command's
 * Console.command() call then misread them as its own reply (confirmed
 * live against real hardware before this fix). Matches 'bootload info's own
 * one-line-many-fields style. */
static void cmd_cert_show(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    CMD_PRINT(pCmdIO,
        "CERT: source=%s server_cert=%luB server_key=%luB ca_cert=%luB server_cert_crc=%08lX "
        "(save+reset to activate)\n\r",
        s_staged_from_eeprom ? "eeprom" : "compiled-in",
        (unsigned long)s_staged.server_cert_len, (unsigned long)s_staged.server_key_len,
        (unsigned long)s_staged.ca_cert_len,
        (unsigned long)cp_crc32(s_staged.server_cert_der, s_staged.server_cert_len));
}

static void cmd_cert_save(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    if (s_state != CP_IDLE) {
        CMD_PRINT(pCmdIO, "CERT: ERR a transfer is still in progress\n\r");
        return;
    }
    s_staged.magic   = CERT_MAGIC;
    s_staged.version = CERT_VERSION;
    s_staged.crc32   = cp_store_crc(&s_staged);

    if (EMU_EEPROM_BufferWrite(CERT_EE_OFFSET, (const uint8_t *)&s_staged, (uint16_t)sizeof s_staged)
            != EMU_EEPROM_STATUS_OK) {
        CMD_PRINT(pCmdIO, "CERT: ERR EEPROM write failed - nothing changed\n\r");
        return;
    }
    if (EMU_EEPROM_PageBufferCommit() != EMU_EEPROM_STATUS_OK) {
        CMD_PRINT(pCmdIO, "CERT: ERR EEPROM commit failed - nothing changed\n\r");
        return;
    }
    s_staged_from_eeprom = true;
    CMD_PRINT(pCmdIO, "CERT: saved. 'reset' to actually use this identity for new TLS "
                      "connections (existing ones are unaffected).\n\r");
}

static void cmd_cert_reset(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    if (s_state != CP_IDLE) {
        CMD_PRINT(pCmdIO, "CERT: ERR a transfer is still in progress\n\r");
        return;
    }
    cp_seed_from_compiled_defaults(&s_staged);
    if (EMU_EEPROM_BufferWrite(CERT_EE_OFFSET, (const uint8_t *)&s_staged, (uint16_t)sizeof s_staged)
            == EMU_EEPROM_STATUS_OK) {
        (void)EMU_EEPROM_PageBufferCommit();
    }
    s_staged_from_eeprom = false;
    CMD_PRINT(pCmdIO, "CERT: reset to the compiled-in default identity - 'reset' (the device "
                      "command) to actually use it for new TLS connections\n\r");
}

static const SYS_CMD_DESCRIPTOR cert_cmd_tbl[] = {
    {"cert_arm",   (SYS_CMD_FNC)cmd_cert_arm,   ": arm a cert/key transfer (cert_arm <server_cert|server_key|ca_cert> <bytes> <crc32hex>)"},
    {"cert_abort", (SYS_CMD_FNC)cmd_cert_abort, ": cancel an armed/in-progress transfer"},
    {"cert_show",  (SYS_CMD_FNC)cmd_cert_show,  ": show the active TLS identity (source, sizes, fingerprint crc)"},
    {"cert_save",  (SYS_CMD_FNC)cmd_cert_save,  ": persist the staged identity to EEPROM (needs 'reset' to take effect)"},
    {"cert_reset", (SYS_CMD_FNC)cmd_cert_reset, ": revert the staged/saved identity to the compiled-in default"},
};

// *****************************************************************************
// Section: Init / Tasks
// *****************************************************************************

void CERT_PROVISION_Initialize(void)
{
    cert_store_t fromEe;
    bool haveValid = false;

    if (EMU_EEPROM_BufferRead(CERT_EE_OFFSET, (uint8_t *)&fromEe, (uint16_t)sizeof fromEe)
            == EMU_EEPROM_STATUS_OK) {
        if ((fromEe.magic == CERT_MAGIC) && (fromEe.version == CERT_VERSION) &&
            (fromEe.crc32 == cp_store_crc(&fromEe)) &&
            (fromEe.server_cert_len <= CERT_MAX_DER) && (fromEe.server_cert_len > 0u) &&
            (fromEe.server_key_len  <= CERT_MAX_DER) && (fromEe.server_key_len  > 0u) &&
            (fromEe.ca_cert_len     <= CERT_MAX_DER) && (fromEe.ca_cert_len     > 0u)) {
            haveValid = true;
        }
    }

    if (haveValid) {
        s_staged = fromEe;
        s_staged_from_eeprom = true;
    } else {
        cp_seed_from_compiled_defaults(&s_staged);
        s_staged_from_eeprom = false;
    }
    if (!SYS_CMD_ADDGRP(cert_cmd_tbl, (int)(sizeof cert_cmd_tbl / sizeof *cert_cmd_tbl),
                        "cert", ": remote TLS identity provisioning")) {
        SYS_CONSOLE_PRINT("CERT_PROVISION: SYS_CMD_ADDGRP failed\n\r");
    }
}

void CERT_PROVISION_Tasks(void)
{
    if (s_ticks_per_ms == 0u) {
        s_ticks_per_ms = (uint64_t)SYS_TIME_FrequencyGet() / 1000ULL;
    }

    switch (s_state) {
    case CP_IDLE:
        break;

    case CP_WAIT_CONN:
        if (NET_PRES_SocketIsConnected(s_sock) && NET_PRES_SocketIsSecure(s_sock)) {
            /* Same reasoning as bootload.c's BL_WAIT_CONN: IsConnected() alone
             * is true well before the TLS+mTLS handshake finishes. The
             * WasReset() read-and-clear matters too - the stack sets that flag
             * as a side effect of accept/handshake itself (as iperf.c/
             * testserver.c do), not just on a genuine reset; skipping it here
             * made CP_RECV's very first WasReset() check see a stale true and
             * abort a perfectly good connection before reading a single byte
             * (confirmed live: cert_show afterwards showed "aborted
             * (connection lost)" despite the transfer never having a chance
             * to run). */
            (void)NET_PRES_SocketWasReset(s_sock);
            s_state    = CP_RECV;
            s_deadline = SYS_TIME_Counter64Get() + (uint64_t)CERT_STALL_TIMEOUT_MS * s_ticks_per_ms;
        } else if ((int64_t)(SYS_TIME_Counter64Get() - s_deadline) >= 0) {
            cp_abort("no client connected in time");
        }
        break;

    case CP_RECV: {
        uint16_t ready = NET_PRES_SocketReadIsReady(s_sock);
        if (NET_PRES_SocketWasReset(s_sock) || NET_PRES_SocketWasDisconnected(s_sock)) {
            cp_abort("connection lost");
            break;
        }
        if (ready > 0u) {
            uint32_t want = s_armed_size - s_recv_len;
            uint16_t got;
            if ((uint32_t)ready < want) {
                want = ready;
            }
            got = NET_PRES_SocketRead(s_sock, &s_recv_buf[s_recv_len], (uint16_t)want);
            if (got > 0u) {
                s_recv_len += got;
                s_deadline = SYS_TIME_Counter64Get() + (uint64_t)CERT_STALL_TIMEOUT_MS * s_ticks_per_ms;
            }
        } else if ((int64_t)(SYS_TIME_Counter64Get() - s_deadline) >= 0) {
            cp_abort("stalled");
            break;
        }

        if (s_recv_len >= s_armed_size) {
            uint32_t crc = cp_crc32(s_recv_buf, s_armed_size);
            if (crc != s_armed_crc) {
                cp_report("CERT: ERR crc mismatch (got %08lX, expected %08lX) - %s NOT staged\n",
                    (unsigned long)crc, (unsigned long)s_armed_crc, cp_item_name(s_armed_item));
            } else {
                switch (s_armed_item) {
                case CP_ITEM_SERVER_CERT:
                    memcpy(s_staged.server_cert_der, s_recv_buf, s_armed_size);
                    s_staged.server_cert_len = s_armed_size;
                    break;
                case CP_ITEM_SERVER_KEY:
                    memcpy(s_staged.server_key_der, s_recv_buf, s_armed_size);
                    s_staged.server_key_len = s_armed_size;
                    break;
                case CP_ITEM_CA_CERT:
                    memcpy(s_staged.ca_cert_der, s_recv_buf, s_armed_size);
                    s_staged.ca_cert_len = s_armed_size;
                    break;
                default:
                    break;
                }
                cp_report("CERT: OK %s staged (%lu bytes, crc32=%08lX) - 'cert_save' to persist\n",
                    cp_item_name(s_armed_item), (unsigned long)s_armed_size, (unsigned long)crc);
            }
            cp_close_socket();
            s_state = CP_IDLE;
            s_armed_item = CP_ITEM_NONE;
        }
        break;
    }

    default:
        break;
    }
}
