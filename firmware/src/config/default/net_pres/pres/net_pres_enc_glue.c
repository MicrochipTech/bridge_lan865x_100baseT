/*******************************************************************************
 wolfSSL glue functions to work with Harmony's NET_PRES encryption provider API

 Description:
    Branch t1s-t1s-bridge-lan8670's Telnet/bootload TLS experiment. Implements
    the Net_ProvObject vtable (net_pres_encryptionproviderapi.h) on top of
    wolfSSL's classic custom-I/O API (WOLFSSL_USER_IO, see configuration.h),
    for NET_PRES stream-server sockets only - this project only needs a TLS
    *server*, one connection at a time (NO_WOLFSSL_CLIENT, NO_SESSION_CACHE).

    One WOLFSSL_CTX for the whole provider (fpInit, once). One small heap
    conn struct per connection (fpOpen/fpClose), holding the WOLFSSL* and the
    transHandle wolfSSL's IO callbacks need - NET_PRES only hands providerData
    (8 bytes, net_pres_local.h) to every call after fpOpen, so the conn
    struct's address is all that's stored there.

    Bring-up only: fpInit loads this project's own generated RSA-2048 CA/
    server cert/key (bridge_certs.h, see certs/bridge/ and certs/ca/ for the
    source .pem files and how to regenerate) - NOT wolfSSL's public test
    certs (wolfssl/certs_test.h), which turned out to be both expired (all
    dated 2022-2024) and mismatched (client_cert_der_2048 does not chain to
    ca_cert_der_2048 at all - verified with `openssl verify`, it is an
    unrelated self-signed cert). The CA's own private key still lives in this
    repo (certs/ca/ca_key.pem), which is fine for bring-up but must not
    happen for anything actually exposed to a network - see bridge_certs.h.
*******************************************************************************/

#include "net_pres_enc_glue.h"
#include "net_pres_transportapi.h"   /* full S_NET_PRES_TransportObject definition (fpRead/fpWrite/...) */

#include <stdlib.h>
#include <string.h>

#include "wolfssl/ssl.h"
#include "cert_provision.h"
#include "config/default/system/time/sys_time.h"   /* SYS_TIME_Counter64Get/FrequencyGet - ENC_CLOSE_TIMEOUT_MS */

/* How long EncGlue_Close() waits for a peer to complete wolfSSL_shutdown()'s
   bidirectional close_notify before giving up and freeing the connection
   anyway. Without this, a client that vanishes mid-shutdown - confirmed live:
   an `openssl s_client` session killed with Ctrl-C, no close_notify sent -
   leaves wolfSSL_shutdown() returning WANT_READ/WANT_WRITE forever, and with
   TCPIP_TELNET_MAX_CONNECTIONS=1 that one wedged slot then makes every future
   connection attempt time out at the TLS handshake stage, indistinguishable
   from the board being dead, until it is physically reset. */
#define ENC_CLOSE_TIMEOUT_MS     5000u

/* Same idea, other end of the connection's life: how long EncGlue_Connect()
   pumps wolfSSL_accept() before giving up on a handshake that never
   finishes. Without this, a client that opens the TCP connection and then
   stalls or abandons the handshake mid-flight - confirmed live (2026-09-05,
   scripts/discover.py's subnet scan under load) - leaves this socket's
   wolfSSL_accept() returning WANT_READ/WANT_WRITE forever. That is a WORSE
   wedge than the close-side one above: net_pres.c's own pump loop
   (NET_PRES_EncTasks) stops calling fpConnect once status is anything other
   than *_NEGOTIATING, so a connection stuck here does not even get the
   periodic re-checks the close side gets - confirmed live: raw TCP connect
   kept succeeding (something still accepted the SYN), but the TLS handshake
   timed out on every subsequent login attempt indefinitely, recovering only
   on a physical reset. A real handshake completes in ~1.2s (measured,
   docs/tls-poc-report.md sec.10); 15s gives a slow/legitimate client real
   margin while still bounding a stalled one. */
#define ENC_CONNECT_TIMEOUT_MS  15000u

