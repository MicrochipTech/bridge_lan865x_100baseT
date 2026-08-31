/*
 * env.c - persistent network config ("environment") on the Emulated EEPROM.
 *
 * The bridge ships identical firmware on every board with static network defaults
 * compiled into configuration.h. This layer keeps a versioned, CRC-protected copy
 * of the per-interface IP/mask/gateway/DNS in the Emulated EEPROM:
 *
 *   - On boot ENV_Init() reads the record; if it is missing/blank/corrupt (e.g. a
 *     freshly flashed board) it seeds the EEPROM from the compiled defaults.
 *   - env_apply() pushes the loaded config into the TCP/IP stack at runtime
 *     (TCPIP_STACK_NetAddressSet/...), so we never touch the MCC-generated
 *     TCPIP_HOSTS_CONFIGURATION.
 *   - CLI: showenv / setenv <key> <ip> / saveenv / readenv / resetenv.
 *
 * Defaults live in code (not a pre-baked EEPROM image): the emulated-EEPROM library
 * owns the on-flash format and formats a blank region on first init, so seeding from
 * the configuration.h values is the robust approach. "Change the build default" =
 * change configuration.h. No C++.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>

#include "definitions.h"
#include "configuration.h"                                   /* TCPIP_NETWORK_DEFAULT_* */
#include "config/default/library/tcpip/tcpip.h"              /* IPV4_ADDR, TCPIP_STACK_*, TCPIP_Helper_* */
#include "config/default/system/console/sys_console.h"
#include "system/command/sys_command.h"
#include "config/default/library/emulated_eeprom/emulated_eeprom.h"
#include "lan865x_diag.h"                                    /* LAN865X_DIAG_ApplyPlca() */
#include "port_mirror.h"                                     /* MIRROR_IsEnabled() for showenv */
#include "env.h"
#include "cmd_print.h"                                        /* CMD_PRINT/CMD_MSG - reply to pCmdIO, not always the serial console */

/* The magic doubles as the ENVIRONMENT ID: it says which firmware variant wrote this
 * record, not just "this is an env record". It has to, because variants disagree about
 * the layout while agreeing on everything else - t1s_ptp_bridge also stores at offset 0,
 * also calls it version 4, and puts ptp_auto where this one puts mirror. Only the size
 * (68 vs 72 bytes) and therefore the crc32 tell them apart today, which is luck rather
 * than design: two variants of equal size would silently misread each other's settings.
 *
 * New variants pick their own four characters and never reuse one. */
#define ENV_MAGIC    0x54494252u   /* 'TIBR' - tcpip_iperf_lan865x bridge, this firmware (distinct from
                                     * the t1s_100baset_bridge variant's 'EBRG' per the convention above:
                                     * different project, different board population, must not silently
                                     * accept that variant's stored record). */
#define ENV_MAGIC_LEGACY 0x4C414E45u /* 'LANE' - written before ids were per-variant.
                                      * Still accepted on read: a record that also matches
                                      * this layout's version AND crc can only have come
                                      * from this variant. Rewritten with the new id on the
                                      * next saveenv, so it disappears by itself. */
#define ENV_VERSION  5u            /* v2 PLCA, v3 the MACs, v4 the mirror flag, v5 the sniffer flag */
#define ENV_VERSION_V3 3u          /* migrated in place by env_migrate_v3() - see there why */
#define ENV_VERSION_V4 4u          /* migrated in place by env_migrate_v4() - see there why */
#define ENV_VARIANT  "tcpip_iperf_lan865x_bridge"   /* printed by showenv next to the id */
#define ENV_IF_CNT   2             /* [0] = eth0 (LAN865x/T1S), [1] = eth1 (GMAC/100BASE-T) */
#define ENV_EE_OFFSET 0u           /* byte offset of the record in the emulated EEPROM */

/* SAME54 128-bit device serial number, word 0 (least-significant word). The lowest
 * 3 bytes seed the eth0 MAC so every board is unique with one firmware image. */
#define SAME54_SERIAL_WORD0  (*(volatile uint32_t *)0x008061FCu)
static const uint8_t ENV_OUI[3] = { 0x00u, 0x04u, 0x25u };   /* Microchip OUI (matches the config default) */

