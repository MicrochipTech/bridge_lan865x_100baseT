#!/usr/bin/env python3
"""
apply_patches.py - re-apply this project's hand-patches to MCC-generated code
after running "Generate Code" in MPLAB X.

Background: firmware/src/config/default/** is normally touched only through
MCC + Generate Code (see CLAUDE.md section 1). A handful of files need a
small number of hand-edits anyway - documented exceptions, see
docs/mcc-generated-code-patches.md for the full story on each one. Every
Generate Code run silently reverts them. This script puts them back.

What it does, per patch:
    1. Try to apply it cleanly (git apply --check). If that works, the file
       was freshly regenerated and needs the patch - apply it for real.
    2. If that fails, check whether the patch is already applied
       (git apply --check -R, i.e. "does reversing this patch apply
       cleanly?"). If so, nothing to do - already patched (e.g. Generate
       Code wasn't run, or didn't touch this file).
    3. If neither works, MCC changed something in or around the patched
       region - the script does NOT guess. It reports FAILED and leaves the
       file alone; go look at what changed and re-apply that one patch by
       hand (see docs/mcc-generated-code-patches.md for exactly what each
       patch is supposed to do).

The two recurring "#include <stdarg.h> missing" bugs (a known MCC generator
gap, no diff-able baseline exists for either occurrence - see
docs/mcc-generated-code-patches.md item 6) are handled separately as a
simple idempotent text check-and-insert, not a .patch file.

Usage:
    python apply_patches.py            # apply everything, print a report
    python apply_patches.py --check    # dry run: report only, change nothing

Run from anywhere inside the repo - it finds the repo root itself via git.
"""
import argparse
import subprocess
import sys
from pathlib import Path

# --- Config -----------------------------------------------------------------

PATCHES_DIR = Path(__file__).resolve().parent

# (label, relative path from repo root, text to insert, anchor line to insert
#  it before). The anchor is the first line of the file's own include block
# that is NOT expected to move - if MCC ever restructures far enough that
# the anchor itself is gone, this reports FAILED instead of guessing.
STDARG_FIXES = [
    (
        "drv_lan865x_api.c stdarg.h",
        "firmware/src/config/default/driver/lan865x/src/dynamic/drv_lan865x_api.c",
        '#include "configuration.h"',
    ),
    (
        "telnet.c stdarg.h",
        "firmware/src/config/default/library/tcpip/src/telnet.c",
        '#include "net_pres/pres/net_pres_socketapi.h"',
    ),
]

# The wolfSSL/TLS contract configuration.h must satisfy for this project's
# mutual-TLS features (Telnet, bootload, cert provisioning, the MQTT client) to
# build and work at all. Deliberately NOT a .patch file: MCC rewrites
# configuration.h on essentially every Generate Code for unrelated reasons
# (pins, components, heap sizes), so a whole-file diff would conflict
# constantly. This is the same idempotent check-and-fix shape as STDARG_FIXES.
#
# Each entry was verified against the commit that introduced it:
#   TCPIP_TELNET_MAX_CONNECTIONS 2 -> 1, NO_ASN_TIME added     (31ca819)
#   NO_WOLFSSL_CLIENT removed, HAVE_TLS_EXTENSIONS +
#   HAVE_SUPPORTED_CURVES added                                (f4ed669)
#   WOLFCRYPT_ONLY removed                                     (c6c32ae)
TLS_CONFIG_PATH = "firmware/src/config/default/configuration.h"
TLS_CONFIG_ANCHOR = "// ---------- FUNCTIONAL CONFIGURATION START ----------"
TLS_REQUIRED_DEFINES = [
    # (macro, why it matters if MCC drops it)
    ("NO_OLD_TLS", "forces TLS >= 1.2"),
    ("NO_SESSION_CACHE", "one session at a time; no resumption cache"),
    ("NO_ASN_TIME", "no RTC on this board - every cert would look 'not yet valid'"),
    ("WOLFSSL_HAVE_SP_RSA", "Cortex-M assembly RSA; without it a handshake blocks ~1.6s"),
    ("WOLFSSL_SP_ARM_CORTEX_M_ASM", "selects that ASM path"),
    ("HAVE_TLS_EXTENSIONS", "prerequisite of HAVE_SUPPORTED_CURVES"),
    ("HAVE_SUPPORTED_CURVES", "without it the ClientHello carries no curve list and "
                               "a strict TLS 1.2 peer answers NO_SHARED_CIPHER"),
]
# Macros MCC generates that must NOT be active - each one disables a whole half
# of the TLS feature set.
TLS_FORBIDDEN_DEFINES = [
    ("WOLFCRYPT_ONLY", "would drop the entire TLS protocol layer, leaving only crypto"),
    ("NO_WOLFSSL_CLIENT", "would drop wolfSSL_connect() - the MQTT client cannot dial out"),
]
# (macro, required value, what MCC generates instead)
TLS_REQUIRED_VALUES = [
    ("TCPIP_TELNET_MAX_CONNECTIONS", "1", "MCC generates 2; this board has RAM for one TLS session"),
]

