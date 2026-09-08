# Implementation plan: offload RSA to the PUKCC

Goal: stop doing RSA-2048 in software. The measured cost today is **139 M
cycles (1.16 s of CPU) per mutual-TLS handshake, of which a single
uninterruptible 745 ms call is the private-key operation** that stalls the
whole round-robin loop (`docs/resource-cost-tls-mqtt.md` §4.2). The part has a
public-key accelerator this firmware does not touch (§5 of the same report).

This plan is written so it can be abandoned cheaply after stage 1, which is
deliberate: the payoff is unknown until measured, and stage 1 measures it
without touching the TLS path at all.

---

## 1. What already exists

Verified in this tree on 2026-09-08, not assumed:

| Piece | Where | Note |
|---|---|---|
| PUKCL parameter-block API | `firmware/src/third_party/wolfssl/wolfssl/wolfcrypt/port/pic32/CryptoLib_*_pb.h` | complete: ExpMod, CRT, SelfTest, RedMod, Fmult, ECDSA, … |
| ROM jump table | `CryptoLib_JumpTable_pb.h`: `__vCPKCLCsJumpTableStart 0x02000001` | one entry per service, Thumb address |
| Entry macro + parameter type | `CryptoLib_Headers_pb.h`: `PUKCL_PARAM`, `vPUKCL_Process(service, pv)`, `PUKCL(field)` | |
| Crypto RAM | `CryptoLib_mapping_pb.h`: `MSB_EXTENT_CRYPTORAM 0x02010000`, `nu1CRYPTORAM_BASE 0x1000`, 4 KB | last two words reserved |
| Status register | `CryptoLib_Hardware_pb.h`: `PUKCCSR` at `0x4200302C`, `BIT_PUKCCSR_CLRRAM_BUSY` | |
| Clock gate | `MCLK_AHBMASK_PUKCC`, bit 20 (`packs/.../component/mclk.h`) | |
| wolfSSL callback framework | `WOLF_CRYPTO_CB` already defined in `configuration.h`; `wc_CryptoCb_RegisterDevice(devId, cb, ctx)`, `CryptoDevCallbackFunc(int devId, wc_CryptoInfo*, void*)` | compiled in today, unused |
| devId plumbing | `wolfSSL_CTX_SetDevId(ctx, devId)` in `ssl.h` | |
| DWT cycle counter | `cpuload.c` already arms `CoreDebug->DEMCR.TRCENA` + `DWT->CTRL.CYCCNTENA` | reuse for benchmarking |
| Toolchain / bench | XC32 v5.10; three boards with EDBG probes attached | build, flash, measure all local |

**What is missing is only the glue**: the `CryptoLib_*_pb.h` files are headers,
nothing in the tree calls them. wolfSSL's only port *source* is
`pic32mz-crypt.c`, which is PIC32MZ hardware crypto, not PUKCC.

## 2. Stage 1 - PUKCC bring-up and benchmark (no TLS involvement)

The point of this stage is a number: how long does the PUKCC take for the
operation that currently costs 745 ms. Nothing in the TLS path changes, so the
worst case is a module that does not work and gets deleted.

**New files**

- `firmware/src/pukcc.c` / `pukcc.h`
  - `PUKCC_Initialize(void)`: set `MCLK_AHBMASK_PUKCC`, spin on
    `PUKCCSR & BIT_PUKCCSR_CLRRAM_BUSY`, then run the `SelfTest` service and
    check `u4Version`, `u4CheckNum1 == 0x6E70DDD2`, `u4CheckNum2 == 0x25C8D64F`
    (data sheet §43.3.3.1). Records the outcome in a module-level status so a
    failure is reportable rather than fatal.
  - `PUKCC_RsaCrt(...)`: one modular exponentiation with CRT, taking p, q, dP,
    dQ, qInv and the input block as plain byte buffers, and returning the
    result. Everything PUKCL-specific lives behind this signature: LSB-first
    byte order, 32-bit alignment, lengths padded to a multiple of four, and
    all parameters copied into the Crypto RAM window
    (`CryptoLib_CRT_pb.h`: `nu1XBase`, `nu1ModBase` = primes P and Q,
    `nu1PrecompBase`, `u2ModLength` = length of *one* prime, `u2ExpLength`).
  - `PUKCC_ExpMod(...)`: plain modular exponentiation (no CRT) as the fallback
    path and as a cross-check against a known vector
    (`CryptoLib_ExpMod_pb.h`, option `PUKCL_EXPMOD_FASTRSA`, window size 1
    first - larger windows are faster but need more Crypto RAM).