/* Fixed-size record. Layout is 4-byte aligned throughout (the 2x6-byte mac block is
 * 12 bytes) so there is no padding and the crc32 is stable. ip/mask/gw/dns are
 * IPV4_ADDR.Val (network byte order); mac[i] are 6-byte MACs; plca_* are the eth0
 * PLCA node id/count. crc32 covers all bytes before it. */
typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t ip[ENV_IF_CNT];
    uint32_t mask[ENV_IF_CNT];
    uint32_t gw[ENV_IF_CNT];
    uint32_t dns[ENV_IF_CNT];
    uint8_t  mac[ENV_IF_CNT][6];   /* eth0 = OUI+serial, eth1 = eth0 with low byte +1 */
    uint32_t plca_id;              /* eth0 PLCA node id    (0 = coordinator)         */
    uint32_t plca_cnt;             /* eth0 PLCA node count (PLCA_CTRL1 NODE_CNT)      */
    uint32_t mirror;               /* 1 = enable the eth0->eth1 port mirror at boot   */
    uint32_t sniffer;              /* 1 = suppress the eth0 PHY transmitter at boot   */
    uint32_t crc32;
} env_t;

/* The v3 record: byte-for-byte the layout above without `mirror`/`sniffer`. Kept so a
 * board that was configured before v4 can be migrated instead of re-seeded - see
 * env_migrate_v3(). Do not "tidy" this away: dropping it turns a firmware update
 * into a silent factory reset. */
typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t ip[ENV_IF_CNT];
    uint32_t mask[ENV_IF_CNT];
    uint32_t gw[ENV_IF_CNT];
    uint32_t dns[ENV_IF_CNT];
    uint8_t  mac[ENV_IF_CNT][6];
    uint32_t plca_id;
    uint32_t plca_cnt;
    uint32_t crc32;
} env_v3_t;

/* The v4 record: byte-for-byte the layout above with `mirror` but without `sniffer`.
 * Kept so a board that was configured before v5 can be migrated instead of re-seeded -
 * see env_migrate_v4(). Do not "tidy" this away, same reasoning as env_v3_t. */
typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t ip[ENV_IF_CNT];
    uint32_t mask[ENV_IF_CNT];
    uint32_t gw[ENV_IF_CNT];
    uint32_t dns[ENV_IF_CNT];
    uint8_t  mac[ENV_IF_CNT][6];
    uint32_t plca_id;
    uint32_t plca_cnt;
    uint32_t mirror;
    uint32_t crc32;
} env_v4_t;

/* The three layouts must be padding-free, and each must be exactly the previous one
 * plus one word - that is what makes the migrations a field-by-field copy and keeps
 * the crc32 stable. Checked at COMPILE time (negative array size fails the build)
 * rather than trusted, because padding would corrupt the stored record silently: the
 * crc would still be self-consistent, so nothing would ever report an error. */
typedef char env_layout_assert[
    (sizeof(env_v3_t) == 64u && sizeof(env_v4_t) == 68u && sizeof(env_t) == 72u &&
     offsetof(env_v3_t, crc32) == 60u && offsetof(env_v4_t, crc32) == 64u &&
     offsetof(env_t, crc32) == 68u &&
     offsetof(env_v3_t, plca_id) == offsetof(env_v4_t, plca_id) &&
     offsetof(env_v4_t, plca_id) == offsetof(env_t, plca_id)) ? 1 : -1];

/* Derive the per-board default MACs from the SAME54 serial: eth0 = OUI + serial[2..0],
 * eth1 = eth0 with the lowest byte +1. */
static void env_derive_mac(uint8_t m0[6], uint8_t m1[6])
{
    uint32_t s = SAME54_SERIAL_WORD0;
    m0[0] = ENV_OUI[0]; m0[1] = ENV_OUI[1]; m0[2] = ENV_OUI[2];
    m0[3] = (uint8_t)(s >> 16); m0[4] = (uint8_t)(s >> 8); m0[5] = (uint8_t)s;
    memcpy(m1, m0, 6);
    m1[5] = (uint8_t)(m0[5] + 1u);
}

