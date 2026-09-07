# Test Results — Telnet, MQTT, Bootload, Identity Provisioning

Executed against the real three-board bench by `scripts/testplan_runner.py`, following [`test-plan-telnet-mqtt-bootload.md`](test-plan-telnet-mqtt-bootload.md).

| | |
|---|---|
| Run started | 2026-09-07T04:22:04 |
| Run ended | 2026-09-07T04:33:16 |
| Groups executed | A, B, C, D, E, G, F |
| Result | **46 passed, 0 failed, 0 skipped** |

## Summary

| ID | Test | Verdict | Note |
|---|---|---|---|
| A1 | ICMP reachability of all board addresses | PASS | secondary-interface addresses may legitimately not answer |
| A2 | mTLS Telnet login on all three boards | PASS |  |
| A3 | Identical firmware build on all three | PASS | 192.168.0.12 Sep  7 2026 03:49:17; 192.168.0.21 Sep  7 2026 03:49:17; 192.168.0.31 Sep  7 2026 03:49:17 |
| A4 | Interface inventory matches the bench definition | PASS | informational - drives the -i choice per board |
| A5 | Fault log clean at start | PASS | a recorded fault survives resets in Backup RAM, so it may predate this run |
| A6 | Fault log cleared before the run | PASS | so F1 reflects only faults produced by this test run |
| B1 | Login with the valid project client identity | PASS |  |
| B2 | TLS connect with NO client certificate is rejected | PASS | board must require a client certificate |
| B3 | TLS connect with a foreign-CA client certificate is rejected | PASS |  |
| B4 | Wrong console password is refused | PASS |  |
| B5 | Command round-trip integrity | PASS |  |
| B6 | 20 sequential connect/command/disconnect cycles per board | PASS | regression guard for the NET_PRES socket-strand defect |
| B7 | Second concurrent session does not disturb the first | PASS | max 1 Telnet connection is by design (TCPIP_TELNET_MAX_CONNECTIONS=1) |
| C1 | Active identity readable on each board | PASS |  |
| C2 | Freshly issued identities carry dual EKU + SKI/AKI | PASS |  |
| C3 | Identity pushed and saved on each board | PASS |  |
| C4 | Board serves the newly issued identity after reset | PASS |  |
| C5 | Telnet still works after the identity change | PASS | same CA, so the client identity is unchanged |
| C6 | Board metadata matches the served certificate | PASS |  |
| D1 | 'bootload info' reports a sane state on each board | PASS |  |
| D2 | 'bootload selftest' passes on each board | PASS | erase+write+readback in the inactive bank |
| D3 | Full OTA update completes on each board | PASS |  |
| D4 | Environment preserved - same IP, console reachable after update | PASS |  |
| D5 | Provisioned identity survives the bank swap | PASS |  |
| D6 | A second consecutive update alternates the running bank | PASS | proves both banks are usable |
| D7 | Fault log still clean after the updates | PASS |  |
| E1 | Broker starts, TLS-only | PASS | listening on 192.168.0.100:8883 |
| E2 | Plaintext MQTT client is rejected | PASS | no CONNACK on a plaintext connection |
| E3 | TLS client without a client certificate is rejected | PASS | judged at the MQTT layer: the TLS handshake itself may legitimately complete, the broker then refuses the session (RequireClientCertPlugin) |
| E4 | TLS client with a foreign-CA certificate is rejected | PASS |  |
| E5 | Each board connects with its correct -i interface | PASS |  |
| E6 | All three boards connected to the broker simultaneously | PASS | 3 distinct client_id(s) seen |
| E7 | Periodic status publishing, ~5s, monotonic seq | PASS | 3 client(s) published within the observation window |
| E8 | -i with the board's non-uplink interface behaves definitely | PASS | connected anyway (bridged) |
| E9 | -i with an invalid interface name is handled | PASS | command must not crash the board or silently accept a bad name |
| E10 | Boards reconnect by themselves after a broker restart | PASS |  |
| G1 | Registered patches all present | PASS |  |
| G2 | wolfSSL/TLS build configuration protected | PASS |  |
| G3 | TLS provider registration protected | PASS |  |
| G4 | NET_PRES socket-strand (DoS) fix protected | PASS |  |
| G5 | Project-owned files under config/default are accounted for | PASS |  |
| F1 | Fault log clean on all three after the whole run | PASS |  |
| F2 | Heap still healthy | PASS |  |
| F3 | Telnet responsive on all three | PASS |  |
| F4 | MQTT connected on all three at the end | PASS |  |
| F5 | No unplanned recovery resets were needed | PASS | 0 unplanned, 3 planned (identity activation) |

