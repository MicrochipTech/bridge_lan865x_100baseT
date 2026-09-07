/*******************************************************************************
 wolfSSL glue functions to work with Harmony's NET_PRES encryption provider API
 - TLS CLIENT role (MQTT-over-TLS work, see docs/mqtt-tls-agent-prompt.md)

 Description:
    net_pres_enc_glue.c (this project's original TLS work, branch
    t1s-t1s-bridge-lan8670) only ever implements a TLS *server*: Telnet,
    bootload and cert_provision all accept inbound connections. The MQTT
    client (app_mqtt.c) needs the opposite role - the board dials OUT to an
    external broker - which NET_PRES models as a completely separate socket
    family (NET_PRES_SKT_ENCRYPTED_STREAM_CLIENT, its own provider slot
    `encProvObjectSC[]`/`pProvObject_sc` in net_pres.c/net_pres.h, entirely
    unrelated to the stream-SERVER slot net_pres_enc_glue.c registers into).

    Deliberately a separate translation unit rather than adding a role
    parameter to net_pres_enc_glue.c: the two providers need their own
    WOLFSSL_CTX (server vs client method, different verify semantics), and
    keeping the already-proven server file untouched avoids any risk of
    regressing Telnet/bootload/cert_provision while adding this. The
    per-connection plumbing (I/O callbacks, Open/Close/Read/Write/Peek/
    ReadReady/WriteReady/OutputSize/MaxOutputSize) is identical in shape to
    net_pres_enc_glue.c's - intentionally duplicated, not shared, for the
    same "don't touch the working file" reason; both are short enough that
    the duplication is cheaper to maintain than a shared abstraction would
    be to get right twice.

    Identity: per the mTLS role-separation question that came up while
    scoping this (docs/mqtt-tls-agent-prompt.md), this does NOT mint a new,
    separate MQTT-client certificate. cert_provision.c's emulated-EEPROM
    certificate store is already at 3864 of 4064 available bytes with its
    three existing slots (server_cert/server_key/ca_cert, each up to
    CERT_MAX_DER=1280B - see cert_provision.c's own budget comment) - there
    is no room left for a fourth RSA-2048 DER blob without either shrinking
    every existing slot (breaking already-provisioned boards) or a bigger
    EEPROM-layout change, out of scope here. Instead, EncGlue_Client_Init()
    reuses whatever cert_provision.c already manages on this board
    (CERT_PROVISION_ActiveServerIdentity()) as this board's OWN client
    certificate when dialing the broker: the exact same cert/key this board
    already presents as a TLS *server* for Telnet/bootload. Nothing new to
    provision, arm, save or lose track of - 'cert_show'/'cert_arm' already
    cover it. The broker's own certificate is verified with the same
    project CA as everywhere else (CERT_PROVISION_ActiveCa()).
*******************************************************************************/

#include "net_pres_enc_glue_client.h"
#include "net_pres_transportapi.h"   /* full S_NET_PRES_TransportObject definition (fpRead/fpWrite/...) */

#include <stdlib.h>
#include <string.h>

#include "wolfssl/ssl.h"
#include "cert_provision.h"
#include "config/default/system/time/sys_time.h"   /* SYS_TIME_Counter64Get/FrequencyGet - timeouts below */

/* Same reasoning as net_pres_enc_glue.c's ENC_CONNECT_TIMEOUT_MS: bound how
   long a single handshake attempt is pumped before this gives up, so a
   broker that never responds (network issue, wrong port, TLS rejection)
   cannot wedge this socket forever. A real handshake was measured at ~1.2s
   server-side (docs/tls-poc-report.md sec.10); the client side does
   comparable RSA work, so 15s gives real margin while still bounding a
   stalled attempt. app_mqtt.c's own state machine is what actually retries
   the connection after a FAILED status - this timeout only bounds one
   attempt, it does not implement backoff. */
#define ENC_CLIENT_CONNECT_TIMEOUT_MS  15000u
#define ENC_CLIENT_CLOSE_TIMEOUT_MS     5000u

typedef struct {
    WOLFSSL   *ssl;
    uintptr_t  transHandle;
    uint64_t   closeDeadline;
    uint64_t   connectDeadline;
} EncGlueClientConn;

static WOLFSSL_CTX                            *s_ctx         = NULL;
static const struct S_NET_PRES_TransportObject *s_transObject = NULL;
static uint64_t                                s_ticksPerMs  = 0u;

static EncGlueClientConn *EncGlueClient_ConnFromProviderData(void *providerData)
{
    EncGlueClientConn *conn;
    memcpy(&conn, providerData, sizeof(conn));
    return conn;
}