static env_t s_env;

/* The bridge's interfaces are reachable under these names (matches app.c). */
static const char *const ENV_IF[ENV_IF_CNT] = { "eth0", "eth1" };

/* --- CRC32 (IEEE 802.3, poly 0xEDB88320) over len bytes ------------------------- */
static uint32_t env_crc32(const void *p, size_t len)
{
    const uint8_t *b = (const uint8_t *)p;
    uint32_t crc = 0xFFFFFFFFu;
    size_t i; int k;
    for (i = 0; i < len; i++) {
        crc ^= b[i];
        for (k = 0; k < 8; k++)
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1u)));
    }
    return ~crc;
}
static uint32_t env_calc_crc(const env_t *e)
{
    return env_crc32(e, offsetof(env_t, crc32));
}

/* --- defaults straight from configuration.h ------------------------------------- */
static void env_load_defaults(env_t *e)
{
    static const char *const dip[ENV_IF_CNT]   = { TCPIP_NETWORK_DEFAULT_IP_ADDRESS_IDX0, TCPIP_NETWORK_DEFAULT_IP_ADDRESS_IDX1 };
    static const char *const dmask[ENV_IF_CNT] = { TCPIP_NETWORK_DEFAULT_IP_MASK_IDX0,    TCPIP_NETWORK_DEFAULT_IP_MASK_IDX1 };
    static const char *const dgw[ENV_IF_CNT]   = { TCPIP_NETWORK_DEFAULT_GATEWAY_IDX0,    TCPIP_NETWORK_DEFAULT_GATEWAY_IDX1 };
    static const char *const ddns[ENV_IF_CNT]  = { TCPIP_NETWORK_DEFAULT_DNS_IDX0,        TCPIP_NETWORK_DEFAULT_DNS_IDX1 };
    int i; IPV4_ADDR a;
    memset(e, 0, sizeof *e);
    e->magic = ENV_MAGIC;
    e->version = ENV_VERSION;
    for (i = 0; i < ENV_IF_CNT; i++) {
        a.Val = 0; (void)TCPIP_Helper_StringToIPAddress(dip[i],   &a); e->ip[i]   = a.Val;
        a.Val = 0; (void)TCPIP_Helper_StringToIPAddress(dmask[i], &a); e->mask[i] = a.Val;
        a.Val = 0; (void)TCPIP_Helper_StringToIPAddress(dgw[i],   &a); e->gw[i]   = a.Val;
        a.Val = 0; (void)TCPIP_Helper_StringToIPAddress(ddns[i],  &a); e->dns[i]  = a.Val;
    }
    env_derive_mac(e->mac[0], e->mac[1]);
    e->plca_id  = (uint32_t)DRV_LAN865X_PLCA_NODE_ID_IDX0;
    e->plca_cnt = (uint32_t)DRV_LAN865X_PLCA_NODE_COUNT_IDX0;
    e->mirror   = 0u;              /* off by default: mirroring costs packet-pool entries */
    e->sniffer  = 0u;              /* off by default: a sniffer never talks on the bus */
    e->crc32 = env_calc_crc(e);
}

/* Format env MAC for interface (0/1) as "XX:XX:XX:XX:XX:XX" into buf (>=18 bytes). */
void env_mac_str(int iface, char *buf)
{
    const uint8_t *m;
    if (iface < 0 || iface >= ENV_IF_CNT) { buf[0] = '\0'; return; }
    m = s_env.mac[iface];
    (void)snprintf(buf, 18, "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
}

/* Parse "XX:XX:XX:XX:XX:XX" into out[6]; true on success. */
static bool env_parse_mac(const char *s, uint8_t out[6])
{
    unsigned v[6]; int i, n;
    n = sscanf(s, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]);
    if (n != 6) return false;
    for (i = 0; i < 6; i++) { if (v[i] > 0xFFu) return false; out[i] = (uint8_t)v[i]; }
    return true;
}

