# TLS Proof of Concept: Mutual-TLS Telnet + Bootload (branch `t1s-t1s-bridge-lan8670`)

Status: **experimental, not committed, not merged.** Everything below was built and
tested in one working session, live against real bench hardware, but none of it
has had a security review, and several open items (listed at the end) block it
from being anything more than a feasibility demonstration.

## 1. Origin and scope

The starting question was unrelated to security: could the LAN865x's eth1
(currently LAN8742A/LAN8740A, 100BASE-TX) be swapped for a LAN8670 (10BASE-T1S)
driver, turning this board into a T1S↔T1S bridge? That swap was built, made to
compile and link, then **reverted** at the user's request — eth1 is back to
LAN8742A/LAN8740A in the current tree. It is mentioned here only because the
wolfSSL/TLS work that follows was scoped and sized while that experiment was
still in the tree, and the resource numbers below were re-measured after the
revert.

The actual scope that stuck: this board's Telnet console (admin/password over
plaintext TCP/23) and its network firmware-update channel (`bootload.c`, a
bespoke protocol on TCP/5567) are both unauthenticated and unencrypted on the
wire. Full SSH was ruled out early as too heavy for this budget (a fair
assessment — see §4). TLS, scoped to exactly these two services and to one
active session at a time, was the alternative explored here.

## 2. What was built

### 2.1 wolfSSL TLS layer (vendored, not previously present)

The project already linked wolfCrypt (RSA/ECC/AES/SHA primitives, ~100 KB)
via `WOLFCRYPT_ONLY`, but nothing used it — no TLS protocol code was even
present in the source tree, only `wolfcrypt/`. Pulled in from the same
`net_10base_t1s`-pinned wolfSSL v5.4.0 package already cached locally:

- `ssl.c`, `internal.c`, `tls.c`, `keys.c`, `wolfio.c` (compiled), plus
  `pk.c`/`bio.c`/`x509.c`/`conf.c`/`x509_str.c` (pulled in via `#include`
  from `ssl.c`, not compiled separately).
- Full `wolfssl/` public header tree (143 headers; started with 77 already
  vendored for wolfCrypt-only use).
- Three harmless `-Waddress` warnings in unmodified wolfSSL code silenced via
  `#pragma GCC diagnostic ignored` at the top of the three top-level `.c`
  files (this project bakes `-Wall -Werror` into every compile recipe with no
  found per-file override) — not a code change to wolfSSL's logic.

Configuration (`configuration.h`): removed `WOLFCRYPT_ONLY`; added
`NO_WOLFSSL_CLIENT` (server-only), `NO_OLD_TLS` (TLS ≥1.2 only, and no
`tls13.c` vendored so effectively TLS 1.2 only), `NO_SESSION_CACHE` (one
session at a time), and `NO_ASN_TIME` (§3.2).

### 2.2 NET_PRES encryption provider (`net_pres_enc_glue.c`)

Harmony's Networking Presentation Layer already defines a clean, documented
provider interface (`net_pres_encryptionproviderapi.h`) — a vtable of
init/open/connect/read/write/close/etc. function pointers — that existing
app code (Telnet's `telnet.c`, this project's `bootload.c`) can sit behind
without knowing whether the socket underneath is encrypted. This was
previously registered as `NULL` (`initialization.c`'s
`netPresCfgs[0].pProvObject_ss`).

Implemented a full wolfSSL-backed provider: one shared `WOLFSSL_CTX`, one
`WOLFSSL*` + a small heap struct per connection (8 bytes handed back through
`providerData`), custom I/O callbacks (`WOLFSSL_USER_IO`) bridging to
whatever transport NET_PRES is layered on (TCP, via the transport object
passed at `fpInit`). Wired into `netPresCfgs[0].pProvObject_ss`.

**Mutual TLS**, not just server-side encryption: `wolfSSL_CTX_set_verify(ctx,
WOLFSSL_VERIFY_PEER | WOLFSSL_VERIFY_FAIL_IF_NO_PEER_CERT, NULL)` plus loading
a CA to verify against. A completed TLS handshake alone only proves *a* TLS
client connected — this makes the server reject the handshake outright unless
the client presents a certificate chaining to the project's own CA.

### 2.3 Telnet and bootload switched to the encrypted socket type

- `telnet.c` (MCC-generated, hand-patched — not yet in `patches/`):
  `NET_PRES_SKT_DEFAULT_STREAM_SERVER` → `NET_PRES_SKT_ENCRYPTED_STREAM_SERVER`.
  `TCPIP_TELNET_MAX_CONNECTIONS` 2 → 1 (see §4 for the RAM reasoning, later
  found to be *necessary* for other reasons too — see §6).