typedef struct {
    WOLFSSL   *ssl;
    uintptr_t  transHandle;
    uint64_t   closeDeadline;    /* 0 = EncGlue_Close() not yet called for this conn */
    uint64_t   connectDeadline;  /* 0 = EncGlue_Connect() not yet called for this conn */
} EncGlueConn;

/* One provider, one transport type (stream/TCP) - both set once at fpInit,
   read from every per-connection callback via the stashed transHandle. */
static WOLFSSL_CTX                            *s_ctx         = NULL;
static const struct S_NET_PRES_TransportObject *s_transObject = NULL;
static uint64_t                                s_ticksPerMs  = 0u;

static EncGlueConn *EncGlue_ConnFromProviderData(void *providerData)
{
    EncGlueConn *conn;
    memcpy(&conn, providerData, sizeof(conn));
    return conn;
}

/* Shared by EncGlue_Connect()/EncGlue_Close() - both bound their respective
   wait with a deadline computed from this. */
static uint64_t EncGlue_TicksPerMs(void)
{
    if (s_ticksPerMs == 0u) {
        s_ticksPerMs = (uint64_t)SYS_TIME_FrequencyGet() / 1000ULL;
    }
    return s_ticksPerMs;
}

/* wolfSSL custom I/O: called from inside wolfSSL_accept/read/write/shutdown
   whenever it needs to move bytes across the actual transport. Non-blocking,
   matching the transport object's own non-blocking TCPIP_TCP_* calls -
   "no bytes right now" becomes WANT_READ/WANT_WRITE, which wolfSSL_get_error()
   turns back into NET_PRES_ENC_SS_*_NEGOTIATING for fpConnect to report. */
static int EncGlue_IoRecv(WOLFSSL *ssl, char *buf, int sz, void *ctx)
{
    EncGlueConn *conn = (EncGlueConn *)ctx;
    uint16_t n = s_transObject->fpRead(conn->transHandle, (uint8_t *)buf, (uint16_t)sz);
    (void)ssl;
    if (n == 0U) {
        return WOLFSSL_CBIO_ERR_WANT_READ;
    }
    return (int)n;
}

static int EncGlue_IoSend(WOLFSSL *ssl, char *buf, int sz, void *ctx)
{
    EncGlueConn *conn = (EncGlueConn *)ctx;
    uint16_t n = s_transObject->fpWrite(conn->transHandle, (const uint8_t *)buf, (uint16_t)sz);
    (void)ssl;
    if (n == 0U) {
        return WOLFSSL_CBIO_ERR_WANT_WRITE;
    }
    return (int)n;
}