/* --- EEPROM read/write ---------------------------------------------------------- */
static bool env_save(void)
{
    s_env.magic = ENV_MAGIC;
    s_env.version = ENV_VERSION;
    s_env.crc32 = env_calc_crc(&s_env);
    if (EMU_EEPROM_BufferWrite(ENV_EE_OFFSET, (const uint8_t *)&s_env, (uint16_t)sizeof s_env) != EMU_EEPROM_STATUS_OK)
        return false;
    return EMU_EEPROM_PageBufferCommit() == EMU_EEPROM_STATUS_OK;
}

/* Read a record from the EEPROM into *out; true only if magic+version+crc check out. */
/* What was actually found in the EEPROM at boot, before anything was accepted or
 * discarded. Kept separately because s_env cannot answer the question: env_read_valid()
 * demands an exact version match, so after a mismatch s_env holds the compiled defaults
 * and its .version is ALWAYS the firmware's own - a showenv reading it could never
 * reveal a mismatch, which is the one thing worth reporting. */
static uint32_t s_ee_id        = 0u;      /* magic/environment id found in the EEPROM, 0 = none */
static uint32_t s_ee_version   = 0u;      /* version field of that record                       */
static bool     s_ee_crc_ok    = false;   /* ... and whether it was intact                      */

static bool env_read_valid(env_t *out)
{
    if (EMU_EEPROM_BufferRead(ENV_EE_OFFSET, (uint8_t *)out, (uint16_t)sizeof *out) != EMU_EEPROM_STATUS_OK)
        return false;
    /* Record what is there regardless of whether it is usable. magic and version sit at
     * the same offsets in every layout written so far, so they can be read even when the
     * rest of the record belongs to a variant or version we cannot interpret. */
    s_ee_id      = out->magic;
    s_ee_version = out->version;
    s_ee_crc_ok  = (out->crc32 == env_calc_crc(out));

    return (out->magic == ENV_MAGIC || out->magic == ENV_MAGIC_LEGACY)
           && out->version == ENV_VERSION && s_ee_crc_ok;
}

/* Four characters of an environment id, for printing. Non-printable bytes become '?'
 * so a garbage record cannot scramble the console output. */
static void env_id_str(uint32_t id, char out[5])
{
    int i;
    for (i = 0; i < 4; i++) {
        uint8_t c = (uint8_t)(id >> (24 - 8 * i));
        out[i] = (c >= 0x20u && c < 0x7Fu) ? (char)c : '?';
    }
    out[4] = '\0';
}

/* Migrate a v3 record in place: carry every field over, default the new one, store
 * as v4. True if a valid v3 record was found and converted.
 *
 * WHY THIS IS NOT OPTIONAL. env_read_valid() demands an exact version match, so
 * bumping the version alone makes every stored record invalid and ENV_Init falls
 * back to the compiled defaults. Those are not harmless here: the default PLCA node
 * id is DRV_LAN865X_PLCA_NODE_ID_IDX0 = 7, while a bridge acting as coordinator runs
 * id 0 - a value that can only come from the EEPROM. Without this function a
 * firmware update would silently stop the bridge being the coordinator: no beacons,
 * a bus without a coordinator, and nothing in the log pointing at the EEPROM. One
 * would go looking in the PLCA configuration. */
static bool env_migrate_v3(env_t *out)
{
    env_v3_t old;
    int i;

    if (EMU_EEPROM_BufferRead(ENV_EE_OFFSET, (uint8_t *)&old, (uint16_t)sizeof old) != EMU_EEPROM_STATUS_OK)
        return false;
    if (old.magic != ENV_MAGIC || old.version != ENV_VERSION_V3)
        return false;
    if (old.crc32 != env_crc32(&old, offsetof(env_v3_t, crc32)))
        return false;

    memset(out, 0, sizeof *out);
    out->magic   = ENV_MAGIC;
    out->version = ENV_VERSION;
    for (i = 0; i < ENV_IF_CNT; i++) {
        out->ip[i]   = old.ip[i];
        out->mask[i] = old.mask[i];
        out->gw[i]   = old.gw[i];
        out->dns[i]  = old.dns[i];
        memcpy(out->mac[i], old.mac[i], 6);
    }
    out->plca_id  = old.plca_id;
    out->plca_cnt = old.plca_cnt;
    out->mirror   = 0u;                /* the flag did not exist before: start off */
    out->sniffer  = 0u;                /* neither did this one */
    out->crc32    = env_calc_crc(out);
    return true;
}