static uint64_t EncGlueClient_TicksPerMs(void)
{
    if (s_ticksPerMs == 0u) {
        s_ticksPerMs = (uint64_t)SYS_TIME_FrequencyGet() / 1000ULL;
    }
    return s_ticksPerMs;
}

static int EncGlueClient_IoRecv(WOLFSSL *ssl, char *buf, int sz, void *ctx)
{
    EncGlueClientConn *conn = (EncGlueClientConn *)ctx;
    uint16_t n = s_transObject->fpRead(conn->transHandle, (uint8_t *)buf, (uint16_t)sz);
    (void)ssl;
    if (n == 0U) {
        return WOLFSSL_CBIO_ERR_WANT_READ;
    }
    return (int)n;
}

static int EncGlueClient_IoSend(WOLFSSL *ssl, char *buf, int sz, void *ctx)
{
    EncGlueClientConn *conn = (EncGlueClientConn *)ctx;
    uint16_t n = s_transObject->fpWrite(conn->transHandle, (const uint8_t *)buf, (uint16_t)sz);
    (void)ssl;
    if (n == 0U) {
        return WOLFSSL_CBIO_ERR_WANT_WRITE;
    }
    return (int)n;
}

static bool EncGlueClient_Init(struct S_NET_PRES_TransportObject *transObject)
{
    s_transObject = transObject;

    if (s_ctx != NULL) {
        return true;   /* already initialized - fpInit can be called more than once */
    }

    (void) wolfSSL_Init();

    s_ctx = wolfSSL_CTX_new(wolfTLSv1_2_client_method());
    if (s_ctx == NULL) {
        return false;
    }

    wolfSSL_CTX_SetIORecv(s_ctx, EncGlueClient_IoRecv);
    wolfSSL_CTX_SetIOSend(s_ctx, EncGlueClient_IoSend);

    /* This board's own identity for the mTLS handshake with the broker -
       deliberately the SAME cert/key cert_provision.c already manages for
       this board's TLS *server* role (Telnet/bootload), not a separate
       MQTT-specific identity - see this file's header comment for why. */
    {
        const uint8_t *certDer, *keyDer;
        uint32_t certLen, keyLen;
        bool fromEeprom;
        CERT_PROVISION_ActiveServerIdentity(&certDer, &certLen, &keyDer, &keyLen, &fromEeprom);
        if (wolfSSL_CTX_use_certificate_buffer(s_ctx, certDer, (long)certLen,
                WOLFSSL_FILETYPE_ASN1) != WOLFSSL_SUCCESS) {
            return false;
        }
        if (wolfSSL_CTX_use_PrivateKey_buffer(s_ctx, keyDer, (long)keyLen,
                WOLFSSL_FILETYPE_ASN1) != WOLFSSL_SUCCESS) {
            return false;
        }
    }

    /* Verify the broker's certificate chains to this project's own CA -
       same trust anchor as everywhere else (CERT_PROVISION_ActiveCa()), so
       whatever identity scripts/pki.py issued the broker
       (issue_mqtt_broker_identity()) is automatically trusted with no
       separate firmware-side CA change. No hostname/CN check (matches this
       project's existing client-side precedent in scripts/bridge_gui_telnet.py
       - the broker's CN is a fixed name from cert-issue time, not tied to
       whatever IP it happens to run on); the chain-to-CA check alone is
       still a real identity check, just not a hostname-bound one. */
    wolfSSL_CTX_set_verify(s_ctx, WOLFSSL_VERIFY_PEER, NULL);
    {
        const uint8_t *caDer;
        uint32_t caLen;
        bool fromEeprom;
        CERT_PROVISION_ActiveCa(&caDer, &caLen, &fromEeprom);
        if (wolfSSL_CTX_load_verify_buffer(s_ctx, caDer, (long)caLen,
                WOLFSSL_FILETYPE_ASN1) != WOLFSSL_SUCCESS) {
            return false;
        }
    }

    return true;
}

static bool EncGlueClient_Deinit(void)
{
    if (s_ctx != NULL) {
        wolfSSL_CTX_free(s_ctx);
        s_ctx = NULL;
    }
    (void) wolfSSL_Cleanup();
    return true;
}

static bool EncGlueClient_Open(SYS_MODULE_OBJ obj, uintptr_t presHandle, uintptr_t transHandle, void *providerData)
{
    EncGlueClientConn *conn;

    (void)obj;
    (void)presHandle;

    if (s_ctx == NULL) {
        return false;
    }

    conn = (EncGlueClientConn *)malloc(sizeof(EncGlueClientConn));
    if (conn == NULL) {
        return false;
    }
    conn->transHandle = transHandle;
    conn->closeDeadline = 0u;
    conn->connectDeadline = 0u;
    conn->ssl = wolfSSL_new(s_ctx);
    if (conn->ssl == NULL) {
        free(conn);
        return false;
    }
    wolfSSL_SetIOReadCtx(conn->ssl, conn);
    wolfSSL_SetIOWriteCtx(conn->ssl, conn);

    memcpy(providerData, &conn, sizeof(conn));
    return true;
}