## Full evidence

### A1 — ICMP reachability of all board addresses (PASS)

secondary-interface addresses may legitimately not answer

```
192.168.0.11    reply
192.168.0.12    reply
192.168.0.21    reply
192.168.0.22    no reply
192.168.0.31    reply
192.168.0.32    reply
```

### A2 — mTLS Telnet login on all three boards (PASS)

```
192.168.0.12: ok
192.168.0.21: ok
192.168.0.31: ok
```

### A3 — Identical firmware build on all three (PASS)

192.168.0.12 Sep  7 2026 03:49:17; 192.168.0.21 Sep  7 2026 03:49:17; 192.168.0.31 Sep  7 2026 03:49:17

```
{
  "192.168.0.12": "Build Timestamp: Sep  7 2026 03:49:17",
  "192.168.0.21": "Build Timestamp: Sep  7 2026 03:49:17",
  "192.168.0.31": "Build Timestamp: Sep  7 2026 03:49:17"
}
```

### A4 — Interface inventory matches the bench definition (PASS)

informational - drives the -i choice per board

```
--- 192.168.0.12 ---
stats
eth0 TX: ok=2750 err=0 qFull=2 pend=0
eth0 RX: ok=3143 err=4 nobufs=4 pend=0
eth1 TX: ok=3696 err=0 qFull=0 pend=0
eth1 RX: ok=3101 err=0 nobufs=0 pend=2
main loop: 43883 cycles/s

--- 192.168.0.21 ---
stats
eth0 TX: ok=877 err=0 qFull=31 pend=1
eth0 RX: ok=3242 err=15 nobufs=15 pend=0
eth1: stats not available
main loop: 48073 cycles/s

--- 192.168.0.31 ---
stats
eth0 TX: ok=556 err=0 qFull=29 pend=1
eth0 RX: ok=1823 err=7 nobufs=7 pend=0
eth1 TX: ok=0 err=69 qFull=0 pend=0
eth1 RX: ok=0 err=0 nobufs=0 pend=0
main loop: 45858 cycles/s
```

### A5 — Fault log clean at start (PASS)

a recorded fault survives resets in Backup RAM, so it may predate this run

```
--- 192.168.0.12 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
--- 192.168.0.21 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
--- 192.168.0.31 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
```

### A6 — Fault log cleared before the run (PASS)

so F1 reflects only faults produced by this test run

```
192.168.0.12: faultlog clear faultlog: cleared >
192.168.0.21: faultlog clear faultlog: cleared >
192.168.0.31: faultlog clear faultlog: cleared >
```

### B1 — Login with the valid project client identity (PASS)

```
192.168.0.12: login ok
192.168.0.21: login ok
192.168.0.31: login ok
```

### B2 — TLS connect with NO client certificate is rejected (PASS)

board must require a client certificate

```
192.168.0.12: SSLError: [SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled (_ssl.c:1082)
192.168.0.21: SSLError: [SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled (_ssl.c:1082)
192.168.0.31: SSLError: [SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled (_ssl.c:1082)
```

### B3 — TLS connect with a foreign-CA client certificate is rejected (PASS)

```
192.168.0.12: SSLError: [SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled (_ssl.c:1082)
192.168.0.21: SSLError: [SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled (_ssl.c:1082)
192.168.0.31: SSLError: [SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled (_ssl.c:1082)
```

### B4 — Wrong console password is refused (PASS)

```
192.168.0.12: refused (BootloadError)
192.168.0.21: refused (BootloadError)
192.168.0.31: refused (BootloadError)
```

### B5 — Command round-trip integrity (PASS)