# --- Helpers ------------------------------------------------------------

def repo_root():
    res = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=True)
    return Path(res.stdout.strip())


def git_apply_check(root, patch_path, reverse=False):
    cmd = ["git", "apply", "--check"]
    if reverse:
        cmd.append("-R")
    cmd.append(str(patch_path))
    res = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    return res.returncode == 0, res.stderr


def git_apply(root, patch_path):
    res = subprocess.run(["git", "apply", str(patch_path)], cwd=root,
                          capture_output=True, text=True)
    return res.returncode == 0, res.stderr


def apply_one_patch(root, patch_path, dry_run):
    name = patch_path.stem
    can_apply, _ = git_apply_check(root, patch_path)
    if can_apply:
        if dry_run:
            return name, "WOULD APPLY", "file is unpatched (fresh MCC output) - patch applies cleanly"
        ok, err = git_apply(root, patch_path)
        if ok:
            return name, "APPLIED", "was missing, now re-applied"
        return name, "FAILED", f"clean-apply check passed but the real apply failed: {err.strip()}"

    already, _ = git_apply_check(root, patch_path, reverse=True)
    if already:
        return name, "OK", "already applied, nothing to do"

    return name, "FAILED", ("neither applies cleanly nor is already applied - "
                             "MCC likely changed the surrounding code; check by hand "
                             "(see docs/mcc-generated-code-patches.md)")


def apply_stdarg_fix(root, label, rel_path, anchor, dry_run):
    path = root / rel_path
    if not path.exists():
        return label, "FAILED", f"file not found: {rel_path}"
    text = path.read_text(encoding="utf-8")
    if "#include <stdarg.h>" in text:
        return label, "OK", "already present, nothing to do"
    if anchor not in text:
        return label, "FAILED", f"anchor line not found ({anchor!r}) - insert '#include <stdarg.h>' by hand"
    if dry_run:
        return label, "WOULD APPLY", "missing, would insert before the anchor line"
    new_text = text.replace(anchor, "#include <stdarg.h>\n" + anchor, 1)
    path.write_text(new_text, encoding="utf-8")
    return label, "APPLIED", "was missing, inserted"


