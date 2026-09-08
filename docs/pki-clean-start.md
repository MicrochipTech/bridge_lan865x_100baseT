# Clean start: from a fresh checkout to a provisioned bench

What to do after cloning this repo (or when starting a bench over from
scratch). The short version: **nothing but the default identity comes with
the repo.** Every board identity is created locally and has to be pushed onto
the board it belongs to.

## What is in the repo, and what is not

| Path | In git? | What it is |
|---|---|---|
| `firmware/src/bridge_certs.h` | yes | the compiled-in **default identity**: CA certificate + default server cert/key, in DER arrays. Every freshly flashed board runs this until an individual identity is pushed to it. |
| `certs/default/` | yes | the same default identity as `.pem`/`.der` - the source those arrays were generated from. |
| `certs/ca/` | yes | the project CA, **including `ca_key.pem`**. Bring-up material, not a deployment pattern - see the warning below. |
| `certs/client/` | **no** | the operator identity every host-side tool presents (GUI, `bootload.py`, `discover.py`). Created locally. |
| `certs/boards/<id>/` | **no** | one board's private key + certificate. Never committed: a checked-in board key would let every clone of this repo impersonate that board. |
| `json/boards/<id>.json` | **no** | that board's host-side record (fingerprint, IP, dates). |
| `certs/mqtt/` | **no** | the MQTT broker's server identity (`mqtt_broker.py`). Created locally, only needed for the MQTT work. |

> **The CA private key is in this repo on purpose, and only because this is
> bring-up material.** Whoever holds `certs/ca/ca_key.pem` can mint a client
> certificate that every board here accepts. It stays tracked so that a fresh
> clone can talk to a freshly flashed board at all: the board demands a client
> certificate (`WOLFSSL_VERIFY_FAIL_IF_NO_PEER_CERT`,
> `net_pres_enc_glue.c`) and verifies it against the CA compiled into
> `bridge_certs.h`, so a locally invented CA would lock the operator out until
> the firmware is rebuilt around it. A real deployment moves the CA offline and
> ships `bridge_certs.h` built from that CA's certificate only - see
> `docs/tls-poc-report.md` §8/§12.

## The procedure

Assumes `setup.bat` has been run once (creates `.venv`). Commands are written
for the repo root; `PY=.venv\Scripts\python.exe`.

### 1. Delete every board identity on the host

A fresh clone has none - this step is for a bench being started over, and for
making sure the host claims to know nothing it cannot prove.

```
%PY% scripts\pki.py delete-all-boards --yes
```

GUI equivalent: *Certificates* tab -> **Delete ALL Identities...**. Both
delete `json/boards/*.json` and `certs/boards/<id>/`, and leave the CA, the
operator identity and the MQTT identity alone.

### 2. Flash the firmware onto every target

```
flash.bat --probe <serial>          rem release\bridge_lan865x_100baseT.hex
```

After this every board must be running the **default identity** from
`bridge_certs.h`. Flashing alone does not guarantee that: the individual
identity a board was provisioned with lives in emulated EEPROM, which a plain
`pyocd flash` of the program image does not touch. So either

- chip-erase first (`flash.bat --erase --probe <serial>`, then flash), or
- if the board is still reachable over Telnet, run `cert_reset` and then
  `reset` on it - that reverts the saved identity to the compiled-in default
  without an erase.

`cert_show` on the board says which one is active (`compiled-in default` vs.
`EEPROM`).

### 3. Create the operator client identity

The CA comes from the repo; the operator leaf does not.

```
%PY% scripts\pki.py ca-status         rem sanity check: CA present + fingerprint
%PY% scripts\pki.py issue-client
```

GUI equivalent: *Certificates* tab -> **Create Operator Client Identity**.
Only needed for MQTT work:

```
%PY% scripts\pki.py issue-mqtt-broker
```

Until this step, discovery finds the boards but the fingerprint column stays
empty - there is no client certificate to hand them yet, and the GUI says so
rather than showing three unexplained failures.

### 4. Find the boards

```
%PY% scripts\discover.py --base-ip 192.168.0.12
```

UDP broadcast, then one mutual-TLS handshake per answering board. At this
point every board should answer with the same fingerprint: they are all still
running the shared default identity. GUI equivalent: **Discover Boards**.

### 5. Issue one identity per board

```
%PY% scripts\pki.py issue-board bridge-192-168-0-12 --ip 192.168.0.12
```

Use whatever board id is stable for the bench - a probe serial
(`bridge-ATML3264031800001103`) survives an IP change, an IP-derived name is
easier to read. GUI equivalent: **Issue New Board Identity...**.

### 6. Push each identity onto its board

```
%PY% scripts\cert_provision.py --ip 192.168.0.12 --push-board bridge-192-168-0-12
```

Sends `server_cert` + `server_key` over the mutual-TLS provisioning channel
(port 5568) and runs `cert_save`. **The board keeps serving the old identity
until it reboots** - `reset` it afterwards, same "verify, then commit, then
reboot" order as a `bootload` image swap. GUI equivalent: **Push Selected
Board Identity to Device**.

### 7. Verify

```
%PY% scripts\discover.py --base-ip 192.168.0.12
```

Every board must now show its **own** fingerprint, and in the GUI its own
`board_id` instead of `(unregistered)`. If one still shows the default
fingerprint, it was not reset after `cert_save`.

## Related

- `docs/tls-poc-report.md` - why the PKI looks like this, §8/§12 for what a
  real deployment would have to change.
- `scripts/pki.py` - the module every step above goes through; usable from a
  plain Python shell, not only via the GUI.
- `scripts/discover.py` - discovery (broadcast first, then mTLS); explains why
  no subnet or ping sweeps are used.