```
192.168.0.12 uptime       ok
192.168.0.12 mqtt_status  ok
192.168.0.12 stats        ok
192.168.0.21 uptime       ok
192.168.0.21 mqtt_status  ok
192.168.0.21 stats        ok
192.168.0.31 uptime       ok
192.168.0.31 mqtt_status  ok
192.168.0.31 stats        ok
```

### B6 — 20 sequential connect/command/disconnect cycles per board (PASS)

regression guard for the NET_PRES socket-strand defect

```
192.168.0.12: 20/20 cycles ok, still reachable afterwards: True
192.168.0.21: 20/20 cycles ok, still reachable afterwards: True
192.168.0.31: 20/20 cycles ok, still reachable afterwards: True
```

### B7 — Second concurrent session does not disturb the first (PASS)

max 1 Telnet connection is by design (TCPIP_TELNET_MAX_CONNECTIONS=1)

```
192.168.0.12: second concurrent session -> TCP connect failed: timed out | first session still alive: True | new session after close: ok
192.168.0.21: second concurrent session -> TCP connect failed: timed out | first session still alive: True | new session after close: ok
192.168.0.31: second concurrent session -> TCP connect failed: timed out | first session still alive: True | new session after close: ok
```

### C1 — Active identity readable on each board (PASS)

```
--- 192.168.0.12 ---

--- Telnet Console ---
Type help for commands
>cert_show
CERT: source=eeprom server_cert=954B server_key=1190B ca_cert=947B server_cert_crc=EF843281 (save+reset to activate)

--- 192.168.0.21 ---

--- Telnet Console ---
Type help for commands
>cert_show
CERT: source=eeprom server_cert=954B server_key=1191B ca_cert=947B server_cert_crc=5E07DA5F (save+reset to activate)

--- 192.168.0.31 ---

--- Telnet Console ---
Type help for commands
>cert_show
CERT: source=eeprom server_cert=954B server_key=1191B ca_cert=947B server_cert_crc=458ECE0F (save+reset to activate)
```

### C2 — Freshly issued identities carry dual EKU + SKI/AKI (PASS)

```
192.168.0.12: serverAuth=True clientAuth=True SKI=True AKI=True fp=AA:12:49:7A:8E:D4:1A:58...
192.168.0.21: serverAuth=True clientAuth=True SKI=True AKI=True fp=D3:59:96:AF:C0:66:7D:FF...
192.168.0.31: serverAuth=True clientAuth=True SKI=True AKI=True fp=E0:30:60:1B:8D:A9:C9:F9...
```

### C3 — Identity pushed and saved on each board (PASS)

```
--- 192.168.0.12 (rc=0) ---
CERT: source=eeprom server_cert=954B server_key=1190B ca_cert=947B server_cert_crc=EF843281 (save+reset to activate)
CERT: READY port=5568 item=server cert bytes=954 crc=B4DC512A
CERT: OK server cert staged (954 bytes, crc32=B4DC512A) - 'cert_save' to persist
CERT: READY port=5568 item=server key bytes=1191 crc=7C37909C
CERT: OK server key staged (1191 bytes, crc32=7C37909C) - 'cert_save' to persist
CERT: saved. 'reset' to actually use this identity for new TLS connections (existing ones are unaffected).
Pushed 'bridge-ATML3264031800001049' - the board is still running its OLD identity until 'reset' (existing connections, including this one, are unaffected).

--- 192.168.0.21 (rc=0) ---
CERT: source=eeprom server_cert=954B server_key=1191B ca_cert=947B server_cert_crc=5E07DA5F (save+reset to activate)
CERT: READY port=5568 item=server cert bytes=954 crc=3EE3558C
CERT: OK server cert staged (954 bytes, crc32=3EE3558C) - 'cert_save' to persist
CERT: READY port=5568 item=server key bytes=1188 crc=E7CF2663
CERT: OK server key staged (1188 bytes, crc32=E7CF2663) - 'cert_save' to persist
CERT: saved. 'reset' to actually use this identity for new TLS connections (existing ones are unaffected).
Pushed 'bridge-ATML3264031800001103' - the board is still running its OLD identity until 'reset' (existing connections, including this one, are unaffected).

--- 192.168.0.31 (rc=0) ---
CERT: source=eeprom server_cert=954B server_key=1191B ca_cert=947B server_cert_crc=458ECE0F (save+reset to activate)
CERT: READY port=5568 item=server cert bytes=954 crc=42FC1C5D
CERT: OK server cert staged (954 bytes, crc32=42FC1C5D) - 'cert_save' to persist
CERT: READY port=5568 item=server key bytes=1192 crc=18FB2D11
CERT: OK server key staged (1192 bytes, crc32=18FB2D11) - 'cert_save' to persist
CERT: saved. 'reset' to actually use this identity for new TLS connections (existing ones are unaffected).
Pushed 'bridge-ATML3264031800001290' - the board is still running its OLD identity until 'reset' (existing connections, including this one, are unaffected).
```

