# Agenten-Prompt: MQTT über TLS (Paho-Client + Broker-Tab im GUI)

Aufgabe: MQTT über TLS (Paho-Client auf dem Board, Broker im Python-GUI-Tool) für
dieses Projekt (bridge_lan865x_100baseT) implementieren. Ausschließlich TLS/mTLS -
kein Klartext-MQTT (Port 1883) darf irgendwo erreichbar sein, auch nicht als Fallback.

## Architektur (Vorgabe, nicht optional)

- Das SAME54-Board ist MQTT-CLIENT (Paho Embedded-C), NICHT der Broker. Ein Broker
  auf dem Board selbst ist bei diesem Flash-/RAM-Budget nicht sinnvoll.
- Der Broker läuft im bestehenden Python-GUI-Tool (scripts/bridge_gui_telnet.py,
  Tkinter), in einem NEUEN eigenen Tab. Der Tab muss:
  - den Broker starten/stoppen und seinen Status anzeigen (Port, TLS aktiv,
    Anzahl verbundener Clients),
  - verbundene MQTT-Clients live auflisten (Client-ID, IP, Cert-CN, Verbindungszeit),
  - eingehende Publish-Nachrichten live anzeigen (Topic, Payload, Zeitstempel).
- Referenz für den Broker: Python hat keinen "offiziellen" MQTT-Broker in der Stdlib;
  die brauchbare, gepflegte Wahl ist **amqtt** (asyncio-basiert, pip install amqtt).
  hbmqtt ist der veraltete Vorgänger - nicht verwenden.

## Bestehende TLS/PKI-Infrastruktur (wiederverwenden, nicht neu erfinden)

Dieses Projekt hat bereits eine funktionierende mTLS-Lösung für Telnet+Bootload
(siehe docs/tls-poc-report.md - vollständig lesen vor Beginn). Relevant für MQTT:

- Eine Projekt-CA unter certs/ca/ca_cert.pem + ca_key.pem (RSA-2048).
- scripts/pki.py: Python-Modul zum Ausstellen von Leaf-Zertifikaten aus dieser CA
  (issue_client_identity() für CLIENT_AUTH-Leafs, issue_board_identity() für
  SERVER_AUTH-Leafs pro Board). Für den Broker muss dieses Modul erweitert werden
  (issue_mqtt_broker_identity()) - für board-seitige MQTT-Client-Identitäten NICHT,
  siehe Entscheidung unten.
- Firmware-seitig: firmware/src/bridge_certs.h (eingebettete DER-Arrays: CA,
  Server-Cert, Server-Key) und firmware/src/cert_provision.h/.c (Laufzeit-Override
  der Identität über EEPROM, CERT_PROVISION_ActiveServerIdentity() /
  CERT_PROVISION_ActiveCa()).
- Firmware-seitig TLS-Provider: firmware/src/config/default/net_pres/pres/
  net_pres_enc_glue.c - ein wolfSSL-Provider hinter Harmonys NET_PRES-
  Verschlüsselungs-API, aktuell nur für die SERVER-Rolle gebaut (Telnet, Bootload).

## Kritischer Blocker, den der Agent zuerst lösen muss

firmware/src/config/default/configuration.h Zeile 515: `#define NO_WOLFSSL_CLIENT`.
Das deaktiviert die komplette wolfSSL-TLS-Client-Rolle im Firmware-Build. Ein MQTT-
Client, der sich zu einem externen Broker verbindet, IST TLS-Client. Dieses Define
muss entfernt (oder MQTT-spezifisch bedingt kompiliert) werden, und
net_pres_enc_glue.c braucht einen zweiten Pfad: einen WOLFSSL_CTX in Client-Rolle
(wolfSSL_CTX_new(wolfTLSv1_2_client_method())) neben dem bestehenden Server-CTX,
inklusive Peer-Verifikation der Broker-Identität gegen die Projekt-CA.

## Referenzimplementierung, die es bereits gibt

Unter C:\work\Gauselmann\Bridge\wireless_apps_pic32mzw1_wfi32e01\apps\
paho_mqtt_tls_client\ liegt ein vollständiges Microchip-Harmony-Beispiel: Paho
Embedded-C (firmware/src/third_party/paho.mqtt.embedded-c), TLS über wolfSSL
(firmware/src/third_party/wolfssl), app_mqtt.c/app_cert.h als Integrationsmuster.
Als Vorlage für die Transport-Anbindung verwenden, ABER an NET_PRES anpassen
(dieses Projekt bindet TLS nicht direkt an wolfSSL-Sockets, sondern über Harmonys
NET_PRES_SKT_ENCRYPTED_STREAM_*-Abstraktion, wie es telnet.c und bootload.c
bereits vormachen).