**Wiring**

- Call `PUKCC_Initialize()` from `APP_Initialize()` in `app.c`, next to the
  existing `CERT_PROVISION_Initialize()` call - before anything can open a TLS
  socket.
- Register the file in `firmware/tcpip_iperf_lan865x.X/nbproject/configurations.xml`
  as `<itemPath>../src/pukcc.c</itemPath>`, next to `cert_provision.c`, then
  regenerate the Makefile fragments (`batch\genmk.bat`) - a new source file is
  the one case where the generated fragments do not pick things up by
  themselves.
- Two console commands in the same table as `cert_show`/`cpuload`:
  - `pukcc status` - self-test result, library version, Crypto RAM base.
  - `pukcc bench [n]` - runs `n` (default 10) RSA-2048 CRT operations on a
    compiled-in known key/vector, times them with `DWT->CYCCNT` the way
    `cpuload.c` already does, prints min/mean/max in cycles and ms, and
    verifies the result against the expected value so a fast wrong answer
    cannot pass as a success.

**Verification for stage 1**

1. `pukcc status` reports the self-test passing with the two documented check
   values. If it does not, stop: everything after this depends on it.
2. `pukcc bench` result matches the known vector **and** is meaningfully faster
   than 745 ms. The data sheet's "Service Timing for RSA" table (§43.3.8) is
   the expectation to compare against - look the values up before starting, so
   the benchmark can be judged rather than just admired.
3. `cpuload stats` while benchmarking: the cost should show up as one big
   `app`-slot call, mirroring today's `net_pres` picture.

**Decision point.** If the speed-up is small, or the CRT path is unreliable,
this stops here: delete the module, keep the benchmark numbers in the report,
and note in `resource-cost-tls-mqtt.md` §5 that the accelerator was tried and
measured rather than left as a suggestion.

## 3. Stage 2 - wolfSSL crypto callback

**New file** `firmware/src/pukcc_wolfssl.c` (kept separate from `pukcc.c` so
the driver stays testable without wolfSSL in the picture):

```c
static int PukccCryptoCb(int devId, wc_CryptoInfo* info, void* ctx)
{
    if (info->algo_type == WC_ALGO_TYPE_PK &&
        info->pk.type == WC_PK_TYPE_RSA &&
        (info->pk.rsa.type == RSA_PRIVATE_DECRYPT ||
         info->pk.rsa.type == RSA_PRIVATE_ENCRYPT)) {
        /* extract p, q, dP, dQ, u from info->pk.rsa.key, call PUKCC_RsaCrt() */
        ...
        return 0;                       /* handled */
    }
    return CRYPTOCB_UNAVAILABLE;        /* wolfSSL falls back to software */
}
```

- Register once from `PUKCC_Initialize()`'s caller:
  `wc_CryptoCb_RegisterDevice(PUKCC_DEV_ID, PukccCryptoCb, NULL)`.
- Set the devId on both contexts, one line each:
  - `net_pres_enc_glue.c` (server: Telnet, bootload, cert provisioning) after
    `wolfSSL_CTX_new(wolfTLSv1_2_server_method())`, around line 132.
  - `net_pres_enc_glue_client.c` (the MQTT client role) after its own
    `wolfSSL_CTX_new(wolfTLSv1_2_client_method())`, around line 125.
  Both are MCC-generated files that this project already hand-patches, so the
  change belongs in `patches/` and in
  `docs/mcc-generated-code-patches.md` like every other one - **not** as a
  silent edit that the next MCC run reverts.

**The delicate part** is not the callback but the key material. wolfSSL's
`RsaKey` with `USE_FAST_MATH` holds `p`, `q`, `dP`, `dQ`, `u` as `fp_int` (552
bytes each in this build); PUKCL wants them LSB-first in Crypto RAM with
32-bit-aligned lengths that are multiples of four. Getting the byte order or
the padding wrong yields a wrong signature, not a crash - which is why the
bench in stage 1 verifies against a known vector before any of this is wired
to a live handshake.

**Fail-safe by construction:** any unexpected condition - self-test failed, key
too large, CRT parameters absent, PUKCL status not `PUKCL_OK` - returns
`CRYPTOCB_UNAVAILABLE` and wolfSSL silently uses its software path. The worst
realistic outcome is "no faster than before", not "no TLS".