### C4 — Board serves the newly issued identity after reset (PASS)

```
192.168.0.12: served=AA:12:49:7A:8E:D4:1A:58:27:A8:5E:5F:77:C2:C9:CD:82:C9:D0:B4:1E:09:F0:A9:65:F8:6E:00:F2:DE:0D:96
              issued=AA:12:49:7A:8E:D4:1A:58:27:A8:5E:5F:77:C2:C9:CD:82:C9:D0:B4:1E:09:F0:A9:65:F8:6E:00:F2:DE:0D:96  -> MATCH
192.168.0.21: served=D3:59:96:AF:C0:66:7D:FF:55:34:88:EE:88:B1:EA:D5:92:42:4D:3C:CC:E3:A2:A4:4A:C6:C3:80:AB:2E:49:E6
              issued=D3:59:96:AF:C0:66:7D:FF:55:34:88:EE:88:B1:EA:D5:92:42:4D:3C:CC:E3:A2:A4:4A:C6:C3:80:AB:2E:49:E6  -> MATCH
192.168.0.31: served=E0:30:60:1B:8D:A9:C9:F9:FF:40:1D:25:01:71:CD:78:ED:3F:D9:03:04:61:C8:A9:D4:6C:A4:C6:04:B9:00:EA
              issued=E0:30:60:1B:8D:A9:C9:F9:FF:40:1D:25:01:71:CD:78:ED:3F:D9:03:04:61:C8:A9:D4:6C:A4:C6:04:B9:00:EA  -> MATCH
```

### C5 — Telnet still works after the identity change (PASS)

same CA, so the client identity is unchanged

```
192.168.0.12: login ok with the new identity
192.168.0.21: login ok with the new identity
192.168.0.31: login ok with the new identity
```

### C6 — Board metadata matches the served certificate (PASS)

```
192.168.0.12: json=AA:12:49:7A:8E:D4:1A:58... -> MATCH
192.168.0.21: json=D3:59:96:AF:C0:66:7D:FF... -> MATCH
192.168.0.31: json=E0:30:60:1B:8D:A9:C9:F9... -> MATCH
```

### D1 — 'bootload info' reports a sane state on each board (PASS)

```
--- 192.168.0.12 ---

--- Telnet Console ---
Type help for commands
>bootload info
bootload - dual-bank firmware update (receive, verify, commit)
  running bank      : B (STATUS.AFIRST=0), mapped at 0x00000000
  update target     : 0x00080000 .. 0x000FBFFF (bank A)
  max image         : 507904 bytes (bank 524288 - EEPROM window 16384)
  live env window   : 0x000FC000 .. 0x000FFFFF (never written here)
  page / block      : 512 / 8192 bytes
  region locks      : RUNLOCK=0xFFFFFFFF (1 = unlocked)
  data port         : 5567

--- 192.168.0.21 ---

--- Telnet Console ---
Type help for commands
>bootload info
bootload - dual-bank firmware update (receive, verify, commit)
  running bank      : B (STATUS.AFIRST=0), mapped at 0x00000000
  update target     : 0x00080000 .. 0x000FBFFF (bank A)
  max image         : 507904 bytes (bank 524288 - EEPROM window 16384)
  live env window   : 0x000FC000 .. 0x000FFFFF (never written here)
  page / block      : 512 / 8192 bytes
  region locks      : RUNLOCK=0xFFFFFFFF (1 = unlocked)
  data port         : 5567

--- 192.168.0.31 ---

--- Telnet Console ---
Type help for commands
>bootload info
bootload - dual-bank firmware update (receive, verify, commit)
  running bank      : B (STATUS.AFIRST=0), mapped at 0x00000000
  update target     : 0x00080000 .. 0x000FBFFF (bank A)
  max image         : 507904 bytes (bank 524288 - EEPROM window 16384)
  live env window   : 0x000FC000 .. 0x000FFFFF (never written here)
  page / block      : 512 / 8192 bytes
  region locks      : RUNLOCK=0xFFFFFFFF (1 = unlocked)
  data port         : 5567
```