static bool EncGlue_Init(struct S_NET_PRES_TransportObject *transObject)
{
    s_transObject = transObject;

    if (s_ctx != NULL) {
        return true;   /* already initialized - fpInit can be called more than once */
    }

    (void) wolfSSL_Init();

    s_ctx = wolfSSL_CTX_new(wolfTLSv1_2_server_method());
    if (s_ctx == NULL) {
        return false;
    }

    wolfSSL_CTX_SetIORecv(s_ctx, EncGlue_IoRecv);
    wolfSSL_CTX_SetIOSend(s_ctx, EncGlue_IoSend);

    /* This project's own CA/server cert+key, not wolfSSL's test PKI - either
       the compiled-in default (bridge_certs.h) or an EEPROM override pushed
       remotely via the 'cert_arm'/'cert_save' commands (cert_provision.c),
       whichever CERT_PROVISION_Initialize() found valid at boot. Either way
       CERT_PROVISION_ActiveServerIdentity() always returns a real cert/key -
       the compiled-in default backs up an invalid/absent EEPROM record. */
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

    /* Mutual TLS: a completed handshake alone is not access control - it
       only proves *a* TLS client talked to us, not *which* one. Require the
       client to present a certificate and reject the handshake outright if
       it doesn't chain up to the CA below (WOLFSSL_VERIFY_FAIL_IF_NO_PEER_CERT
       fails a bare "no certificate" the same as a wrong one). This project's
       own CA (bridge_certs.h) signs both the server cert above and the
       client cert issued to whoever is meant to reach this Telnet/bootload
       (certs/client/client_cert.pem + client_key.pem) - unlike wolfSSL's
       test PKI (see file header), server and client here actually chain to
       the same CA (verified with `openssl verify`). Still bring-up-grade:
       the CA private key sits in this repo (certs/ca/ca_key.pem), which
       a real deployment must not do. */
    wolfSSL_CTX_set_verify(s_ctx, WOLFSSL_VERIFY_PEER | WOLFSSL_VERIFY_FAIL_IF_NO_PEER_CERT, NULL);
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

static bool EncGlue_Deinit(void)
{
    if (s_ctx != NULL) {
        wolfSSL_CTX_free(s_ctx);
        s_ctx = NULL;
    }
    (void) wolfSSL_Cleanup();
    return true;
}

static bool EncGlue_Open(SYS_MODULE_OBJ obj, uintptr_t presHandle, uintptr_t transHandle, void *providerData)
{
    EncGlueConn *conn;

    (void)obj;
    (void)presHandle;

    if (s_ctx == NULL) {
        return false;
    }

    conn = (EncGlueConn *)malloc(sizeof(EncGlueConn));
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

/* Pumped once per NET_PRES task pass while negotiating - see
   Net_ProvConnect's doc comment in net_pres_encryptionproviderapi.h.

   Bounded by ENC_CONNECT_TIMEOUT_MS: see its comment for why a stalled or
   abandoned handshake must not be allowed to sit here forever.

   On timeout this does NOT free `conn` itself, on purpose - net_pres.c's own
   NET_PRES_EncTasks() only stops calling fpConnect once status leaves the
   *_NEGOTIATING family (net_pres.c ~line 202); it never calls fpClose() off
   the back of a FAILED status by itself. telnet.c's own session loop only
   notices a dead connection via NET_PRES_SocketWasReset()/WasDisconnected()
   - both TRANSPORT-level checks, blind to the encryption layer's status
   (net_pres.c ~line 491-523) - and, on catching one, calls
   NET_PRES_SocketDisconnect(), which is the thing that actually calls
   fpClose() for a socket whose status is already past
   WAITING_TO_START_NEGOTIATION and resets it to accept a new client
   (net_pres.c ~line 526-561). So returning FAILED alone would leave `conn`
   allocated and this Telnet slot permanently stuck - because nothing
   transport-level ever changed to trigger that check. Calling the
   transport's own fpDisconnect() here is what makes WasDisconnected() true
   on telnet.c's very next pass, driving it through
   NET_PRES_SocketDisconnect() into a proper fpClose() call (EncGlue_Close(),
   below) - which is what actually frees `conn`. Freeing it here directly,
   instead, would race that same fpClose() call landing later on an
   already-freed pointer - a use-after-free / double-free.

   Applies to BOTH ways this gives up - the new bounded timeout below, and
   the pre-existing "any other wolfSSL error" case (bad/rejected client
   cert, protocol error): that path had exactly the same latent gap before
   this fix, just harder to hit than a stalled handshake. */
static NET_PRES_EncSessionStatus EncGlue_Connect(void *providerData)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
    int ret;

    if (conn->connectDeadline == 0u) {
        conn->connectDeadline = SYS_TIME_Counter64Get()
                               + (uint64_t)ENC_CONNECT_TIMEOUT_MS * EncGlue_TicksPerMs();
    }

    ret = wolfSSL_accept(conn->ssl);

    if (ret == WOLFSSL_SUCCESS) {
        return NET_PRES_ENC_SS_OPEN;
    }

    int err = wolfSSL_get_error(conn->ssl, ret);
    if (((err == WOLFSSL_ERROR_WANT_READ) || (err == WOLFSSL_ERROR_WANT_WRITE)) &&
        ((int64_t)(SYS_TIME_Counter64Get() - conn->connectDeadline) < 0)) {
        return NET_PRES_ENC_SS_SERVER_NEGOTIATING;
    }

    /* Giving up - either a real error, or the WANT_READ/WRITE deadline
     * above passed. Force the transport down so telnet.c's
     * WasDisconnected() check (next pass) drives the real cleanup through
     * NET_PRES_SocketDisconnect(). */
    if (s_transObject != NULL && s_transObject->fpDisconnect != NULL) {
        (void)s_transObject->fpDisconnect(conn->transHandle);
    }
    return NET_PRES_ENC_SS_FAILED;
}

/* Pumps wolfSSL_shutdown()'s bidirectional close_notify exchange; frees the
   conn struct only once it reports CLOSED, matching the "provider data has
   been freed" contract for that state.

   Bounded by ENC_CLOSE_TIMEOUT_MS: a peer that vanished without sending its
   own close_notify would otherwise keep this returning CLOSING forever - see
   the define's comment for why that matters with only one connection slot. */
static NET_PRES_EncSessionStatus EncGlue_Close(void *providerData)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
    int ret;

    if (s_ticksPerMs == 0u) {
        s_ticksPerMs = (uint64_t)SYS_TIME_FrequencyGet() / 1000ULL;
    }
    if (conn->closeDeadline == 0u) {
        conn->closeDeadline = SYS_TIME_Counter64Get()
                             + (uint64_t)ENC_CLOSE_TIMEOUT_MS * s_ticksPerMs;
    }

    ret = wolfSSL_shutdown(conn->ssl);

    if (ret == WOLFSSL_SUCCESS) {
        wolfSSL_free(conn->ssl);
        free(conn);
        return NET_PRES_ENC_SS_CLOSED;
    }

    int err = wolfSSL_get_error(conn->ssl, ret);
    if ((err == WOLFSSL_ERROR_WANT_READ) || (err == WOLFSSL_ERROR_WANT_WRITE)) {
        if ((int64_t)(SYS_TIME_Counter64Get() - conn->closeDeadline) < 0) {
            return NET_PRES_ENC_SS_CLOSING;
        }
        /* Timed out waiting for the peer's close_notify - give up on a
           graceful shutdown and free the slot anyway. */
    }

    /* Peer gone/error mid-shutdown, or the timeout above fired - nothing
       more to send, clean up now. */
    wolfSSL_free(conn->ssl);
    free(conn);
    return NET_PRES_ENC_SS_CLOSED;
}

