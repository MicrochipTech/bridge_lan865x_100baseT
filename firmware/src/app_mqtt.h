/*
 * app_mqtt.h - MQTT-over-TLS status publisher (docs/mqtt-tls-agent-prompt.md).
 *
 * This board as an MQTT CLIENT (never a broker - see the doc for why),
 * publishing a small JSON status line every MQTT_PUBLISH_INTERVAL_MS to a
 * broker over mutual TLS (net_pres_enc_glue_client.c). Modeled on
 * cert_provision.c/bootload.c's shape: a small non-blocking state machine
 * driven from APP_Tasks(), not a blocking connect-then-loop client - this
 * firmware is a single-threaded cooperative superloop with no room for a
 * call that blocks waiting on the network (see docs/tls-poc-report.md §7).
 *
 * Deliberately built directly on the pure, I/O-free MQTTPacket
 * (de)serializers (third_party/paho.mqtt.embedded-c/MQTTPacket/) rather than
 * that library's own MQTTClient-C wrapper: MQTTClient-C's MQTTConnect()/
 * MQTTPublish() block internally (a spin-loop reading until a reply or
 * timeout) - fine for a threaded host, but on this superloop it would starve
 * NET_PRES_Tasks() (which is what actually pumps the TLS handshake this
 * connect is waiting on) and deadlock. The MQTTPacket serializers have no
 * such assumption; this file supplies its own small CONNECT/CONNACK/PUBLISH
 * state machine around them, the same way cert_provision.c/bootload.c each
 * implement their own tiny protocol state machine directly against NET_PRES
 * rather than pulling in a blocking helper.
 *
 * Identity: uses whatever cert_provision.c already manages on this board
 * (CERT_PROVISION_ActiveServerIdentity()/ActiveCa()) as its TLS-client
 * credential - see net_pres_enc_glue_client.c's header comment for why this
 * does NOT mint a separate MQTT-only certificate.
 */
#ifndef APP_MQTT_H
#define APP_MQTT_H

#ifdef __cplusplus
extern "C" {
#endif

/* Register the 'mqtt' console command group (mqtt_broker/mqtt_status) and
 * seed the broker address from the compiled default. Call once, after
 * SYS_CMD is up - same place as CERT_PROVISION_Initialize(). */
void MQTT_Initialize(void);

/* Drive the connect/publish state machine. Call every APP_Tasks() cycle,
 * same as CERT_PROVISION_Tasks()/BOOTLOAD_Tasks(). Does nothing (aside from
 * the retry-deadline check) until a broker address is configured. */
void MQTT_Tasks(void);

#ifdef __cplusplus
}
#endif

#endif /* APP_MQTT_H */