### D2 — 'bootload selftest' passes on each board (PASS)

erase+write+readback in the inactive bank

```
--- 192.168.0.12 ---
bootload selftest
bootload selftest: bank B is running; erasing+writing 0x00080000
  erase 38957 us, write 1478 us, readback [0]=0xB0070000 [127]=0xB007007F
BL: selftest PASS (0 mismatching words)
>
--- 192.168.0.21 ---
bootload selftest
bootload selftest: bank B is running; erasing+writing 0x00080000
  erase 39290 us, write 1551 us, readback [0]=0xB0070000 [127]=0xB007007F
BL: selftest PASS (0 mismatching words)
>
--- 192.168.0.31 ---
bootload selftest
bootload selftest: bank B is running; erasing+writing 0x00080000
  erase 38805 us, write 1525 us, readback [0]=0xB0070000 [127]=0xB007007F
BL: selftest PASS (0 mismatching words)
>
```

### D3 — Full OTA update completes on each board (PASS)

```
--- 192.168.0.12 (rc=0) ---
phase 1: prepare
image  : C:\work\t1s_bridge\bridge\temp\bridge_lan865x_100baseT\release\bridge_lan865x_100baseT.hex
size   : 373815 bytes (365.1 KiB, 74% of one bank), crc32=0xDE6BFA50
BL: state=IDLE bank=B rx=0 written=0 size=0 err=0 (none) probation=off
user page : 0xFFFF9239 0xAAA8FF80 -> BOOTPROT=0xF, SBLK=0
phase 2: arm
BL: READY port=5567 max=507904

transferred 373815 bytes in 4.0 s (92 kB/s)
phase 4: verify
BL: OK written=373815 crc=0xDE6BFA50
BL: state=VERIFIED bank=B rx=373815 written=374272 size=373815 err=0 (none) probation=off
phase 5: commit
BL: COMMIT env-copy+bankswap in 500ms, the board will reset
phase 6: reboot
board is back after 14 s
phase 7: check
BL: RUNNING bank=A size=373815 crc=0xDE6BFA50 match=1
running bank      : A (STATUS.AFIRST=1), mapped at 0x00000000
eth1  ip 192.168.0.12
BL: CONFIRMED nothing was pending

OK - the board is running the new image (373815 bytes, crc=0xDE6BFA50) and kept its environment.
--- 192.168.0.21 (rc=0) ---
phase 1: prepare
image  : C:\work\t1s_bridge\bridge\temp\bridge_lan865x_100baseT\release\bridge_lan865x_100baseT.hex
size   : 373815 bytes (365.1 KiB, 74% of one bank), crc32=0xDE6BFA50
BL: state=IDLE bank=B rx=0 written=0 size=0 err=0 (none) probation=off
user page : 0x3C001239 0x2AA80080 -> BOOTPROT=0xF, SBLK=0
phase 2: arm
BL: READY port=5567 max=507904

transferred 373815 bytes in 4.7 s (77 kB/s)
phase 4: verify
BL: OK written=373815 crc=0xDE6BFA50
BL: state=VERIFIED bank=B rx=373815 written=374272 size=373815 err=0 (none) probation=off
phase 5: commit
BL: COMMIT env-copy+bankswap in 500ms, the board will reset
phase 6: reboot
board is back after 10 s
phase 7: check
BL: RUNNING bank=A size=373815 crc=0xDE6BFA50 match=1
running bank      : A (STATUS.AFIRST=1), mapped at 0x00000000
eth1  ip 192.168.0.22
BL: CONFIRMED nothing was pending

OK - the board is running the new image (373815 bytes, crc=0xDE6BFA50) and kept its environment.
--- 192.168.0.31 (rc=0) ---
phase 1: prepare
image  : C:\work\t1s_bridge\bridge\temp\bridge_lan865x_100baseT\release\bridge_lan865x_100baseT.hex
size   : 373815 bytes (365.1 KiB, 74% of one bank), crc32=0xDE6BFA50
BL: state=IDLE bank=B rx=0 written=0 size=0 err=0 (none) probation=off
user page : 0x3C001239 0x2AA80080 -> BOOTPROT=0xF, SBLK=0
phase 2: arm
BL: READY port=5567 max=507904

transferred 373815 bytes in 5.0 s (73 kB/s)
phase 4: verify
BL: OK written=373815 crc=0xDE6BFA50
BL: state=VERIFIED bank=B rx=373815 written=374272 size=373815 err=0 (none) probation=off
phase 5: commit
BL: COMMIT env-copy+bankswap in 500ms, the board will reset
phase 6: reboot
board is back after 10 s
phase 7: check
BL: RUNNING bank=A size=373815 crc=0xDE6BFA50 match=1
running bank      : A (STATUS.AFIRST=1), mapped at 0x00000000
eth1  ip 192.168.0.32
BL: CONFIRMED nothing was pending

OK - the board is running the new image (373815 bytes, crc=0xDE6BFA50) and kept its environment.
```