- `bootload.c`: a bigger change, since it used raw `TCPIP_TCP_*` calls
  throughout, not NET_PRES. Converted `s_sock` to `NET_PRES_SKT_HANDLE_T` and
  every `TCPIP_TCP_*` call to its `NET_PRES_Socket*` equivalent
  (`ServerOpen→SocketOpen` with the encrypted type, `Close`, `ArrayPut→Write`,
  `Flush`, `GetIsReady→ReadIsReady`, `ArrayGet→Read`, `WasReset`,
  `WasDisconnected`).
  - **Bug found and fixed during the conversion**: `BL_WAIT_CONN` originally
    checked only `IsConnected()`, which for an encrypted socket is TCP-level
    only — true well before (or even if) the TLS handshake and mTLS
    certificate check finish. Without `NET_PRES_SocketIsSecure()` added to the
    guard, the state machine would have parsed a client's TLS ClientHello as
    this protocol's magic/size/crc header and failed every connection
    attempt with `BL_E_HEADER`.

### 2.4 Certificates — two real bugs found and fixed before anything worked

Initial bring-up used wolfSSL's own built-in test PKI
(`wolfssl/certs_test.h`, `USE_CERT_BUFFERS_2048`). Verifying it with
`openssl verify` found:

1. **All of it was expired** — every test cert dated 2022, valid until
   November 2024.
2. **The "matching" client/CA pair does not chain at all.**
   `client_cert_der_2048` is an unrelated, self-signed certificate under a
   different identity (`O=wolfSSL_2048`); `ca_cert_der_2048` only actually
   signs `server_cert_der_2048` (`O=Sawtooth, OU=Consulting`). The intended
   test client would never have passed the server's own mTLS check.

**Fix:** generated a fresh, correctly-chained CA/server/client trio with
OpenSSL (RSA-2048, valid 2026-09-05 through 2046-08-31, chain verified).
Embedded the CA/server cert/server key as DER byte arrays in
`firmware/src/bridge_certs.h`; the client cert/key ship as `.pem` files for
the Python tooling (§5).

**The CA private key (`certs/ca/ca_key.pem`) lives in this repo.** Fine
for this experiment, disqualifying for anything real — see §6. It was moved
into its own `certs/ca/` directory (2026-09-05), separate from every leaf
identity (`certs/client/`'s shared operator identity, `certs/bridge/`'s
original compiled-in default server identity, `certs/boards/<id>/`'s
per-board ones) precisely because it is the one file that would need to move
offline for anything beyond a PoC - keeping it physically apart from every
leaf identity makes that eventual move a directory move, not a hunt through
mixed files.

### 2.5 No RTC — `NO_ASN_TIME`