## Umzusetzen

### Firmware
1. NO_WOLFSSL_CLIENT-Blocker lösen (siehe oben).
2. paho.mqtt.embedded-c vendoren (Layout wie beim wolfSSL-Vendoring in
   docs/tls-poc-report.md §2.1 beschrieben: nur was tatsächlich gebraucht wird,
   volle Header, minimale .c-Liste kompiliert).
3. Neues Modul firmware/src/app_mqtt.c/.h nach dem Muster von cert_provision.c/
   bootload.c: MQTT_Initialize() (einmalig, nach SYS_CMD/EEPROM) und
   MQTT_Tasks() (jeden SYS_Tasks()-Zyklus), analog zu CERT_PROVISION_Tasks().
4. Paho-Transport-Callbacks auf NET_PRES_SKT_ENCRYPTED_STREAM_CLIENT umbiegen,
   nicht auf rohe TCPIP_TCP_*-Aufrufe.
5. mTLS-Identität - ENTSCHIEDEN (nicht mehr offen): das Board präsentiert dem
   Broker sein bereits vorhandenes, von cert_provision.c verwaltetes
   Server-Cert/Key (CERT_PROVISION_ActiveServerIdentity()) als eigene
   Client-Identität, KEIN separates neues Zertifikat. Begründung: das
   EEPROM-Budget für cert_provision.c's Zertifikatsspeicher ist mit den
   3 bestehenden Slots (server_cert, server_key, ca_cert je bis 1280 B) auf
   3864 von 4064 verfügbaren Bytes bereits praktisch ausgeschöpft (siehe
   cert_provision.c CERT_MAX_DER-Kommentar) - für weitere RSA-2048-Slots ist
   kein Platz. Verifiziert wird das Broker-Zertifikat gegen dieselbe CA
   (g_bridge_ca_cert_der aus bridge_certs.h bzw. CERT_PROVISION_ActiveCa()).
6. Sinnvolle Telemetriedaten publizieren (Link-Status, PLCA-Stats, Zähler - siehe
   vorhandene SYS_CMD-Kommandogruppen/meminfo als Quelle) auf klar benannten
   Topics, z. B. `bridge/<board_id>/...`.
7. Ressourcen-Risiken aus docs/tls-poc-report.md beachten und im Ergebnis kurz
   bewerten: §7 (ein RSA-2048-Handshake blockiert die Single-Thread-Superloop für
   ~1.6 s - bei MQTT unkritischer als bei Telnet, da nur einmal beim Connect statt
   pro Session, aber trotzdem zu nennen), §3.2 (Heap-Headroom ist mit
   Telnet+Bootload bereits knapp - eine dritte dauerhafte TLS-Verbindung muss
   gegengerechnet werden, ggf. TCPIP_TELNET_MAX_CONNECTIONS/Heap-Größen anpassen).

### Python-Seite (Broker + GUI)
1. Neues Modul scripts/mqtt_broker.py, GUI-frei und eigenständig testbar
   (gleiches Prinzip wie scripts/pki.py/bootload.py - Docstring dort erklärt
   die Konvention): kapselt amqtt mit TLS-only-Listener (Standardport 8883,
   KEIN Klartext-Listener auf 1883), Broker-Serverzertifikat + Key aus
   certs/mqtt/ (pki.issue_mqtt_broker_identity()), Client-Zertifikat-Pflicht
   (mTLS) gegen certs/ca/ca_cert.pem.
2. Broker-Start MUSS fehlschlagen (nicht klammheimlich auf Klartext
   zurückfallen), wenn Zertifikat/Key fehlen oder ungültig sind.
3. Neuer Tab in scripts/bridge_gui_telnet.py nach dem Muster von
   create_certificates_tab() (Zeile ~1602-1616: eigene create_*_tab()-Methode,
   self.notebook.add(frame, text="MQTT")): Start/Stop-Button, Status, Live-
   Client-Liste (Treeview, wie z. B. bei der Registers-Tab-Struktur verwendet),
   Live-Nachrichten-Log.

## Akzeptanzkriterien
- Ein MQTT-Client ohne gültiges, von der Projekt-CA signiertes Client-Zertifikat
  wird vom Broker abgelehnt.
- Eine reine Klartext-Verbindung zum Broker-Port liefert nichts (analog zum in
  docs/tls-poc-report.md §6 dokumentierten Telnet-Verhalten).
- Das reale Board verbindet sich per mTLS zum Broker, publiziert auf einem Topic,
  die Nachricht erscheint live im neuen GUI-Tab.
- Kurzer Ressourcenbericht (Flash/RAM-Delta, analog zu docs/tls-poc-report.md §3)
  am Ende, nach demselben Muster wie der bestehende Report.