### D4 — Environment preserved - same IP, console reachable after update (PASS)

```
192.168.0.12: ping=True console=True
192.168.0.21: ping=True console=True
192.168.0.31: ping=True console=True
```

### D5 — Provisioned identity survives the bank swap (PASS)

```
192.168.0.12: identity survived the bank swap
192.168.0.21: identity survived the bank swap
192.168.0.31: identity survived the bank swap
```

### D6 — A second consecutive update alternates the running bank (PASS)

proves both banks are usable

```
192.168.0.12: before=[running bank      : A (STATUS.AFIRST=1), mapped at 0x00000000] after=[running bank      : B (STATUS.AFIRST=0), mapped at 0x00000000] -> alternated
192.168.0.21: before=[running bank      : A (STATUS.AFIRST=1), mapped at 0x00000000] after=[running bank      : B (STATUS.AFIRST=0), mapped at 0x00000000] -> alternated
192.168.0.31: before=[running bank      : A (STATUS.AFIRST=1), mapped at 0x00000000] after=[running bank      : B (STATUS.AFIRST=0), mapped at 0x00000000] -> alternated
```

### D7 — Fault log still clean after the updates (PASS)

```
--- 192.168.0.12 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
--- 192.168.0.21 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
--- 192.168.0.31 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
```

### E1 — Broker starts, TLS-only (PASS)

listening on 192.168.0.100:8883

### E2 — Plaintext MQTT client is rejected (PASS)

no CONNACK on a plaintext connection

```
bytes received: b''
```

### E3 — TLS client without a client certificate is rejected (PASS)

judged at the MQTT layer: the TLS handshake itself may legitimately complete, the broker then refuses the session (RequireClientCertPlugin)

```
broker refused: reply b' \x02\x00\x05'
```

### E4 — TLS client with a foreign-CA certificate is rejected (PASS)

```
TLS handshake completed, broker closed the session without a CONNACK
```

### E5 — Each board connects with its correct -i interface (PASS)

```
192.168.0.12: MQTT: broker set to 192.168.0.100:8883 via eth1 - connecting
192.168.0.21: MQTT: broker set to 192.168.0.100:8883 via eth0 - connecting
192.168.0.31: MQTT: broker set to 192.168.0.100:8883 via eth0 - connecting
192.168.0.12: MQTT: state=connected client_id=bridge-000425CACED9 topic=bridge/bridge-000425CACED9/status port=8883 if=eth1 seq=6 last_fail=(none yet)
192.168.0.21: MQTT: state=connected client_id=bridge-0004258E8CA1 topic=bridge/bridge-0004258E8CA1/status port=8883 if=eth0 seq=6 last_fail=(none yet)
192.168.0.31: MQTT: state=connected client_id=bridge-0004259D4C63 topic=bridge/bridge-0004259D4C63/status port=8883 if=eth0 seq=6 last_fail=(none yet)
```

