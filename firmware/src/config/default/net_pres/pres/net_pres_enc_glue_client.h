/*******************************************************************************
 Header file for the wolfSSL TLS-CLIENT glue functions to work with Harmony


  Summary:


  Description:

*******************************************************************************/

/*
Copyright (C) 2013-2025, Microchip Technology Inc., and its subsidiaries. All rights reserved.

The software and documentation is provided by microchip and its contributors
"as is" and any express, implied or statutory warranties, including, but not
limited to, the implied warranties of merchantability, fitness for a particular
purpose and non-infringement of third party intellectual property rights are
disclaimed to the fullest extent permitted by law. In no event shall microchip
or its contributors be liable for any direct, indirect, incidental, special,
exemplary, or consequential damages (including, but not limited to, procurement
of substitute goods or services; loss of use, data, or profits; or business
interruption) however caused and on any theory of liability, whether in contract,
strict liability, or tort (including negligence or otherwise) arising in any way
out of the use of the software and documentation, even if advised of the
possibility of such damage.

Except as expressly permitted hereunder and subject to the applicable license terms
for any third-party software incorporated in the software and any applicable open
source software license terms, no license or other rights, whether express or
implied, are granted under any patent or other intellectual property rights of
Microchip or any third party.
*/


#ifndef H_NET_TLS_WOLFSSL_GLUE_CLIENT_H_
#define H_NET_TLS_WOLFSSL_GLUE_CLIENT_H_

#include "configuration.h"
#include "net_pres/pres/net_pres.h"
#include "net_pres/pres/net_pres_encryptionproviderapi.h"
#ifdef __CPLUSPLUS
extern "C" {
#endif

/* wolfSSL-backed stream-CLIENT encryption provider (MQTT-over-TLS work, see
   docs/mqtt-tls-agent-prompt.md and net_pres_enc_glue_client.c). Wire into
   initialization.c's netPresCfgs[...].pProvObject_sc to make NET_PRES
   stream-client sockets (NET_PRES_SKT_ENCRYPTED_STREAM_CLIENT) capable of
   dialing OUT over TLS - the counterpart to net_pres_enc_glue.c's
   NET_PRES_EncProviderObject_wolfSSL_Stream, which only ever accepts
   inbound connections (Telnet, bootload, cert_provision). */
extern const Net_ProvObject NET_PRES_EncProviderObject_wolfSSL_StreamClient;

/* Diagnostics only - see net_pres_enc_glue_client.c's own comment. The
   wolfSSL error code (wolfSSL_get_error()) from the most recent failed
   client-role handshake, or 0 if it never got further than WANT_READ/
   WANT_WRITE before timing out (i.e. no real protocol-level rejection was
   ever seen - most likely a network/routing problem, not a TLS/cert one). */
int EncGlueClient_LastError(void);

#ifdef __CPLUSPLUS
}
#endif
#endif //H_NET_TLS_WOLFSSL_GLUE_CLIENT_H_