/* Migrate a v4 record in place: carry every field (including `mirror`) over, default
 * the new `sniffer` field, store as v5. True if a valid v4 record was found and
 * converted. Same reasoning as env_migrate_v3() - without this, a firmware update on a
 * v4 board would silently discard the whole record (not just fail to add `sniffer`),
 * including the PLCA coordinator id. */
static bool env_migrate_v4(env_t *out)
{
    env_v4_t old;
    int i;

    if (EMU_EEPROM_BufferRead(ENV_EE_OFFSET, (uint8_t *)&old, (uint16_t)sizeof old) != EMU_EEPROM_STATUS_OK)
        return false;
    if (old.magic != ENV_MAGIC || old.version != ENV_VERSION_V4)
        return false;
    if (old.crc32 != env_crc32(&old, offsetof(env_v4_t, crc32)))
        return false;

    memset(out, 0, sizeof *out);
    out->magic   = ENV_MAGIC;
    out->version = ENV_VERSION;
    for (i = 0; i < ENV_IF_CNT; i++) {
        out->ip[i]   = old.ip[i];
        out->mask[i] = old.mask[i];
        out->gw[i]   = old.gw[i];
        out->dns[i]  = old.dns[i];
        memcpy(out->mac[i], old.mac[i], 6);
    }
    out->plca_id  = old.plca_id;
    out->plca_cnt = old.plca_cnt;
    out->mirror   = old.mirror;
    out->sniffer  = 0u;                 /* the flag did not exist before: start off */
    out->crc32    = env_calc_crc(out);
    return true;
}

/* --- apply to the running stack ------------------------------------------------- */
void env_apply(void)
{
    int i;
    for (i = 0; i < ENV_IF_CNT; i++) {
        TCPIP_NET_HANDLE nh = TCPIP_STACK_NetHandleGet(ENV_IF[i]);
        IPV4_ADDR ip, mask, gw, dns;
        if (nh == NULL)
            continue;
        ip.Val = s_env.ip[i]; mask.Val = s_env.mask[i]; gw.Val = s_env.gw[i]; dns.Val = s_env.dns[i];
        (void)TCPIP_STACK_NetAddressSet(nh, &ip, &mask, true);
        (void)TCPIP_STACK_NetAddressGatewaySet(nh, &gw);
        if (dns.Val != 0u)
            (void)TCPIP_STACK_NetDnsPrimarySet(nh, &dns);
    }
    /* PLCA on eth0 (LAN865x) - queued via the diagnostics module's register machine.
     * This call is also what keeps that module's node-count shadow current, which is
     * how 'plca_node <id>' knows the right count without depending on env. */
    LAN865X_DIAG_ApplyPlca((uint8_t)s_env.plca_id, (uint8_t)s_env.plca_cnt);
}

uint8_t env_plca_id(void)  { return (uint8_t)s_env.plca_id;  }
uint8_t env_plca_cnt(void) { return (uint8_t)s_env.plca_cnt; }
bool    env_mirror(void)   { return s_env.mirror != 0u;      }
bool    env_sniffer(void)  { return s_env.sniffer != 0u;     }

/* --- CLI ------------------------------------------------------------------------ */
static void pr_addr(SYS_CMD_DEVICE_NODE *pCmdIO, const char *label, uint32_t val)
{
    char b[20]; IPV4_ADDR a; a.Val = val;
    (void)TCPIP_Helper_IPAddressToString(&a, b, sizeof b);
    CMD_PRINT(pCmdIO, "%s%s", label, b);
}