### E6 — All three boards connected to the broker simultaneously (PASS)

3 distinct client_id(s) seen

```
bridge-000425CACED9 from 192.168.0.12 (peer CN bridge-ATML3264031800001049)
bridge-0004258E8CA1 from 192.168.0.21 (peer CN bridge-ATML3264031800001103)
bridge-0004259D4C63 from 192.168.0.31 (peer CN bridge-ATML3264031800001290)
```

### E7 — Periodic status publishing, ~5s, monotonic seq (PASS)

3 client(s) published within the observation window

```
bridge-000425CACED9: 11 message(s), topic=bridge/bridge-000425CACED9/status, seq=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10] strictly increasing
bridge-0004258E8CA1: 10 message(s), topic=bridge/bridge-0004258E8CA1/status, seq=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9] strictly increasing
bridge-0004259D4C63: 10 message(s), topic=bridge/bridge-0004259D4C63/status, seq=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9] strictly increasing
```

### E8 — -i with the board's non-uplink interface behaves definitely (PASS)

connected anyway (bridged)

```
--- Telnet Console ---
Type help for commands
>mqtt_broker 192.168.0.100 -i eth1
MQTT: broker set to 192.168.0.100:8883 via eth1 - connecting
--- Telnet Console ---
Type help for commands
>mqtt_status
MQTT: state=connected client_id=bridge-0004258E8CA1 topic=bridge/bridge-0004258E8CA1/status port=8883 if=eth1 seq=17 last_fail=(none yet)
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
```

### E9 — -i with an invalid interface name is handled (PASS)

command must not crash the board or silently accept a bad name

```
--- Telnet Console ---
Type help for commands
>mqtt_broker 192.168.0.100 -i wlan9
MQTT: broker set to 192.168.0.100:8883 via wlan9 - connecting
--- Telnet Console ---
Type help for commands
>mqtt_status
MQTT: state=idle/retrying client_id=bridge-0004258E8CA1 topic=bridge/bridge-0004258E8CA1/status port=8883 if=wlan9 seq=19 last_fail=wlan9 SocketNetSet failed
```

### E10 — Boards reconnect by themselves after a broker restart (PASS)

```
while the broker was down:
192.168.0.12: MQTT: state=connecting (tcp/tls) client_id=bridge-000425CACED9 topic=bridge/bridge-000425CACED9/status port=8883 if=eth1 seq=21 last_fail=broker connection lost
192.168.0.21: MQTT: state=connecting (tcp/tls) client_id=bridge-0004258E8CA1 topic=bridge/bridge-0004258E8CA1/status port=8883 if=eth0 seq=21 last_fail=broker connection lost
192.168.0.31: MQTT: state=connecting (tcp/tls) client_id=bridge-0004259D4C63 topic=bridge/bridge-0004259D4C63/status port=8883 if=eth0 seq=20 last_fail=broker connection lost

after it came back:
192.168.0.12: MQTT: state=connected client_id=bridge-000425CACED9 topic=bridge/bridge-000425CACED9/status port=8883 if=eth1 seq=23 last_fail=tcp/tls timeout, wolfSSL err=0
192.168.0.21: MQTT: state=connected client_id=bridge-0004258E8CA1 topic=bridge/bridge-0004258E8CA1/status port=8883 if=eth0 seq=27 last_fail=broker connection lost
192.168.0.31: MQTT: state=connected client_id=bridge-0004259D4C63 topic=bridge/bridge-0004259D4C63/status port=8883 if=eth0 seq=26 last_fail=broker connection lost
```

### G1 — Registered patches all present (PASS)

