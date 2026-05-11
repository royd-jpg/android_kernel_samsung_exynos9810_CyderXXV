#!/usr/bin/env python3
"""
fix_build.py — drop this file next to build.py in the builder repo
and run it once before (or instead of) triggering the CI build.

Fixes:
  1. ksu_hook.h not found  — adds ccflags line to kernel's fs/Makefile
  2. SUSFS patch swallowed — patches build.py to validate the patch file
  3. Checks the SUSFS patch URL is actually reachable and valid

Usage:
  python3 fix_build.py                        # auto-detects paths
  python3 fix_build.py --kernel /path/to/src  # override kernel source path
"""

import argparse
import shutil
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

def info(msg):  print(f"{GREEN}[INFO]{RESET}  {msg}")
def warn(msg):  print(f"{YELLOW}[WARN]{RESET}  {msg}")
def err(msg):   print(f"{RED}[ERR]{RESET}   {msg}", file=sys.stderr)

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).resolve().parent
DEFAULT_BUILD = SCRIPT_DIR / "build.py"
DEFAULT_KERNEL = SCRIPT_DIR / "workspace" / "kernel"

SUSFS_URL = (
    "https://raw.githubusercontent.com/galaxybuild-project/tools"
    "/refs/heads/main/Patches/0001susfs157forksunext.patch"
)

# ═══════════════════════════════════════════════════════════════════════════════
# FIX 1 — add KernelSU include path to fs/Makefile
# ═══════════════════════════════════════════════════════════════════════════════
KSU_CCFLAG = "ccflags-y += -I$(srctree)/drivers/kernelsu"

def fix_fs_makefile(kernel_dir: Path) -> bool:
    makefile = kernel_dir / "fs" / "Makefile"

    if not makefile.exists():
        err(f"fs/Makefile not found at '{makefile}'")
        err("Make sure the kernel source is cloned before running this script.")
        return False

    text = makefile.read_text()

    if KSU_CCFLAG in text:
        warn("fs/Makefile already has the ccflags line — skipping Fix 1.")
        return True

    lines = text.splitlines(keepends=True)

    # Insert after the last existing ccflags-y line, or at line 1 if none
    insert_at = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("ccflags-y"):
            insert_at = i + 1

    lines.insert(insert_at, KSU_CCFLAG + "\n")
    makefile.write_text("".join(lines))
    info(f"fs/Makefile — inserted at line {insert_at + 1}:")
    info(f"  {KSU_CCFLAG}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 2 — patch build.py to validate the SUSFS patch file before declaring ✅
# ═══════════════════════════════════════════════════════════════════════════════
GUARD_TOKEN = "# SUSFS_GUARD_APPLIED"

VALIDATION_CODE = '''\
# SUSFS_GUARD_APPLIED
# ---------- SUSFS patch validation (injected by fix_build.py) ----------
_susfs_patch = workspace / "susfs.patch"
if not _susfs_patch.exists():
    log.error("SUSFS patch file missing — download likely failed.")
    raise SystemExit(1)
if _susfs_patch.stat().st_size < 100:
    log.error(f"SUSFS patch file is only {_susfs_patch.stat().st_size} bytes — "
              "probably an HTTP error page, not a real patch.")
    raise SystemExit(1)
_head = _susfs_patch.read_text(errors="replace")[:512]
if "diff --git" not in _head and "\\n---" not in _head:
    log.error("SUSFS patch file does not look like a valid unified diff "
              "(got an HTTP error page instead?).")
    log.error(f"First 200 chars: {_head[:200]!r}")
    raise SystemExit(1)
# ---------- end validation ----------
'''

SUCCESS_MARKER = 'log.info("✅  SUSFS patch applied.")'


def fix_build_py(build_py: Path) -> bool:
    if not build_py.exists():
        warn(f"build.py not found at '{build_py}' — skipping Fix 2.")
        return True

    src = build_py.read_text()

    if GUARD_TOKEN in src:
        warn("build.py is already patched — skipping Fix 2.")
        return True

    if SUCCESS_MARKER not in src:
        warn("Could not find the SUSFS success marker in build.py.")
        warn("The file structure may have changed — apply Fix 2 manually:")
        warn("  Add patch-file validation before the '✅ SUSFS patch applied' log line.")
        return True

    # Back up first
    backup = build_py.with_suffix(".py.bak")
    shutil.copy2(build_py, backup)
    info(f"build.py backed up → {backup.name}")

    patched = src.replace(SUCCESS_MARKER, VALIDATION_CODE + SUCCESS_MARKER, 1)
    build_py.write_text(patched)
    info("build.py — SUSFS validation block injected successfully.")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 3 — verify the SUSFS patch URL returns a real patch
# ═══════════════════════════════════════════════════════════════════════════════
def check_susfs_url() -> None:
    info(f"Checking SUSFS patch URL …")
    try:
        req = urllib.request.Request(SUSFS_URL, headers={"User-Agent": "fix_build.py/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(4096).decode("utf-8", errors="replace")

        if "diff --git" in data or "\n---" in data:
            info(f"SUSFS URL is reachable and looks like a valid patch ✓")
        else:
            warn("SUSFS URL returned data but it doesn't look like a patch!")
            warn(f"First 200 chars: {data[:200]!r}")
            warn("You may need to update the URL in build.py.")

    except urllib.error.HTTPError as e:
        warn(f"SUSFS URL returned HTTP {e.code} — patch download will fail at build time.")
    except urllib.error.URLError as e:
        warn(f"Could not reach SUSFS URL ({e.reason}) — check network or update the URL.")


# ═══════════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kernel", type=Path, default=DEFAULT_KERNEL,
                        help=f"Path to kernel source (default: {DEFAULT_KERNEL})")
    parser.add_argument("--build-py", type=Path, default=DEFAULT_BUILD,
                        help=f"Path to build.py (default: {DEFAULT_BUILD})")
    args = parser.parse_args()

    print("=" * 65)
    print(" fix_build.py")
    print(f" Kernel source : {args.kernel}")
    print(f" build.py      : {args.build_py}")
    print("=" * 65)

    ok1 = fix_fs_makefile(args.kernel)
    ok2 = fix_build_py(args.build_py)
    check_susfs_url()

    print()
    print("=" * 65)
    if ok1 and ok2:
        info("All fixes applied. Next steps:")
        print()
        print("  1. Commit the fs/Makefile change to your kernel source branch:")
        print(f"       cd {args.kernel}")
        print("       git add fs/Makefile")
        print('       git commit -m "fs: add KernelSU include path for ksu_hook.h"')
        print("       git push")
        print()
        print("  2. Re-run the GitHub Actions workflow.")
    else:
        err("One or more fixes could not be applied — see warnings above.")
        sys.exit(1)
    print("=" * 65)


if __name__ == "__main__":
    main()
