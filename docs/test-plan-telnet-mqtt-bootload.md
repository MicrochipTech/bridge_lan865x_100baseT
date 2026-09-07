# Test Plan — Telnet, MQTT, Bootload and Identity Provisioning

Full-bench functional test plan covering the four features that share this
project's mutual-TLS plumbing, exercised across **all three bench boards**.

Written 2026-09-07. Results of each run go into their own dated document
(`test-results-<date>.md`), never into this file — this file is the plan, the
results file is the evidence.

---

## 1. Bench under test

| Board | Console IP | Probe serial | Role | Interfaces (from `stats`) | MQTT uplink |
|---|---|---|---|---|---|
| `bridge-ATML3264031800001049` | `192.168.0.12` | `ATML3264031800001049` | Leader | eth0 (T1S) **and** eth1 (100BASE-TX) both live; eth0 also answers on `.11` | `-i eth1` |
| `bridge-ATML3264031800001103` | `192.168.0.21` | `ATML3264031800001103` | Follower | eth0 only — `eth1: stats not available` (no 100BASE-TX PHY populated) | `-i eth0` |
| `bridge-ATML3264031800001290` | `192.168.0.31` | `ATML3264031800001290` | Follower | eth0 live; eth1 present but link dead (`TX ok=0 err=n`) | `-i eth0` |

PC / broker host: `192.168.0.100` (NIC "Ethernet 8"), MQTT broker port `8883`.

**Why the uplink column matters:** `mqtt_broker` pins the outbound socket to a
named interface. The correct interface is a per-board wiring fact, not something
the firmware can infer — see `session-log.md`, 2026-09-07. A test that uses the
wrong one is testing the failure path, not the feature.

## 2. Rules for the run

1. **One console session at a time, bench-wide.** `TCPIP_TELNET_MAX_CONNECTIONS`
   is `1` per board; a second connection to the *same* board is expected to fail.
   The runner must open, use and close a session before opening the next.
2. **Firmware updates go through `bootload`** (network OTA), not pyOCD/SWD.
   pyOCD is permitted only as a *recovery reset* for a board whose TCP stack has
   stopped answering, and that recovery must itself be recorded as a test
   observation, not silently swallowed.
3. Every test records its raw evidence (captured console/tool output), not just a
   verdict.
4. A failing test does not abort the run — later groups still execute, so one run
   yields a complete picture.

## 3. Test cases

### Group A — Baseline

| ID | Test | Pass criteria |
|---|---|---|
| A1 | ICMP reachability of every board IP incl. secondary interface addresses (`.11`, `.12`, `.21`, `.22`, `.31`, `.32`) | Each address either replies, or its non-reply is explained by the interface inventory in §1 |
| A2 | mTLS Telnet login on all three boards | All three accept the project client identity and reach the command prompt |
| A3 | Firmware build identity | `timestamp` reports the **same** build string on all three |
| A4 | Interface inventory | `stats` output matches §1; no unexpected error counters on the interface each board actually uses |
| A5 | Fault log clean at start | `faultlog` reports no recorded fault on all three |

### Group B — Telnet and mTLS enforcement

| ID | Test | Pass criteria |
|---|---|---|
| B1 | Login with the valid project client identity | Succeeds on all three |
| B2 | TLS connect with **no** client certificate | Rejected by the board (handshake fails); board stays reachable afterwards |
| B3 | TLS connect with a client certificate signed by a **foreign** CA | Rejected; board stays reachable afterwards |
| B4 | Valid certificate but wrong console password | Login refused, connection not granted a command prompt |
| B5 | Command round-trip integrity | `uptime`, `mqtt_status`, `stats` each return their expected marker line, uncorrupted |
| B6 | 20 sequential connect → command → disconnect cycles per board | All 20 succeed; board still answers a 21st session (regression guard for the socket-strand defect fixed in `net_pres.c`) |
| B7 | Second concurrent session while one is open | Documented, deterministic behaviour (refusal/timeout) — **not** a wedged board: the first session keeps working and a new session succeeds after the first closes |

### Group C — Identity provisioning

| ID | Test | Pass criteria |
|---|---|---|
| C1 | Read active identity on each board (`cert_provision.py --show`) | Reports source, sizes and CRC for all three |
| C2 | Issue a fresh identity per board (`pki.py issue-board --force`) | Certificate carries both `serverAuth` **and** `clientAuth` EKU, plus SubjectKeyIdentifier and AuthorityKeyIdentifier |
| C3 | Push identity + `cert_save` to each board | Transfer reports staged + saved with matching CRC |
| C4 | Reset and verify activation | After reset the board presents the **newly issued** fingerprint on its TLS port |
| C5 | Telnet still works with the new identity | Login succeeds with the unchanged client identity (same CA) |
| C6 | Board metadata consistency | `json/boards/<id>.json` fingerprint equals the certificate actually served by the board |

### Group D — Bootload (network OTA)

| ID | Test | Pass criteria |
|---|---|---|
| D1 | `bootload info` on each board | Reports running bank, target window, max image, data port; values self-consistent |
| D2 | `bootload selftest` on each board | `PASS (0 mismatching words)` |
| D3 | Full OTA update on each board with the current release image | Reaches phase 7 with `match=1` and `BL: CONFIRMED` |
| D4 | Environment preserved across the update | Board returns on the **same IP**; console reachable without re-provisioning |
| D5 | Identity preserved across the update | Fingerprint served after the update equals the one from C4 (the EEPROM identity survives a bank swap) |
| D6 | Bank alternation | A second consecutive update swaps the running bank back (A→B→A), proving both banks are usable |
| D7 | Fault log clean after updates | `faultlog` still reports no fault on all three |