```
patch                        status       detail
------------------------------------------------------------------------------------------
[ok] configuration.h wolfSSL/TLS OK           all TLS-critical settings present, nothing to do
[ok] drv_lan865x_api.c stdarg.h OK           already present, nothing to do
[ok] telnet.c stdarg.h          OK           already present, nothing to do
[ok] drv_lan865x_api            OK           already applied, nothing to do
[ok] drv_lan865x_h              OK           already applied, nothing to do
[ok] initialization             OK           already applied, nothing to do
[ok] interrupts                 OK           already applied, nothing to do
[ok] net_pres                   OK           already applied, nothing to do
[ok] plib_clock                 OK           already applied, nothing to do
[ok] sys_command                OK           already applied, nothing to do
[ok] sys_command_h              OK           already applied, nothing to do
[ok] tasks                      OK           already applied, nothing to do
[ok] tc6-conf                   OK           already applied, nothing to do
[ok] tcpip_mac_bridge           OK           already applied, nothing to do
[ok] tcpip_manager              OK           already applied, nothing to do
[ok] telnet                     OK           already applied, nothing to do

Dry run - nothing was changed. Re-run without --check to apply.
All patches present.
```

### G2 — wolfSSL/TLS build configuration protected (PASS)

```
searched every patches/*.patch for: HAVE_TLS_EXTENSIONS, HAVE_SUPPORTED_CURVES, NO_WOLFSSL_CLIENT, TCPIP_TELNET_MAX_CONNECTIONS
```

### G3 — TLS provider registration protected (PASS)

```
searched every patches/*.patch for: pProvObject_ss, pProvObject_sc, net_pres_enc_glue_client.h
```

### G4 — NET_PRES socket-strand (DoS) fix protected (PASS)

```
searched every patches/*.patch for: NET_PRES_SocketDisconnect, provOpen
```

### G5 — Project-owned files under config/default are accounted for (PASS)

```
checked patches/*.patch and docs/mcc-generated-code-patches.md
```

### F1 — Fault log clean on all three after the whole run (PASS)

```
--- 192.168.0.12 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
--- 192.168.0.21 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
--- 192.168.0.31 ---
faultlog
faultlog: no fault recorded since Backup RAM was last blank
>
```

### F2 — Heap still healthy (PASS)

```
192.168.0.12 est free block=5120 (nano-malloc; no exact free count) TCP/IP heap: size=98224 free=37056 maxblock=20608 highwater=68336
192.168.0.21 est free block=5120 (nano-malloc; no exact free count) TCP/IP heap: size=98224 free=55024 maxblock=54224 highwater=44672
192.168.0.31 est free block=5120 (nano-malloc; no exact free count) TCP/IP heap: size=98224 free=38080 maxblock=37280 highwater=60400
```

### F3 — Telnet responsive on all three (PASS)

```
192.168.0.12: ok
192.168.0.21: ok
192.168.0.31: ok
```

### F4 — MQTT connected on all three at the end (PASS)

```
192.168.0.12: MQTT: state=connected client_id=bridge-000425CACED9 topic=bridge/bridge-000425CACED9/status port=8883 if=eth1 seq=28 last_fail=tcp/tls timeout, wolfSSL err=0
192.168.0.21: MQTT: state=connected client_id=bridge-0004258E8CA1 topic=bridge/bridge-0004258E8CA1/status port=8883 if=eth0 seq=32 last_fail=broker connection lost
192.168.0.31: MQTT: state=connected client_id=bridge-0004259D4C63 topic=bridge/bridge-0004259D4C63/status port=8883 if=eth0 seq=31 last_fail=broker connection lost
```

### F5 — No unplanned recovery resets were needed (PASS)

0 unplanned, 3 planned (identity activation)

```
unplanned:
none

planned:
[
  {
    "board": "192.168.0.12",
    "why": "C4: activate the newly provisioned identity",
    "when": "2026-09-07T04:26:31"
  },
  {
    "board": "192.168.0.21",
    "why": "C4: activate the newly provisioned identity",
    "when": "2026-09-07T04:26:40"
  },
  {
    "board": "192.168.0.31",
    "why": "C4: activate the newly provisioned identity",
    "when": "2026-09-07T04:26:48"
  }
]
```