static void cmd_showenv(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    int i; (void)argc; (void)argv;
    CMD_PRINT(pCmdIO, "env (RAM shadow):\r\n");
    /* Identity first, and always the same shape, because a tool reads this line to decide
     * whether it may interpret the rest at all. "eeprom" is what was found at boot, not
     * what is in RAM now - after a rejected record those two differ, and that difference
     * is the whole point. */
    {
        char found[5], mine[5];
        env_id_str(s_ee_id, found);
        env_id_str((uint32_t)ENV_MAGIC, mine);
        CMD_PRINT(pCmdIO, "  env   id %s  version %lu  crc %s  |  firmware id %s  version %lu  %s\r\n",
                          (s_ee_id != 0u) ? found : "none",
                          (unsigned long)s_ee_version,
                          s_ee_crc_ok ? "ok" : "BAD",
                          mine, (unsigned long)ENV_VERSION, ENV_VARIANT);
    }
    for (i = 0; i < ENV_IF_CNT; i++) {
        CMD_PRINT(pCmdIO, "  eth%d  ", i);
        pr_addr(pCmdIO, "ip ",     s_env.ip[i]);
        pr_addr(pCmdIO, "  mask ", s_env.mask[i]);
        pr_addr(pCmdIO, "  gw ",   s_env.gw[i]);
        pr_addr(pCmdIO, "  dns ",  s_env.dns[i]);
        CMD_PRINT(pCmdIO, "\r\n");
    }
    {
        char mb[18];
        env_mac_str(0, mb); CMD_PRINT(pCmdIO, "  eth0  mac %s\r\n", mb);
        env_mac_str(1, mb); CMD_PRINT(pCmdIO, "  eth1  mac %s  (applied at boot)\r\n", mb);
    }
    CMD_PRINT(pCmdIO, "  plca  id %lu  count %lu  (eth0/T1S)\r\n",
                      (unsigned long)s_env.plca_id, (unsigned long)s_env.plca_cnt);
    /* Two states, because they can differ: what boots, and what is running now
     * ('mirror 1' is deliberately volatile). Showing only one would mislead. */
    CMD_PRINT(pCmdIO, "  mirror %s at boot  (now: %s)\r\n",
                      (s_env.mirror != 0u) ? "ON " : "OFF",
                      MIRROR_IsEnabled() ? "ON" : "OFF");
    CMD_PRINT(pCmdIO, "  sniffer %s at boot  (now: %s)\r\n",
                      (s_env.sniffer != 0u) ? "ON " : "OFF",
                      SNIFFER_IsEnabled() ? "ON" : "OFF");
    CMD_PRINT(pCmdIO, "  (saveenv = persist+apply, readenv = reload, resetenv = defaults)\r\n");
}

static uint32_t *env_field(const char *key)
{
    if (!strcmp(key, "ip0"))   return &s_env.ip[0];
    if (!strcmp(key, "mask0")) return &s_env.mask[0];
    if (!strcmp(key, "gw0"))   return &s_env.gw[0];
    if (!strcmp(key, "dns0"))  return &s_env.dns[0];
    if (!strcmp(key, "ip1"))   return &s_env.ip[1];
    if (!strcmp(key, "mask1")) return &s_env.mask[1];
    if (!strcmp(key, "gw1"))   return &s_env.gw[1];
    if (!strcmp(key, "dns1"))  return &s_env.dns[1];
    return NULL;
}