/* Pumps wolfSSL_connect() (the client-side counterpart of
   net_pres_enc_glue.c's EncGlue_Connect(), which pumps wolfSSL_accept()) -
   see that function's own comment for the fpDisconnect()-on-give-up
   reasoning, which applies identically here: net_pres.c's NET_PRES_Tasks()
   stops pumping fpConnect once status leaves the *_NEGOTIATING family, and
   never calls fpClose() off a FAILED status by itself, so this must force
   the transport down on giving up (real error or timeout) to drive
   app_mqtt.c's own NET_PRES_SocketWasReset()/WasDisconnected() check into
   the real cleanup path (NET_PRES_SocketDisconnect() -> fpClose() ->
   EncGlueClient_Close(), below). */
/* Diagnostics only (docs/mqtt-tls-agent-prompt.md live debugging, 2026-09-06):
 * NET_PRES_EncSessionStatus has no room for a reason code, and app_mqtt.c's
 * only other signal on failure is NET_PRES_SocketWasDisconnected() - "the
 * handshake didn't finish" with no way to tell "never got a TLS byte at all"
 * (real network/routing problem) from "wolfSSL rejected the peer's
 * certificate" (a real, load-bearing distinction while debugging exactly
 * that question). EncGlueClient_LastError() lets app_mqtt.c report the
 * actual wolfSSL error code (or 0 for "timed out, no error yet") in
 * 'mqtt_status' over Telnet - SYS_CONSOLE_PRINT elsewhere in this file only
 * reaches the serial/EDBG console, not whichever console asked. */
static int s_lastWolfSSLErr = 0;

int EncGlueClient_LastError(void)
{
    return s_lastWolfSSLErr;
}

static NET_PRES_EncSessionStatus EncGlueClient_Connect(void *providerData)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);
    int ret;

    if (conn->connectDeadline == 0u) {
        conn->connectDeadline = SYS_TIME_Counter64Get()
                               + (uint64_t)ENC_CLIENT_CONNECT_TIMEOUT_MS * EncGlueClient_TicksPerMs();
    }

    ret = wolfSSL_connect(conn->ssl);

    if (ret == WOLFSSL_SUCCESS) {
        return NET_PRES_ENC_SS_OPEN;
    }

    int err = wolfSSL_get_error(conn->ssl, ret);
    if (((err == WOLFSSL_ERROR_WANT_READ) || (err == WOLFSSL_ERROR_WANT_WRITE)) &&
        ((int64_t)(SYS_TIME_Counter64Get() - conn->connectDeadline) < 0)) {
        return NET_PRES_ENC_SS_CLIENT_NEGOTIATING;
    }

    /* Deadline expired while still WANT_READ/WANT_WRITE -> record 0 (no real
     * wolfSSL error, never got far enough) so app_mqtt.c can tell that case
     * apart from an actual rejection code below. */
    s_lastWolfSSLErr = ((err == WOLFSSL_ERROR_WANT_READ) || (err == WOLFSSL_ERROR_WANT_WRITE)) ? 0 : err;

    if (s_transObject != NULL && s_transObject->fpDisconnect != NULL) {
        (void)s_transObject->fpDisconnect(conn->transHandle);
    }
    return NET_PRES_ENC_SS_FAILED;
}