## 4. Stage 3 - verify and write it down

1. **Function** before speed: full handshake against all three bench boards
   (`discover.py`), then a `bootload` transfer and one `cert_provision` push,
   because those use the same server context on different ports. Then the MQTT
   client, which is the *client* context and the one most likely to be
   forgotten.
2. **Re-measure** with exactly the method of the resource report §4.2: reset
   counters, close the console, run N handshakes, read back. The number to
   watch is `net_pres`'s longest single call - 89.4 M cycles today.
3. **Heap**: `heapinfo` before and after, and a check that the 4 KB Crypto RAM
   is not stolen from the C heap (it is a separate address window, so it
   should not be - confirm rather than assume).
4. **Update** `docs/resource-cost-tls-mqtt.md` §4.2, §5 and §7's item 6 with
   the measured result, whichever way it goes.

## 5. Risks, and what each one costs

| Risk | Likelihood | Cost if it happens | Mitigation |
|---|---|---|---|
| Wrong signature from byte-order/padding mistakes | medium | handshakes fail | known-vector check in stage 1; `CRYPTOCB_UNAVAILABLE` fallback |
| A board becomes unreachable (TLS is the only remote path) | low | reflash over SWD | all three boards have EDBG probes attached; `flash.bat --probe <serial>` |
| The MCC-generated glue files get regenerated later | medium | devId lines vanish, silently back to software | record both edits in `patches/` + `docs/mcc-generated-code-patches.md` |
| PUKCL needs more stack than is free | low | hard fault under load | ExpMod 200 B / CRT 304 B per data sheet §43.3.8 against 37,584 B of stack region - but that region is what is left over, not a designed budget (report §3.4) |
| Speed-up smaller than hoped | medium | the work is wasted beyond the measurement | stage 1 exists precisely to find this out first |

**Not a risk:** flash size. The PUKCL lives in the device's ROM, so the code
added here is the glue only. If the software RSA path is ever dropped entirely
afterwards, `sp_cortexm.o` (19.7 KB) and part of `tfm.o` (12.2 KB) become
removable - but keeping the software fallback is the whole safety argument, so
that is a later decision, not part of this plan.

## 6. What this does *not* do

- **The blocking call does not disappear.** The data sheet is explicit
  (§43.3.8): "This library is using the main core to execute its
  computations". The stall gets shorter; the round-robin loop still does not
  turn during a handshake. Timeouts tuned around today's 745 ms
  (`discover.py`'s `TLS_TIMEOUT`/`RETRY_TLS_TIMEOUT`, the firmware's own
  `ENC_CONNECT_TIMEOUT_MS`) should be re-checked afterwards, not blindly
  reduced.
- **AES and ICM stay unused.** Per report §4.3 the per-byte cost of an
  established connection is dominated by the console's per-line framing, not
  by the cipher, so hardware AES would move a few percent of a number that is
  not the bottleneck. Separate question, separate plan.
- **No MCC component is added.** Everything is either a new file under
  `firmware/src/` or a two-line hand-patch to already-patched generated files.

## 7. Suggested sequencing

Work on a branch of its own (`pukcc-rsa-offload`), off
`tls-mtls-telnet-bootload-poc`, so the TLS branch stays deployable while this
is in flux. One commit per stage, each independently revertible:

1. `pukcc.c/.h` + console commands + build registration - **stops here if the
   benchmark disappoints**.
2. the crypto callback and the two devId hand-patches.
3. measurements and the report update.

## Related

- `docs/resource-cost-tls-mqtt.md` - the measurements this plan exists to
  improve, §4.2 (the 745 ms) and §5 (the unused hardware).
- `docs/mcc-generated-code-patches.md` + `patches/README.md` - where the two
  glue edits have to be recorded.
- Data sheet §43 (PUKCC/PUKCL): [overview §43.1](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-85A335EF-0029-47C3-B3B1-05A040D0F82D.html),
  [API §43.3.1](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-8D532BF9-2B97-4A52-AEB7-33EDA9490E02.html),
  [init + self-test §43.3.3.1](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-7C82F552-E5A2-4284-AC7B-EB861635D1B4.html),
  [timings and stack usage §43.3.8](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-2CF0C526-FE9E-4BE9-BEA2-C192956C7507.html).