static void cmd_setenv(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    uint32_t *fld; IPV4_ADDR a;
    if (argc < 3) {
        CMD_PRINT(pCmdIO, "usage: setenv <key> <val>\r\n"
                          "  IP keys:   ip0/mask0/gw0/dns0, ip1/mask1/gw1/dns1  (dotted-quad)\r\n"
                          "  MAC keys:  mac0, mac1  (XX:XX:XX:XX:XX:XX; applies after reset)\r\n"
                          "  PLCA keys: plca_id (0..254), plca_cnt (1..255)\r\n"
                          "  mirror:    0|1  (eth0->eth1 port mirror at boot)\r\n"
                          "  sniffer:   0|1  (suppress the eth0 PHY transmitter at boot)\r\n");
        return;
    }
    /* mirror: stored here, applied at the next boot by MIRROR_Initialize(). The live
     * state stays with the 'mirror' command - same split as plca_node vs plca_id. */
    if (!strcmp(argv[1], "mirror")) {
        unsigned long v = strtoul(argv[2], NULL, 0);
        if (v > 1u) { CMD_PRINT(pCmdIO, "setenv: mirror must be 0 or 1\r\n"); return; }
        s_env.mirror = (uint32_t)v;
        CMD_PRINT(pCmdIO, "setenv: mirror = %lu (RAM only; 'saveenv' to persist; "
                          "takes effect at the next boot - use 'mirror %lu' to switch it now)\r\n",
                          v, v);
        return;
    }
    /* sniffer: stored here, applied INSIDE the LAN865x driver's own init sequence
     * (before NETWORK_CONTROL/TXEN is ever written - see initialization.c and
     * drv_lan865x_api.c), not just at the next boot's app-level init. The live state
     * stays with the 'sniffer' command - same split as mirror/plca_id above. */
    if (!strcmp(argv[1], "sniffer")) {
        unsigned long v = strtoul(argv[2], NULL, 0);
        if (v > 1u) { CMD_PRINT(pCmdIO, "setenv: sniffer must be 0 or 1\r\n"); return; }
        s_env.sniffer = (uint32_t)v;
        CMD_PRINT(pCmdIO, "setenv: sniffer = %lu (RAM only; 'saveenv' to persist; "
                          "takes effect at the next boot - use 'sniffer %lu' to switch it now)\r\n",
                          v, v);
        return;
    }
    /* MAC keys (applied on next reset - the stack binds the MAC at init) */
    if (!strcmp(argv[1], "mac0") || !strcmp(argv[1], "mac1")) {
        int idx = (argv[1][3] == '1') ? 1 : 0;
        uint8_t m[6];
        if (!env_parse_mac(argv[2], m)) {
            CMD_PRINT(pCmdIO, "setenv: bad MAC '%s' (use XX:XX:XX:XX:XX:XX)\r\n", argv[2]);
            return;
        }
        memcpy(s_env.mac[idx], m, 6);
        CMD_PRINT(pCmdIO, "setenv: %s = %s (RAM only; 'saveenv' to persist; MAC applies after reset)\r\n",
                          argv[1], argv[2]);
        return;
    }
    /* numeric PLCA keys */
    if (!strcmp(argv[1], "plca_id") || !strcmp(argv[1], "plca_cnt")) {
        unsigned long v = strtoul(argv[2], NULL, 0);
        if (!strcmp(argv[1], "plca_id")) {
            if (v > 254u) { CMD_PRINT(pCmdIO, "setenv: plca_id range 0..254\r\n"); return; }
            s_env.plca_id = (uint32_t)v;
        } else {
            if (v < 1u || v > 255u) { CMD_PRINT(pCmdIO, "setenv: plca_cnt range 1..255\r\n"); return; }
            s_env.plca_cnt = (uint32_t)v;
        }
        CMD_PRINT(pCmdIO, "setenv: %s = %lu (RAM only; 'saveenv' to persist)\r\n", argv[1], v);
        return;
    }
    fld = env_field(argv[1]);
    if (fld == NULL) { CMD_PRINT(pCmdIO, "setenv: unknown key '%s'\r\n", argv[1]); return; }
    a.Val = 0;
    if (!TCPIP_Helper_StringToIPAddress(argv[2], &a)) { CMD_PRINT(pCmdIO, "setenv: bad IP '%s'\r\n", argv[2]); return; }
    *fld = a.Val;
    CMD_PRINT(pCmdIO, "setenv: %s = %s (RAM only; 'saveenv' to persist)\r\n", argv[1], argv[2]);
}

static void cmd_saveenv(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    if (env_save()) {
        env_apply();
        CMD_PRINT(pCmdIO, "saveenv: persisted to EEPROM and applied "
                          "(an IP change drops the current connection).\r\n");
    } else {
        CMD_PRINT(pCmdIO, "saveenv: EEPROM write FAILED.\r\n");
    }
}

static void cmd_readenv(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    env_t tmp; (void)argc; (void)argv;
    if (env_read_valid(&tmp)) {
        s_env = tmp;
        env_apply();
        CMD_PRINT(pCmdIO, "readenv: reloaded from EEPROM and applied.\r\n");
    } else {
        CMD_PRINT(pCmdIO, "readenv: no valid config in EEPROM.\r\n");
    }
}