### Group E — MQTT over mutual TLS

| ID | Test | Pass criteria |
|---|---|---|
| E1 | Broker starts TLS-only | Listener up on `192.168.0.100:8883`; no plaintext listener exists |
| E2 | Plaintext MQTT client against the broker | Rejected (no plaintext path) |
| E3 | TLS client with **no** client certificate | Rejected by `RequireClientCertPlugin` |
| E4 | TLS client with a **foreign-CA** certificate | Rejected |
| E5 | Each board connects with its correct `-i` interface | `mqtt_status` reaches `state=connected` on all three |
| E6 | All three connected simultaneously | Broker reports three distinct `client_id`s connected at the same time |
| E7 | Periodic publishing | Each board's `bridge/<client_id>/status` JSON arrives about every 5 s, `seq` strictly increasing, `uptime_s` plausible |
| E8 | `-i` with the board's dead/absent interface | Clean, reported failure (`last_fail` set, retry armed) — no crash, no fault log entry |
| E9 | `-i` with an invalid interface name | Command rejects it with an error message; no state change, no crash |
| E10 | Broker stop → restart | Boards report the loss and reconnect automatically once the broker is back |

### Group G — Survivability of the hand-patches to MCC-generated code

Everything under `firmware\src\config\default\` is MCC-generated and is reverted
by the next *Generate Code* run unless it is registered in `patches\`
(`development-notes.md` §1, and `session-log.md` 2026-09-06: "Every Generate Code
silently reverted prior hand-patches", confirmed twice). The whole mutual-TLS
feature set — Telnet, bootload, cert provisioning *and* the MQTT client — depends
on hand-edits to exactly those files, so this group tests that the safety net
actually covers them.

| ID | Test | Pass criteria |
|---|---|---|
| G1 | `patches/apply_patches.py --check` | Reports all registered patches present |
| G2 | wolfSSL/TLS build configuration is protected | The `configuration.h` edits the TLS features depend on (`WOLFCRYPT_ONLY` removed, `NO_WOLFSSL_CLIENT` removed, `HAVE_TLS_EXTENSIONS`, `HAVE_SUPPORTED_CURVES`, `NO_ASN_TIME`, `TCPIP_TELNET_MAX_CONNECTIONS`) are covered by a registered patch |
| G3 | TLS provider registration is protected | `initialization.c`'s `pProvObject_ss` **and** `pProvObject_sc` assignments plus the client-glue include are covered by a registered patch |
| G4 | The NET_PRES socket-strand fix is protected | `net_pres.c`'s `NET_PRES_SocketDisconnect()` fix (the 2026-09-06 DoS root cause) is covered by a registered patch |
| G5 | Project-specific files under `config\default\` are accounted for | `net_pres_enc_glue.c`, `net_pres_enc_glue_client.c/.h` are either covered by a patch or explicitly documented as project-owned files MCC does not generate |

### Group F — Final regression sweep

| ID | Test | Pass criteria |
|---|---|---|
| F1 | Fault log clean on all three after the whole run | No recorded fault |
| F2 | Memory | `meminfo` shows the TCP/IP heap not exhausted, comparable to the start of the run |
| F3 | Telnet responsive on all three | Fresh session succeeds |
| F4 | MQTT still connected on all three | `state=connected`, `seq` higher than at E7 |
| F5 | No pyOCD recovery resets were needed | Any recovery reset performed during the run is listed explicitly as a defect observation |

## 4. Verdicts

- **PASS** — pass criteria met, evidence captured.
- **FAIL** — criteria not met. Must be analysed and fixed, then the affected group
  re-run.
- **SKIP** — not executed (with the reason recorded); never used to hide a failure.

## 5. Execution

`scripts/testplan_runner.py` implements the automatable cases and writes the
results document. It is deliberately re-runnable: every group can be executed on
its own (`--groups A,B`) so a fix can be verified without repeating the whole
bench run.

## 6. Diagnosing a FAIL

A verdict alone is not a result — every FAIL is root-caused before anything is
changed. Escalate in this order, cheapest and least invasive first:

1. **The board's own instrumentation.** `faultlog` (a recorded HardFault/BusFault
   with PC/LR, resolvable offline via `xc32-addr2line -e <production.elf> <pc>`),
   `stats`, `meminfo`, `mqtt_status`'s `last_fail`, `bootload info`.
2. **The wire.** `tshark` on the PC NIC, and the broker's own debug log
   (`logging.basicConfig(DEBUG)`) — TLS handshake messages are unencrypted up to
   `ChangeCipherSpec`, so a rejection's exact stage is visible without any
   decryption (which is impossible here anyway: the suites are ECDHE, so forward
   secrecy rules out after-the-fact decryption even with every private key).
3. **pyOCD live debugging** when 1 and 2 cannot answer *why* — see
   [`pyocd-agent-debugging-guide.md`](pyocd-agent-debugging-guide.md):
   - core register / memory inspection via `pyocd commander` against the real
     `.production.elf`,
   - breakpoints on suspected functions via the pyOCD Python API to prove
     whether a code path is reached at all (**halt before arming a breakpoint**,
     or it silently never fires),
   - watching a whole call chain at once to find which link is not reached.

   Mind the safety rule: halting the core stops the entire firmware including its
   network stack, so a halted board goes silent on the network until resumed —
   never leave a session halted, and never halt a board another test still needs.
4. Only once the mechanism is understood: fix, then re-run the affected group
   (§5) plus Group F, to confirm the fix and catch collateral damage.
