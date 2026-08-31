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
        "apps/tcpip_iperf_lan865x/firmware/src/config/default/driver/lan865x/src/dynamic/drv_lan865x_api.c",
        '#include "configuration.h"',
    ),
    (
        "telnet.c stdarg.h",
        "apps/tcpip_iperf_lan865x/firmware/src/config/default/library/tcpip/src/telnet.c",
        '#include "net_pres/pres/net_pres_socketapi.h"',
    ),
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