static NET_PRES_EncSessionStatus EncGlueClient_Close(void *providerData)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);

    /* Reachable, unlike net_pres_enc_glue.c's server-side EncGlue_Close():
     * NET_PRES_SocketClose() (net_pres.c) calls fpClose() unconditionally
     * whenever the ENCRYPTED bit is set, with NO check for whether fpOpen()
     * ever actually ran - true for ANY socket still sitting in
     * WAITING_TO_START_NEGOTIATION (providerData still all-zero, so `conn`
     * decodes to NULL here). A listening server socket never gets closed
     * before something connects to it, so net_pres_enc_glue.c never hits
     * this; a socket dialing OUT absolutely can, whenever the raw TCP
     * connect itself times out or is refused (before there is ever a TLS
     * session to shut down) - app_mqtt.c's mqtt_fail() closing a socket
     * still stuck in MQTT_ST_WAIT_TLS is exactly that path. Confirmed live
     * 2026-09-06: an unreachable broker address produced a BusFault here
     * (NULL->ssl inside wolfSSL_shutdown) before this guard was added. */
    if (conn == NULL) {
        return NET_PRES_ENC_SS_CLOSED;
    }

    /* Single-pass on purpose - same reasoning as net_pres_enc_glue.c's
     * EncGlue_Close(): net_pres.c never calls fpClose() a second time, so
     * returning NET_PRES_ENC_SS_CLOSING here would leak the WOLFSSL object
     * and this conn struct outright. See that function's comment for the
     * measured effect (C-runtime heap down to a 656-byte largest free block
     * after ~10 sessions, every TLS service on the board dead until reset).
     *
     * It matters at least as much on this client side: MQTT_Tasks() redials
     * every MQTT_RETRY_INTERVAL_MS after any failure, so a broker that is
     * simply not reachable would otherwise leak one session per retry - a
     * few minutes of a stopped broker would be enough to exhaust the heap. */
    (void)wolfSSL_shutdown(conn->ssl);   /* best effort - our close_notify goes out */

    wolfSSL_free(conn->ssl);
    free(conn);
    return NET_PRES_ENC_SS_CLOSED;
}

static int32_t EncGlueClient_Write(void *providerData, const uint8_t *buffer, uint16_t size)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);
    int ret = wolfSSL_write(conn->ssl, buffer, (int)size);
    if (ret < 0) {
        int err = wolfSSL_get_error(conn->ssl, ret);
        if ((err == WOLFSSL_ERROR_WANT_READ) || (err == WOLFSSL_ERROR_WANT_WRITE)) {
            return 0;
        }
        return -1;
    }
    return (int32_t)ret;
}

static int32_t EncGlueClient_Read(void *providerData, uint8_t *buffer, uint16_t size)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);
    int ret;

    if (buffer == NULL) {
        return 0;   /* per net_pres_encryptionproviderapi.h: ignored, not an error */
    }
    ret = wolfSSL_read(conn->ssl, buffer, (int)size);
    if (ret < 0) {
        int err = wolfSSL_get_error(conn->ssl, ret);
        if ((err == WOLFSSL_ERROR_WANT_READ) || (err == WOLFSSL_ERROR_WANT_WRITE)) {
            return 0;
        }
        return -1;
    }
    return (int32_t)ret;
}

static int32_t EncGlueClient_Peek(void *providerData, uint8_t *buffer, uint16_t size)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);
    int ret;

    if (buffer == NULL) {
        return 0;
    }
    ret = wolfSSL_peek(conn->ssl, buffer, (int)size);
    if (ret < 0) {
        return 0;
    }
    return (int32_t)ret;
}

static int32_t EncGlueClient_ReadReady(void *providerData)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);
    int pending = wolfSSL_pending(conn->ssl);
    if (pending > 0) {
        return (int32_t)pending;
    }
    return s_transObject->fpReadyToRead(conn->transHandle) ? 1 : 0;
}

static uint16_t EncGlueClient_WriteReady(void *providerData, uint16_t reqSize, uint16_t minSize)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);
    if (!s_transObject->fpReadyToWrite(conn->transHandle)) {
        return 0;
    }
    return (minSize != 0U) ? minSize : reqSize;
}

static bool EncGlueClient_IsInitialized(void)
{
    return (s_ctx != NULL);
}

static int32_t EncGlueClient_OutputSize(void *providerData, int32_t inSize)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);
    return (int32_t)wolfSSL_GetOutputSize(conn->ssl, (int)inSize);
}

static int32_t EncGlueClient_MaxOutputSize(void *providerData)
{
    EncGlueClientConn *conn = EncGlueClient_ConnFromProviderData(providerData);
    return (int32_t)wolfSSL_GetMaxOutputSize(conn->ssl);
}

const Net_ProvObject NET_PRES_EncProviderObject_wolfSSL_StreamClient = {
    .fpInit          = EncGlueClient_Init,
    .fpDeinit        = EncGlueClient_Deinit,
    .fpOpen          = EncGlueClient_Open,
    .fpConnect       = EncGlueClient_Connect,
    .fpClose         = EncGlueClient_Close,
    .fpWrite         = EncGlueClient_Write,
    .fpWriteReady    = EncGlueClient_WriteReady,
    .fpRead          = EncGlueClient_Read,
    .fpReadReady     = EncGlueClient_ReadReady,
    .fpPeek          = EncGlueClient_Peek,
    .fpIsInited      = EncGlueClient_IsInitialized,
    .fpOutputSize    = EncGlueClient_OutputSize,
    .fpMaxOutputSize = EncGlueClient_MaxOutputSize,
};
