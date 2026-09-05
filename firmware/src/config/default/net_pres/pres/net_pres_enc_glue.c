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
    server cert/key (bridge_certs.h, see certs/bridge/ for the source .pem
    files and how to regenerate) - NOT wolfSSL's public test certs
    (wolfssl/certs_test.h), which turned out to be both expired (all dated
    2022-2024) and mismatched (client_cert_der_2048 does not chain to
    ca_cert_der_2048 at all - verified with `openssl verify`, it is an
    unrelated self-signed cert). The CA's own private key still lives in this
    repo (certs/bridge/ca_key.pem), which is fine for bring-up but must not
    happen for anything actually exposed to a network - see bridge_certs.h.
*******************************************************************************/

#include "net_pres_enc_glue.h"
#include "net_pres_transportapi.h"   /* full S_NET_PRES_TransportObject definition (fpRead/fpWrite/...) */

#include <stdlib.h>
#include <string.h>

#include "wolfssl/ssl.h"
#include "bridge_certs.h"

typedef struct {
    WOLFSSL   *ssl;
    uintptr_t  transHandle;
} EncGlueConn;

/* One provider, one transport type (stream/TCP) - both set once at fpInit,
   read from every per-connection callback via the stashed transHandle. */
static WOLFSSL_CTX                            *s_ctx         = NULL;
static const struct S_NET_PRES_TransportObject *s_transObject = NULL;

static EncGlueConn *EncGlue_ConnFromProviderData(void *providerData)
{
    EncGlueConn *conn;
    memcpy(&conn, providerData, sizeof(conn));
    return conn;
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

    /* Bring-up cert/key only - see file header comment. This project's own
       CA/server cert+key (bridge_certs.h), not wolfSSL's test PKI. */
    if (wolfSSL_CTX_use_certificate_buffer(s_ctx, g_bridge_server_cert_der,
            (long)g_bridge_server_cert_der_len, WOLFSSL_FILETYPE_ASN1) != WOLFSSL_SUCCESS) {
        return false;
    }
    if (wolfSSL_CTX_use_PrivateKey_buffer(s_ctx, g_bridge_server_key_der,
            (long)g_bridge_server_key_der_len, WOLFSSL_FILETYPE_ASN1) != WOLFSSL_SUCCESS) {
        return false;
    }

    /* Mutual TLS: a completed handshake alone is not access control - it
       only proves *a* TLS client talked to us, not *which* one. Require the
       client to present a certificate and reject the handshake outright if
       it doesn't chain up to the CA below (WOLFSSL_VERIFY_FAIL_IF_NO_PEER_CERT
       fails a bare "no certificate" the same as a wrong one). This project's
       own CA (bridge_certs.h) signs both the server cert above and the
       client cert issued to whoever is meant to reach this Telnet/bootload
       (certs/bridge/client_cert.pem + client_key.pem) - unlike wolfSSL's
       test PKI (see file header), server and client here actually chain to
       the same CA (verified with `openssl verify`). Still bring-up-grade:
       the CA private key sits in this repo (certs/bridge/ca_key.pem), which
       a real deployment must not do. */
    wolfSSL_CTX_set_verify(s_ctx, WOLFSSL_VERIFY_PEER | WOLFSSL_VERIFY_FAIL_IF_NO_PEER_CERT, NULL);
    if (wolfSSL_CTX_load_verify_buffer(s_ctx, g_bridge_ca_cert_der,
            (long)g_bridge_ca_cert_der_len, WOLFSSL_FILETYPE_ASN1) != WOLFSSL_SUCCESS) {
        return false;
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
   Net_ProvConnect's doc comment in net_pres_encryptionproviderapi.h. */
static NET_PRES_EncSessionStatus EncGlue_Connect(void *providerData)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
    int ret = wolfSSL_accept(conn->ssl);

    if (ret == WOLFSSL_SUCCESS) {
        return NET_PRES_ENC_SS_OPEN;
    }

    int err = wolfSSL_get_error(conn->ssl, ret);
    if ((err == WOLFSSL_ERROR_WANT_READ) || (err == WOLFSSL_ERROR_WANT_WRITE)) {
        return NET_PRES_ENC_SS_SERVER_NEGOTIATING;
    }
    return NET_PRES_ENC_SS_FAILED;
}

/* Pumps wolfSSL_shutdown()'s bidirectional close_notify exchange; frees the
   conn struct only once it reports CLOSED, matching the "provider data has
   been freed" contract for that state. */
static NET_PRES_EncSessionStatus EncGlue_Close(void *providerData)
{
    EncGlueConn *conn = EncGlue_ConnFromProviderData(providerData);
    int ret = wolfSSL_shutdown(conn->ssl);

    if (ret == WOLFSSL_SUCCESS) {
        wolfSSL_free(conn->ssl);
        free(conn);
        return NET_PRES_ENC_SS_CLOSED;
    }

    int err = wolfSSL_get_error(conn->ssl, ret);
    if ((err == WOLFSSL_ERROR_WANT_READ) || (err == WOLFSSL_ERROR_WANT_WRITE)) {
        return NET_PRES_ENC_SS_CLOSING;
    }

    /* Peer gone/error mid-shutdown - nothing more to send, clean up now. */
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