static int32_t EncGlue_Write(void *providerData, const uint8_t *buffer, uint16_t size)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
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

static int32_t EncGlue_Read(void *providerData, uint8_t *buffer, uint16_t size)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
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

static int32_t EncGlue_Peek(void *providerData, uint8_t *buffer, uint16_t size)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
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

/* Approximation: already-decrypted bytes wolfSSL is holding, or - if none -
   whether the transport has raw bytes worth trying wolfSSL_read() against.
   wolfSSL only decrypts on demand (inside wolfSSL_read/peek), so this can't
   report an exact decrypted-byte count without actually reading. */
static int32_t EncGlue_ReadReady(void *providerData)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
    int pending = wolfSSL_pending(conn->ssl);
    if (pending > 0) {
        return (int32_t)pending;
    }
    return s_transObject->fpReadyToRead(conn->transHandle) ? 1 : 0;
}

static uint16_t EncGlue_WriteReady(void *providerData, uint16_t reqSize, uint16_t minSize)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
    if (!s_transObject->fpReadyToWrite(conn->transHandle)) {
        return 0;
    }
    return (minSize != 0U) ? minSize : reqSize;
}

static bool EncGlue_IsInitialized(void)
{
    return (s_ctx != NULL);
}

static int32_t EncGlue_OutputSize(void *providerData, int32_t inSize)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
    return (int32_t)wolfSSL_GetOutputSize(conn->ssl, (int)inSize);
}

static int32_t EncGlue_MaxOutputSize(void *providerData)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
    return (int32_t)wolfSSL_GetMaxOutputSize(conn->ssl);
}

const Net_ProvObject NET_PRES_EncProviderObject_wolfSSL_Stream = {
    .fpInit          = EncGlue_Init,
    .fpDeinit        = EncGlue_Deinit,
    .fpOpen          = EncGlue_Open,
    .fpConnect       = EncGlue_Connect,
    .fpClose         = EncGlue_Close,
    .fpWrite         = EncGlue_Write,
    .fpWriteReady    = EncGlue_WriteReady,
    .fpRead          = EncGlue_Read,
    .fpReadReady     = EncGlue_ReadReady,
    .fpPeek          = EncGlue_Peek,
    .fpIsInited      = EncGlue_IsInitialized,
    .fpOutputSize    = EncGlue_OutputSize,
    .fpMaxOutputSize = EncGlue_MaxOutputSize,
};
