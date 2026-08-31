# Hand-patch re-apply kit

Re-applies this project's hand-patches to MCC-generated code (see
`../docs/mcc-generated-code-patches.md` for what each one does and why) after a
`Generate Code` run in MPLAB X reverts them.

## Usage

From anywhere inside the repo:

```
python patches/apply_patches.py --check    # dry run - report only, nothing changed
python patches/apply_patches.py            # apply whatever is missing
```

Or, from Windows/Explorer/Git Bash, `patches\apply_patches.bat` (same arguments,
uses this project's own `.venv` like `..\cli.bat`/`..\flash.bat` do, falls
back to the bare `python` from PATH).

Run `--check` first after every `Generate Code`. A clean report (`All patches
present.`) means nothing to do. Anything else, apply for real, then rebuild and
retest.

## What's in here

- `*.patch` — one unified diff per MCC-generated source file, generated straight
  from this repo's own git history (`git diff <clean-baseline-commit> HEAD -- <file>`),
  not hand-typed. Applied via `git apply`, which matches on the surrounding code
  context, not raw line numbers — if MCC changed something nearby, the apply is
  refused instead of guessing, and the script reports `FAILED` for that file so it
  can be checked and re-applied by hand.
- `apply_patches.py` — the runner. For each `.patch` file: tries a clean apply; if
  that fails, checks whether it's already applied (reverse-apply check); reports
  `FAILED` only if neither works. Also handles the two recurring
  `#include <stdarg.h>` regressions (`drv_lan865x_api.c`, `telnet.c`) as a plain
  idempotent text check-and-insert, since no clean git baseline exists for either
  (see item 6 in `../docs/mcc-generated-code-patches.md`).

## What's deliberately NOT in here

The temporary diagnostic instrumentation in `tc6.c` and `drv_lan865x_api.c`
(`g_tc6DiagEnable`, see `../docs/mcc-generated-code-patches.md`'s last section) is
**not** covered - it's meant to be deleted once the residual RX length offset is
root-caused, not preserved forever. If `Generate Code` wipes it and it's still
needed, re-add it by hand from that doc, or `git show <its commit> -- <file>`.

## Regenerating a patch file after changing a hand-patch

If a hand-patch itself changes (e.g. the DPLL timeout value), regenerate the
corresponding `.patch` from the new committed state:

```
git diff <clean-baseline-commit> HEAD -- <path/to/file> > patches/<name>.patch
```

The baseline commit for each current patch (the last commit where that file was
still pristine MCC output, before any hand-patch to it):

| Patch | Baseline commit |
|---|---|
| `plib_clock.patch` | `68a90d0` |
| `drv_lan865x_h.patch` | `10280ca` |
| `drv_lan865x_api.patch` | `3910e51` |
| `initialization.patch` | `e569c7e` |
| `sys_command.patch` | `a23af6c` |
| `telnet.patch` | `a23af6c` |
| `tcpip_mac_bridge.patch` | `0dc62ac` |
| `tc6-conf.patch` | `fd375c4` |

(`drv_lan865x_api.patch`'s baseline already contains the `stdarg.h` fix for that
file, bundled into that commit - that's why `stdarg.h` is handled separately, not
via this file's patch.)