def apply_tls_config_fix(root, dry_run):
    """Enforce the wolfSSL/TLS contract in configuration.h (see the tables above).

    Returns the usual (label, status, detail) triple. Reports every individual
    deviation it found, because a partially-reverted configuration.h is the
    dangerous case: it still compiles, and the failure only shows up as a TLS
    handshake that mysteriously stops working.
    """
    import re

    label = "configuration.h wolfSSL/TLS"
    path = root / TLS_CONFIG_PATH
    if not path.exists():
        return label, "FAILED", f"file not found: {TLS_CONFIG_PATH}"
    text = path.read_text(encoding="utf-8")
    problems, fixes = [], []

    for macro, why in TLS_REQUIRED_DEFINES:
        if not re.search(r"^\s*#define\s+%s\b" % re.escape(macro), text, re.M):
            problems.append(f"missing #define {macro} ({why})")
            fixes.append(("add", macro))

    for macro, why in TLS_FORBIDDEN_DEFINES:
        if re.search(r"^\s*#define\s+%s\b" % re.escape(macro), text, re.M):
            problems.append(f"#define {macro} is back ({why})")
            fixes.append(("remove", macro))

    for macro, want, note in TLS_REQUIRED_VALUES:
        m = re.search(r"^\s*#define\s+%s\s+(\S+)" % re.escape(macro), text, re.M)
        if m is None:
            problems.append(f"missing #define {macro} (want {want}; {note})")
        elif m.group(1) != want:
            problems.append(f"{macro} is {m.group(1)}, want {want} ({note})")
            fixes.append(("value", macro, want))

    if not problems:
        return label, "OK", "all TLS-critical settings present, nothing to do"
    if dry_run:
        return label, "WOULD APPLY", "; ".join(problems)
    if TLS_CONFIG_ANCHOR not in text:
        return label, "FAILED", (f"anchor {TLS_CONFIG_ANCHOR!r} not found - fix by hand: "
                                  + "; ".join(problems))

    for fix in fixes:
        if fix[0] == "add":
            text = text.replace(TLS_CONFIG_ANCHOR,
                                 f"#define {fix[1]}\n{TLS_CONFIG_ANCHOR}", 1)
        elif fix[0] == "remove":
            text = re.sub(r"^(\s*)#define\s+%s\b.*$" % re.escape(fix[1]),
                           r"\1/* #define %s */  /* removed - see patches/apply_patches.py */"
                           % fix[1], text, count=1, flags=re.M)
        else:  # value
            text = re.sub(r"^(\s*#define\s+%s\s+)\S+" % re.escape(fix[1]),
                           r"\g<1>%s" % fix[2], text, count=1, flags=re.M)

    path.write_text(text, encoding="utf-8")
    return label, "APPLIED", "; ".join(problems)


# --- Main -----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                     help="dry run: report what would happen, change nothing")
    args = ap.parse_args()

    root = repo_root()
    patch_files = sorted(PATCHES_DIR.glob("*.patch"))
    if not patch_files:
        print(f"No .patch files found in {PATCHES_DIR}")
        return 1

    rows = []
    # stdarg.h fixes run first: telnet.patch's first hunk sits right next to
    # telnet.c's stdarg.h include (adds sys_time.h right after it), so if that
    # recurring MCC bug (item 6) has struck again, telnet.patch's context
    # won't match until stdarg.h is back - apply that fix before attempting
    # the .patch files, not after.
    # The wolfSSL/TLS contract in configuration.h is checked alongside them:
    # it is not a .patch either, for the reason given at TLS_CONFIG_PATH.
    rows.append(apply_tls_config_fix(root, args.check))

    for label, rel_path, anchor in STDARG_FIXES:
        rows.append(apply_stdarg_fix(root, label, rel_path, anchor, args.check))
    for patch_path in patch_files:
        rows.append(apply_one_patch(root, patch_path, args.check))

    icon = {"OK": "[ok]", "APPLIED": "[+]", "WOULD APPLY": "[?]", "FAILED": "[!]"}
    print(f"\n{'patch':<28} {'status':<12} detail")
    print("-" * 90)
    failed = 0
    pending = 0
    applied = 0
    for name, status, detail in rows:
        print(f"{icon.get(status, ' ')} {name:<26} {status:<12} {detail}")
        if status == "FAILED":
            failed += 1
        elif status == "WOULD APPLY":
            pending += 1
        elif status == "APPLIED":
            applied += 1

    print()
    if args.check:
        print("Dry run - nothing was changed. Re-run without --check to apply.")
    if failed:
        print(f"{failed} patch(es) need manual attention - see docs/mcc-generated-code-patches.md")
        return 1
    if pending:
        print(f"{pending} patch(es) missing - re-run without --check to apply.")
        return 1
    if applied:
        print(f"{applied} patch(es) applied.")
        return 0
    print("All patches present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