Even with fresh, valid certificates, a second problem surfaced: this board
has no real-time clock and no NTP/SNTP client. Confirmed no
`_gettimeofday`/`_times` syscall in `libc_syscalls.c` and no active RTC
peripheral (`RTC_Handler` in the link is only the unused weak startup-code
stub). `time()` still resolves (XC32's libc default is linked in) but returns
nothing resembling the real date. Every certificate's `notBefore` would
appear to be in the future relative to that, and wolfSSL would reject *any*
certificate — including a perfectly valid, freshly issued one — as "not yet
valid".

**Fix:** `#define NO_ASN_TIME`, skipping date validation entirely. Documented
in `configuration.h` as a stand-in for a real time source (RTC seeded from
build time, or SNTP once the stack is up) that a real deployment would need
instead.

## 3. Resource cost — measured, not estimated

### 3.1 Flash

| Build | Used | % of 496 KiB |
|---|---|---|
| Before TLS (wolfCrypt-only, unused) | 219 523 B | 43.2 % |
| TLS linked in for real (provider registered) | 335 075 B | 66.0 % |
| **Delta** | **+115 552 B (~113 KiB)** | |

The jump only appeared once the provider object was actually referenced from
a reachable global (`netPresCfgs[0].pProvObject_ss`) — before that, the fully
compiled TLS code was dead-stripped by `--gc-sections` and cost nothing. The
real number (+113 KB) is notably higher than an early rough estimate
(+30–70 KB): the full dependency set (`pk.c`/`bio.c`/`x509.c`/`conf.c`/
`x509_str.c`/`keys.c`/`wolfio.c`, not just `ssl.c`/`internal.c`/`tls.c`)
only became visible once the linker actually needed all of it.

After reverting the unrelated LAN867x/eth1 experiment, the build with the
final TLS+mTLS state sits at:

| | Used | Free | Total |
|---|---|---|---|
| Flash | 328 120 B (320.4 KiB) | 179 784 B (175.6 KiB) | 507 904 B, 64.6 % |
| RAM, static .data+.bss | 55 235 B (53.9 KiB) | 206 909 B (202.1 KiB) | 262 144 B, 21.1 % |

### 3.2 RAM / heap

Static `sizeof()` probing (a throwaway build-time probe, removed after
reading it via `xc32-nm -S`) measured the *fixed* per-object cost:

| Struct | Size | Allocated |
|---|---|---|
| `WOLFSSL_CTX` | 256 B | once, shared |
| `WOLFSSL` | 636 B | per connection |
| `EncGlueConn` (this project's own wrapper) | 8 B | per connection |

`STATIC_BUFFER_LEN` is only `RECORD_HEADER_SZ` here (no
`LARGE_STATIC_BUFFERS`) — the real per-message cost is a heap allocation via
`GrowInputBuffer`/`GrowOutputBuffer`, sized to the current record, not a fixed
worst case, and explicitly shrunk back down (`ShrinkInputBuffer`/
`ShrinkOutputBuffer`) after each message is consumed/sent. Bounded above by
the actual certificate sizes in play (measured, not assumed): server cert
1260 B, client cert 1313 B, CA cert 1283 B — so peak growth per direction is
roughly 1.3 KB during the handshake, not a generic 16 KB TLS-record worst
case.

**Live, on-device measurement** (`meminfo`, over an active TLS session):

```
C-runtime heap: total=163840  largest free block=10240..20480  (nano-malloc; no exact free count)
TCP/IP heap:    size=98224  free=39824  maxblock=27648..39280  highwater=58400..70048
```

The C-runtime heap total (160 KiB) matches the linker's `_min_heap_size`
exactly. `TCPIP_STACK_DRAM_SIZE` (96 KiB) is carved out of it at stack init,
nominally leaving ~64 KiB — but nano-malloc (XC32's lightweight allocator)
reports no exact free total, only the **largest single contiguous block**,
which fluctuated between 10 240 and 20 480 bytes across repeated
measurements depending on what else had allocated/freed recently. This is
the real, load-bearing number, not the naive `160 KiB − 96 KiB` arithmetic:
a single allocation request larger than the current largest free block fails
regardless of total free memory. For this TLS configuration (≈1.3–2 KB peak
buffer growth, 644 B fixed structs) it fits with real but not enormous
margin — closer to 5–8× than the "15–20×" a purely capacity-based estimate
suggested.

Two full-sized concurrent connections (Telnet + bootload, both mid-handshake
at their peak) were reasoned to add to roughly 6–8 KB combined — still under
even the tighter 10 240 B figure only by a moderate margin, not a
comfortable one. `TCPIP_TELNET_MAX_CONNECTIONS` was capped to 1 partly for
this reason.

Committed RAM (static + heap reservation) is 213.9 KiB of 256 KiB (83.6 %),
leaving ~42 KiB for the stack — with no `_min_stack_size` set in the linker,
so that number is a soft leftover, not a guaranteed, checked reservation.

## 4. Was SSH the wrong call to skip?

No — re-affirmed, not just assumed. SSH would need its own protocol
implementation (or a port of one) on top of at least the same crypto
primitives, with a comparable-or-larger footprint than the ~113 KB TLS
already costs, for capability this board does not need (shell multiplexing,
port forwarding, SFTP). Scoping to "TLS-wrap two existing, simple protocols
this project already owns" was the right-sized choice for the available
budget.

## 5. Client tooling changes

Both `scripts/bridge_gui_telnet.py` (the Tk GUI) and `scripts/bootload.py`
(the command-line updater, which the GUI's Bootload button also drives) were
updated:

- Wrap the connection socket in `ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`
  right after `socket.create_connection()`, before any Telnet/bootload
  protocol bytes are sent.
- `check_hostname = False` — the server cert's CN (`bridge-server`) is a
  fixed name from cert-generation time, not tied to whatever IP the board
  happens to have; the server's identity is still verified against the CA
  (`CERT_REQUIRED`), just not by hostname/SAN match.
- Load the project's own CA (`certs/ca/ca_cert.pem`) to verify the
  server, and present the client identity
  (`certs/client/client_cert.pem`/`client_key.pem`) for the mTLS check.
- **`ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT`** — found live, not
  anticipated: without it, a modern OpenSSL-3.x-based Python aborts the
  handshake with `UNSAFE_LEGACY_RENEGOTIATION_DISABLED`, because this
  embedded wolfSSL build does not send the RFC 5746 `renegotiation_info`
  extension that OpenSSL 3.x's default policy otherwise insists on. A
  client-side compatibility setting, not a workaround for a client bug.
- `bootload.py` needed the same wrapping in two places: the Telnet-like
  control/console connection *and* the separate image-data connection
  (TCP/5567).

`bridge_gui_telnet.py` also gained a **"Memory Overview" quick command**:
fetches live `meminfo` from the device, parses the C-runtime/TCP-IP heap
lines, and combines them with the last local build's Flash/RAM totals (read
from `dist\...\memoryfile.xml`) into one panel — clearly labeled that the
Flash/RAM half reflects the last local build, not necessarily whatever image
is actually running on the connected board.

## 6. Live hardware validation

This board (probe `ATML3264031800001049`, per `json/bench.json`) was
actually flashed with the build described above and tested for real —
everything in this section is a live result, not a simulation.

- **Telnet, full flow**: TLS 1.2 handshake completed
  (`ECDHE-RSA-AES128-GCM-SHA256`), client certificate accepted, login with
  `admin`/`password` succeeded, console reached — all bytes observed
  encrypted on the wire and correctly decrypted by the test client.
- **Bootload, full flow**: armed over the TLS console, transferred the
  current 328 315-byte image over the TLS data port (102 kB/s), verified
  (CRC match), committed (bank swap A→B), the board rebooted (back in 15 s),
  and self-confirmed (rollback cancelled) — the entire dual-bank atomic
  update mechanism exercised end-to-end over the encrypted channel.
- **Plaintext is genuinely gone**: connecting to TCP/23 without TLS gets no
  banner at all (previously an instant `Login:` prompt) — confirms the
  encrypted socket type is enforced before any application data flows, not
  just optionally available.
- **mTLS is genuinely enforced, not just present**: reusing wolfSSL's public
  test client certificate (before the certs/bridge/ fix) would have been
  rejected outright — it does not chain to the CA this server actually
  trusts. Only the project's own, correctly-issued client certificate is
  accepted.

## 7. Performance: the handshake blocks the whole board

Measured two ways, in agreement:

- **Host-side wall clock**: 3438 ms per handshake (bench-local, so mostly
  compute time, not network latency).
- **On-device, cycle-accurate** (`cpuload`, DWT cycle counter, 120 MHz core):
  resetting the counters and triggering one isolated handshake (against the
  bootload data port, to avoid the Telnet session-count cap) showed the
  `net_pres` main-loop slot spike to **197 449 008 cycles in a single pass —
  ≈1.645 seconds**.

**This is an architectural finding, not just a number.** This firmware is a
single-threaded cooperative superloop with no RTOS and no preemption
(`SYS_Tasks()`). A ~1.6 s block in one pass means *everything* stops for that
long — not just the TLS handshake, but the T1S↔100BASE-TX bridging function
itself, the console, every other task. The SAME54 has no RSA hardware
acceleration (unlike the PIC32MZ crypto-engine parts wolfSSL's port files
also support), so a software RSA-2048 private-key operation on a 120 MHz
Cortex-M4F costs exactly this much wall-clock time, blocking or not.

This does not affect the proof-of-concept's conclusion ("can TLS run here at
all") but is very likely the first real blocker for anything beyond it: an
update or login that freezes the bridge for over a second, on a device whose
whole purpose is continuous L2 forwarding, is not necessarily acceptable
depending on what is riding across the bridge at that moment. The two
directions worth exploring if this goes further: switch to ECDSA (a
dramatically cheaper signature operation than RSA-2048 in software) instead
of/alongside RSA, and/or find a way to break the crypto operation into
resumable chunks the superloop can interleave — nontrivial in a cooperative,
non-preemptive architecture, and not attempted here.

## 8. Security assessment (see also §2.4, §2.5)

**What actually holds up:**
- Real gate before the login prompt is even reachable — confirmed live, not
  just configured (§6).
- Forward-secret cipher (ECDHE), confirmed via the live handshake.
- A private, non-public CA (not wolfSSL's well-known test PKI) — a random
  client cannot obtain a certificate this server accepts.

**What does not:**
- **The CA private key lives in this repo** (`certs/ca/ca_key.pem`).
  Whoever has repo access can mint arbitrarily many valid client
  certificates. This is the single biggest reason this stays a PoC.
- **`NO_ASN_TIME` means certificates never expire, ever**, and there is no
  revocation mechanism (no CRL, no OCSP) — a leaked client certificate is
  valid forever, with no way to time-box or revoke it short of replacing the
  CA embedded in the firmware.
- **The server's own private key is baked into the shipped firmware image**
  (`bridge_certs.h`), extractable by anyone with physical/SWD access to the
  board, unless flash read-protection (`BOOTPROT` et al.) is separately
  configured — not checked here.
- **What TLS gates is unchanged behind it**: the Telnet login is still the
  hardcoded, README-documented `admin`/`password`. mTLS restricts who can
  *reach* that login, not the strength of the login itself.
- **`net_pres_enc_glue.c` is this session's own, unreviewed code.** wolfSSL
  itself is a mature, audited library; the custom I/O glue bridging it to
  NET_PRES is new, untested against malformed/hostile input, and has no
  fuzzing or review behind it.
- **No rate-limiting or abuse handling** for repeated failed handshake
  attempts. Combined with §3.2's tight heap margin, a network-local attacker
  hammering the port with junk handshakes is an untested denial-of-service
  surface.

## 9. Accessing the board without the custom scripts

The two client scripts (§5) are a purpose-built ("proprietary", as originally
scoped) way in — no off-the-shelf Telnet or SSH client speaks this out of the
box, since it is TLS-wrapped Telnet with a required client certificate, not a
protocol any standard tool defaults to expecting on TCP/23. That is by
design, not an oversight, but it is still possible to reach the board with
genuinely standard tools, given the certificate material:

- **`openssl s_client`**, tested live and working:

  ```bash
  openssl s_client -connect 192.168.0.12:23 \
    -cert client_cert.pem -key client_key.pem -CAfile ca_cert.pem \
    -legacy_renegotiation -quiet
  ```

  `-legacy_renegotiation` is OpenSSL's own name for the same accommodation
  Python needed (`ssl.OP_LEGACY_SERVER_CONNECT`, §5) — this embedded wolfSSL
  build does not send the RFC 5746 `renegotiation_info` extension, which a
  modern OpenSSL 3.x refuses to proceed without unless told to. Confirmed
  live: TLS handshake completed, both certificates verified (`verify
  return:1` for the CA and the server leaf), the Telnet banner and
  `Login:`/`Password:` prompts arrived decrypted. A pure command-line tool,
  no custom script, given the three certificate files.

  On Windows, note that `openssl` is usually only on PATH inside a **Git
  Bash** shell (Git for Windows bundles it under
  `...\Git\mingw64\bin\openssl.exe`), not in a plain `cmd.exe`/PowerShell
  session unless that full path is used explicitly.

- **`stunnel`** (not installed on this machine, not tested here, but a
  standard, widely-packaged tool for exactly this shape of problem): point
  it at the board with the same three certificate files, have it expose a
  local **plaintext** port, and point a genuinely ordinary Telnet client
  (PuTTY, the `telnet` command) at that local port instead. `stunnel` does
  the TLS/mTLS layer; the Telnet client never knows it is there.

Either way, the certificate files are still required - "standard tool" means
"no purpose-written script", not "no key material needed".

### 9.1 This is functionally SSH, without the SSH protocol

Worth naming explicitly, since it is the whole point of the exercise: what
`openssl s_client` (or the custom scripts) gets you - an encrypted,
mutually-authenticated remote console - is the same *security outcome* SSH
would provide, deliberately achieved without SSH's protocol weight (§4). The
real differences that remain:

- **Authentication is split across two layers instead of one.** The TLS
  client certificate gates who can open a connection at all; the
  still-hardcoded `admin`/`password` Telnet login (§8) gates what happens
  after that. SSH folds both into a single handshake-time auth step
  (pubkey or password).
- **No SSH capabilities are present**: no PTY negotiation, no channel
  multiplexing, no SFTP, no port forwarding - just the plain Telnet byte
  stream, encrypted.

That trade - SSH's security property, Telnet's protocol simplicity - is
exactly what made this fit the board's flash/RAM budget where full SSH would
not have (§4).

## 10. Handshake speed-up: wolfSSL's SP math backend

§7 measured the RSA-2048 handshake at ≈1.6 s of blocked main-loop time, software
RSA on a Cortex-M4F with no crypto accelerator. wolfSSL ships an alternative
bignum backend for exactly this: SP ("Single Precision") math with
hand-written ARM Cortex-M assembly, instead of the general-purpose TFM
(`USE_FAST_MATH`) implementation this build started with. Enabled via two
defines in `configuration.h` (`WOLFSSL_HAVE_SP_RSA`,
`WOLFSSL_SP_ARM_CORTEX_M_ASM`), no protocol or certificate changes.

Measured live, before/after, the same way as §7 (`cpuload`, DWT cycle
counter, 120 MHz core):

| | on-device spike (`net_pres` slot) | host wall-clock (full login) |
|---|---|---|
| Before (TFM) | 197 449 008 cycles ≈ 1.645 s | 3438 ms |
| After (SP/ASM) | 89 809 477 cycles ≈ 748 ms | ≈1156 ms |
| Improvement | ≈2.2× | ≈3× |

The wall-clock number includes more than the raw handshake (TCP connect,
Telnet banner, login/password exchange), which is why it improves by more
than the on-device cycle count alone - the fixed protocol overhead around
the handshake stays the same size while the handshake itself shrinks,
so it dominates the "after" number less. Re-measured on the same board
(`192.168.0.12`) after re-flashing all three bench boards with this build
(2026-09-05) - the improvement holds.

This does not remove the architectural problem (§7) - a single handshake
still blocks the whole cooperative superloop for the better part of a
second - but it is a real, measured, no-tradeoff improvement: same
certificates, same protocol, just a faster modular-exponentiation
implementation.

### 10.1 Two unbounded waits in the TLS glue - both fixed, one nearly missed

Found live, unrelated to the SP-math change itself, in two rounds:

**Round 1 - the close side.** Killing a TLS client mid-session with Ctrl-C
(no `close_notify` sent) left the server-side `wolfSSL_shutdown()` waiting
forever for a clean shutdown it will never get (`EncGlue_Close()`'s
`WOLFSSL_ERROR_WANT_READ`/`WANT_WRITE` branch, `net_pres_enc_glue.c`). With
`TCPIP_TELNET_MAX_CONNECTIONS = 1`, that one wedged slot made every
subsequent connection attempt time out at the TLS handshake stage, even
though the raw TCP connect still succeeded instantly - reproduced live via
an `openssl s_client` session interrupted with Ctrl-C. **Fixed**: a bounded
`ENC_CLOSE_TIMEOUT_MS` (5s) - `EncGlue_Close()` now frees the connection
itself once that passes, close_notify or not.

**Round 2 - the accept side, worse.** Found while stress-testing
`scripts/discover.py`'s own subnet scan (§11): a client that opens the TCP
connection and then stalls or abandons the handshake mid-flight leaves
`EncGlue_Connect()`'s `wolfSSL_accept()` returning WANT_READ/WANT_WRITE
forever, same as round 1 - except this side had **no timeout at all**, and
crucially `net_pres.c`'s own pump loop (`NET_PRES_EncTasks`) stops calling
`fpConnect` the moment status leaves the negotiating family, so a stuck
connect here does not even get revisited. Confirmed live: raw TCP connect
kept succeeding, the TLS handshake timed out on every login attempt
indefinitely, recovering only on a physical reset - reproducible just by
running `discover.py`'s scan a few times in a row.

Fixing this one took an extra pass: the obvious fix - free the connection
directly on timeout, mirroring round 1 - would have been a **use-after-free
/ double-free**. `NET_PRES_SocketClose()` calls `fpClose()` unconditionally
for any encrypted socket, and telnet.c's own session loop only notices a
dead connection via `NET_PRES_SocketWasReset()`/`WasDisconnected()` -
transport-level checks, blind to the encryption layer's status - which,
once true, route through `NET_PRES_SocketDisconnect()` and *that* calls
`fpClose()` too. Freeing early would have raced that second call landing on
an already-freed pointer. **Fixed correctly** instead: on timeout,
`EncGlue_Connect()` calls the transport's own `fpDisconnect()` and returns
`NET_PRES_ENC_SS_FAILED` without freeing anything - that's what makes
`WasDisconnected()` true on telnet.c's next pass, driving the *existing*,
already-correct cleanup path through `NET_PRES_SocketDisconnect()` into a
proper `fpClose()` call. `ENC_CONNECT_TIMEOUT_MS` (15s) gives a slow but
genuine client real margin over the ~1.2s a normal handshake takes.
Re-stress-tested after the fix: four scans back to back, no board needed a
reset (one took ~8s to answer instead of ~1.2s - consistent with the new
bound actually firing - but none stayed stuck).

The same gap - "any other wolfSSL error" (bad/rejected client cert,
protocol error), not just a timeout - existed before either fix and got the
same treatment, since it was never distinguished from the timeout case
inside `EncGlue_Connect()`.

A real deployment would still want `TCPIP_TELNET_MAX_CONNECTIONS > 1` on
top of both fixes, so one slow/hostile client can't monopolize the only
slot for the full timeout window while a legitimate one waits.

### 10.2 Operational note: board IP addresses moved on their own

Unrelated to any of the above, but worth recording since it caused real
confusion while debugging it: board `...1049` changed from `192.168.0.12`
to `192.168.0.11` at some point during this session's many resets (SWD and
software), confirmed by directly comparing the TLS certificate served at
each address against the known fingerprint. Not a scanner bug, not a
bridging artifact (both were seriously considered and ruled out) - just a
DHCP lease without a per-MAC reservation on this bench's network handing
out a different address across reboots. Worth a static reservation (or a
fixed IP on the boards themselves) if address stability matters for
anything beyond this kind of interactive session.

## 11. A small PKI: per-board identities and remote provisioning

Everything in §2-§9 used one shared server identity, compiled into every
board's firmware alike (`bridge_certs.h`). That does not scale past a single
bench unit: with more than one board on a network, they are
indistinguishable at the TLS layer, and there is no way to revoke or rotate
one board's identity without reflashing it. This section adds - and, unlike
the rest of this report's original scope, *lives entirely outside the
firmware image itself* except for the small remote-provisioning receiver -
a way to give each board its own certificate/key pair, tracked as a small
local PKI on the operator's machine.

**`scripts/pki.py`** - a standalone, GUI-free module (same philosophy as
`bootload.py`): a self-signed project CA (`certs/ca/ca_{cert,key}.pem`,
reused from §2), a shared operator client identity, and
`issue_board_identity(board_id, ip=...)`, which generates a fresh RSA-2048
keypair, signs a leaf certificate against the CA, and writes
`certs/boards/<board_id>/server_{cert,key}.{pem,der}` plus a
`json/boards/<board_id>.json` record (fingerprint, serial, validity window,
IP, PEM/DER paths, a `provisioned` flag) - one JSON file per board, so a
fleet of boards is just a directory listing.

**`firmware/src/cert_provision.c`/`.h`** - the on-device receiver. Adds a
`cert` command group (`cert_arm`, `cert_show`, `cert_save`, `cert_reset`,
`cert_abort`) alongside a small binary data port (5568, same
TLS+mTLS-encrypted-socket pattern as `bootload.c`'s data port 5567, for the
same reason: an 80-byte Telnet line buffer cannot carry a ~1 KB certificate
as hex text). A staged, in-RAM `cert_store_t` is armed with one item
(`server_cert` / `server_key` / `ca_cert`) at a time, filled over the data
port, CRC32-checked, and only written to the emulated EEPROM on an explicit
`cert_save` - `cert_reset` reverts to the compiled-in default. Mirrors
`bootload.c`'s existing "arm, transfer, verify, explicit commit, needs a
`reset` to actually take effect" shape throughout, deliberately, so the two
subsystems read as one design rather than two.

**`scripts/cert_provision.py`** - the client side, reusing `bootload.py`'s
already-TLS-wrapped `Console` class rather than duplicating login/framing
logic. `push_board(ip, board_id)` sends a board's `server_cert`+`server_key`
from `pki.py`'s output, then `cert_save`; a device `reset` is still a
separate, explicit step (same "verify then commit then reboot" caution as
bootload's own image swap).

**`scripts/discover.py`** - finds boards on the local network segment, so
the operator doesn't need to already know an IP to start with. A plain TCP
connect + mutual-TLS handshake against port 23 across the local /24 (our CA,
our shared client identity) - not mDNS: a responder would need a new
Harmony component in every board's firmware (flash is already at 66% of
budget, §3.1) and a fleet reflash, for a bench of a handful of boards where
a plain scan takes a few seconds. Every completed handshake is matched
against `pki.py`'s known fingerprints to show a `board_id` where one is
known, `(unregistered)` otherwise. See §10.1 for the real bug this scan's
own load surfaced in the TLS glue, and §10.2 for a board-address mixup this
same testing turned up (unrelated to the scanner itself).

**The "Certificates" tab** (`bridge_gui_telnet.py`, between Terminal and
Help) ties all of it together: a "Discovered on network" panel
(`discover.py`, a "Scan Network" button, click a result to load its IP into
the fields above) above a "Known boards" panel read from `pki.py`'s
`json/boards/*.json`, CA status, "Issue New Board Identity" (calls
`pki.issue_board_identity()`, defaulting the suggested id from the IP field
above), and against whatever device is configured in the IP/user/password
fields - "Show Device's Active Identity" (`cert_show`), "Push Selected
Board Identity to Device", and "Reset Device to Compiled-In Default".
Background-thread + result-queue plumbing matches every other long-running
action already in that file.

### 11.1 Three bugs found live, same rigor as §6

Getting `push_board()` to actually work end-to-end on real hardware
surfaced three separate, real bugs - none visible from reading the code
alone:

1. **A stale "reset" flag aborted every transfer before it read a byte.**
   `bootload.c`'s own `BL_WAIT_CONN` state has a one-line fix,
   `(void)NET_PRES_SocketWasReset(s_sock);`, right where it transitions to
   receiving - "as iperf.c/testserver.c do", per its comment. The stack
   sets that flag as a side effect of accept/handshake itself, not only on
   a genuine reset. `cert_provision.c`'s analogous `CP_WAIT_CONN` transition
   was missing that exact call: the TLS+mTLS handshake completed
   successfully on *both* sides (confirmed independently on the Python
   client), but `CP_RECV`'s very first `NET_PRES_SocketWasReset()` check
   then saw the stale flag and aborted ("connection lost") before a single
   byte of the certificate was read. Fixed by copying the same clear-on-
   transition line into `cert_provision.c`.
2. **The completion report collided with the next command, on the same
   connection.** `cp_report()` originally wrote a transfer's result to both
   the data socket *and* the console (`CMD_PRINT_OR_CONSOLE`), matching
   `bl_fail()`'s pattern. But `TCPIP_TELNET_MAX_CONNECTIONS` is 1, and
   `push_board()` runs `cert_arm`+transfer for `server_cert`, then again for
   `server_key`, all on *one* Telnet connection - the console echo landed
   in that same stream just before the next command's own reply, and
   `Console.command()`'s "one line per command" assumption (documented in
   `bootload.py`) misread it as the reply to arming `server_key`. Fixed by
   dropping the console echo entirely: with only one possible connection,
   there is no "other observer" to echo to.
3. **The cert store didn't fit the emulated EEPROM at all.** The original
   `CERT_MAX_DER = 1536` (three slots, "generous for an RSA-2048 cert or
   key DER") sized `cert_store_t` at 4632 bytes, and `CERT_EE_OFFSET =
   4096` - but the emulated EEPROM's *entire* usable capacity is
   `EEPROM_EMULATOR_NUM_LOGICAL_PAGES (8) × EEPROM_EMULATOR_PAGE_DATA_SIZE
   (508) = 4064 bytes total`, itself sized to exactly fit
   `bootload.c`'s `BL_ENV_SIZE` (16 KiB = 2 flash blocks), the fixed
   "environment window" at the top of every bank that a firmware image can
   never touch (WP3) and that a bank swap shadow-copies intact. Both the
   struct and the offset were already past the valid range - every
   `cert_save` failed with "EEPROM write failed". The real certs/keys in
   use are 879-1192 bytes; **enlarging the 16 KiB window was considered and
   rejected** (it would mean growing `BL_ENV_SIZE`/`BL_MAX_IMAGE`
   consistently across both banks and touching the already-hardened
   bootloader/swap logic - real risk for no need). Fixed instead by
   shrinking `CERT_MAX_DER` to 1280 (real margin over the observed max) and
   moving `CERT_EE_OFFSET` to 128 (just past `env.c`'s 72-byte record) -
   `3*1280+24 = 3864` bytes, comfortably inside the 4064-byte window.

### 11.2 Live end-to-end verification

After all three fixes: `pki.py issue-board bridge-ATML3264031800001049
--ip 192.168.0.12` → `cert_provision.py --push-board
bridge-ATML3264031800001049` → `cert_save` → device `reset` → a fresh TLS
handshake against the board serves a certificate whose SHA-256
(`64f906cf...`) matches the pushed board identity's own DER file exactly,
confirmed by independently hashing both. Repeated cleanly from a fresh
flash (the EEPROM-held identity survives a firmware reflash, as expected -
it lives outside the app image's flash region entirely).

## 12. Open items / not done

- Nothing in this report is committed to git. The branch's working tree has
  the LAN867x/eth1 experiment reverted (back to LAN8742A/LAN8740A) and the
  full TLS/mTLS state on top of that.
- The bench board (`ATML3264031800001049`, currently reachable at
  `192.168.0.12`) is **currently running this experimental build**, not
  whatever was on it before this session.
- `bridge_gui_telnet.py`'s Tk window itself was never clicked through this
  session — only its underlying connection/command logic, exercised directly
  via the same code paths from a script.
- The shared-TLS-session-slot idea (Telnet and bootload never active at the
  same time) was discussed but never enforced in code; nothing currently
  stops both from running concurrently.
- A real deployment would need, at minimum: the CA generated and held
  offline (never in a repo), a real time source (RTC or SNTP) so
  certificate lifetimes mean something, a way to revoke or replace a
  compromised client certificate short of replacing the CA, a non-default
  Telnet password, and a resolution to the §7 blocking-handshake problem.
- §11's PKI is still bench-grade in the same ways §8/§2.4 already flagged:
  board private keys are generated on the *operator's* machine and shipped
  over the wire (never generated on-device), there is still no revocation
  for a compromised board identity (same `NO_ASN_TIME` caveat as §2.5/§8),
  and `push_board()` needs the one available Telnet connection for its
  whole sequence - it cannot run while anyone else is connected, and a
  connection drop mid-sequence leaves the board part-armed (recoverable
  with `cert_abort`/`cert_reset`, but not automatic).
- The "Certificates" tab (§11) was, like the rest of `bridge_gui_telnet.py`,
  exercised through its underlying `pki.py`/`cert_provision.py` calls and
  `py_compile`, not by clicking through the actual Tk window.
