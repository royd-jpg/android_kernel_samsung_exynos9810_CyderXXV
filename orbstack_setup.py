#!/usr/bin/env python3
"""
orbstack_setup.py — Bootstraps an OrbStack Ubuntu VM for kernel building.

Run this ONCE from your macOS host terminal after creating an OrbStack machine:
    orb create ubuntu:22.04 kernel-builder
    python3 orbstack_setup.py

It will:
  1. Install all required build packages inside the OrbStack VM
  2. Copy build.py and flash.py into the VM
  3. Print the commands to kick off a build

Requirements on macOS:
  - OrbStack installed  (https://orbstack.dev)
  - Python 3.9+
"""

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT     = Path(__file__).parent.resolve()
VM_NAME  = "kernel-builder"       # OrbStack machine name
VM_USER  = "ubuntu"               # Default OrbStack Ubuntu user
VM_DIR   = f"/home/{VM_USER}/exynos9810-ksu-builder"

# Packages to install inside the VM
APT_PACKAGES = [
    # Core build tools
    "bc", "bison", "build-essential", "ca-certificates", "curl", "flex",
    "git", "libssl-dev", "libelf-dev", "lz4", "make", "ninja-build",
    "patch", "python3", "python3-pip", "unzip", "wget", "zip",
    # LLVM / Clang toolchain
    "llvm", "lld", "clang",
    # GCC cross-compilers (fallback / assembler)
    "gcc-aarch64-linux-gnu", "gcc-arm-linux-gnueabi",
    "binutils-aarch64-linux-gnu", "binutils-arm-linux-gnueabi",
    # Android tools
    "adb",
]

SCRIPTS_TO_COPY = ["build.py", "flash.py"]


def run_host(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"[host] $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


def orb(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command inside the OrbStack VM."""
    full = ["orb", "run", "-m", VM_NAME, "--", "bash", "-c", cmd]
    print(f"[vm]   $ {cmd}")
    return subprocess.run(full, check=check)


def check_orbstack() -> None:
    if not shutil.which("orb"):
        print("ERROR: 'orb' not found. Install OrbStack: https://orbstack.dev")
        sys.exit(1)


def check_vm_exists() -> bool:
    r = subprocess.run(
        ["orb", "list"],
        capture_output=True, text=True, check=False
    )
    return VM_NAME in r.stdout


def create_vm() -> None:
    print(f"Creating OrbStack VM '{VM_NAME}' (Ubuntu 22.04)…")
    run_host(["orb", "create", "ubuntu:22.04", VM_NAME])


def install_packages() -> None:
    print("\n━━━  Installing build packages  ━━━")
    pkgs = " ".join(APT_PACKAGES)
    orb(f"sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends {pkgs}")


def copy_scripts() -> None:
    print("\n━━━  Copying build scripts into VM  ━━━")
    orb(f"mkdir -p {VM_DIR}")
    for script in SCRIPTS_TO_COPY:
        src = ROOT / script
        if not src.exists():
            print(f"  WARNING: {script} not found at {src}, skipping.")
            continue
        # Use orb cp to copy files into the VM
        run_host(["orb", "cp", str(src), f"{VM_NAME}:{VM_DIR}/{script}"])
        orb(f"chmod +x {VM_DIR}/{script}")
        print(f"  Copied {script} → {VM_DIR}/{script}")


def print_next_steps() -> None:
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  OrbStack VM ready!                                          ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print("║                                                              ║")
    print("║  To build (from macOS):                                      ║")
    print("║    orb run -m kernel-builder -- bash -c \\                   ║")
    print(f"║      'cd {VM_DIR} && python3 build.py --device starlte'  ║")
    print("║                                                              ║")
    print("║  Or enter the VM shell first:                                ║")
    print("║    orb shell kernel-builder                                  ║")
    print(f"║    cd {VM_DIR}                                     ║")
    print("║    python3 build.py --device starlte                        ║")
    print("║    python3 build.py --device star2lte                       ║")
    print("║    python3 build.py --device crownlte                       ║")
    print("║                                                              ║")
    print("║  To flash after building (from macOS, device via USB):      ║")
    print("║    python3 flash.py --method twrp    # TWRP sideload        ║")
    print("║    python3 flash.py --method heimdall # Download Mode       ║")
    print("║                                                              ║")
    print("╚══════════════════════════════════════════════════════════════╝")


def main() -> None:
    print("═══════════════════════════════════════════")
    print(" OrbStack VM Setup — Exynos9810 Kernel Builder")
    print("═══════════════════════════════════════════")

    check_orbstack()

    if not check_vm_exists():
        create_vm()
    else:
        print(f"VM '{VM_NAME}' already exists — skipping creation.")

    install_packages()
    copy_scripts()
    print_next_steps()


if __name__ == "__main__":
    main()