static void cmd_resetenv(SYS_CMD_DEVICE_NODE *pCmdIO, int argc, char **argv)
{
    (void)argc; (void)argv;
    env_load_defaults(&s_env);
    if (env_save()) {
        env_apply();
        CMD_PRINT(pCmdIO, "resetenv: restored compiled defaults, persisted and applied.\r\n");
    } else {
        CMD_PRINT(pCmdIO, "resetenv: defaults applied but EEPROM write FAILED.\r\n");
    }
}

static const SYS_CMD_DESCRIPTOR env_cmd_tbl[] = {
    {"showenv",  (SYS_CMD_FNC)cmd_showenv,  ": show the current network config (RAM shadow)"},
    {"setenv",   (SYS_CMD_FNC)cmd_setenv,   ": setenv <key> <val>  (ip0../dns1, mac0/mac1, plca_id, plca_cnt, mirror, sniffer)"},
    {"saveenv",  (SYS_CMD_FNC)cmd_saveenv,  ": persist config to EEPROM and apply it live"},
    {"readenv",  (SYS_CMD_FNC)cmd_readenv,  ": reload config from EEPROM and apply (discards unsaved edits)"},
    {"resetenv", (SYS_CMD_FNC)cmd_resetenv, ": reset to compiled defaults, persist and apply"},
};

void ENV_Init(void)
{
    env_t tmp;
    /* A freshly flashed board has a blank EEPROM region -> BAD_FORMAT; format it once. */
    if (EMU_EEPROM_StatusGet() == EMU_EEPROM_STATUS_ERR_BAD_FORMAT)
        (void)EMU_EEPROM_FormatMemory();

    if (env_read_valid(&tmp)) {
        s_env = tmp;                       /* valid persisted config */
    } else if (env_migrate_v4(&tmp)) {
        s_env = tmp;                       /* pre-v5 board: keep its settings, add the new field */
        (void)env_save();
        SYS_CONSOLE_PRINT("env: migrated v%u record to v%u (settings kept, sniffer=0)\r\n",
                          (unsigned)ENV_VERSION_V4, (unsigned)ENV_VERSION);
    } else if (env_migrate_v3(&tmp)) {
        s_env = tmp;                       /* pre-v4 board: keep its settings, add the new fields */
        (void)env_save();
        SYS_CONSOLE_PRINT("env: migrated v%u record to v%u (settings kept, mirror=0, sniffer=0)\r\n",
                          (unsigned)ENV_VERSION_V3, (unsigned)ENV_VERSION);
    } else {
        /* Say why. Falling back to the compiled defaults is not harmless here: the default
         * PLCA node id is 7, while this bridge has to run as coordinator (id 0). Without
         * this line the board simply stops sending beacons and nothing points at the
         * EEPROM - see CLAUDE.md section 6. */
        if (s_ee_id != 0u) {
            char found[5], mine[5];
            env_id_str(s_ee_id, found);
            env_id_str((uint32_t)ENV_MAGIC, mine);
            SYS_CONSOLE_PRINT("env: DISCARDED the stored record (id %s version %lu crc %s) - "
                              "this firmware writes id %s version %lu (%s).\r\n"
                              "env: compiled defaults are in use, PLCA node id is now %u. "
                              "Check 'showenv' before relying on the link.\r\n",
                              found, (unsigned long)s_ee_version, s_ee_crc_ok ? "ok" : "BAD",
                              mine, (unsigned long)ENV_VERSION, ENV_VARIANT,
                              (unsigned)DRV_LAN865X_PLCA_NODE_ID_IDX0);
        }
        env_load_defaults(&s_env);         /* first boot / blank / corrupt / foreign variant */
        (void)env_save();                  /* seed the EEPROM from the compiled defaults */
    }
    SYS_CMD_ADDGRP(env_cmd_tbl, (int)(sizeof env_cmd_tbl / sizeof *env_cmd_tbl),
                   "env", ": persistent network config (saveenv/readenv/showenv)");
}
