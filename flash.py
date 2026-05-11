#!/usr/bin/env python3
"""
flash.py — Flash the built kernel to a Samsung Exynos 9810 device.

Flashing methods:
  twrp      ADB sideload of AnyKernel3 zip while device is in TWRP recovery
  heimdall  Unpack boot.img from AnyKernel3 zip and flash via Heimdall (Download Mode)
  push      Push zip to /sdcard and trigger TWRP install (device must be booted in TWRP)

Usage:
    python3 flash.py                           # auto-detect latest zip, TWRP sideload
    python3 flash.py --method heimdall         # use Heimdall
    python3 flash.py --zip dist/my.zip         # use specific zip
    python3 flash.py --list-devices            # show connected ADB devices
"""

import argparse
import glob
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("flash")

ROOT     = Path(__file__).parent.resolve()
DIST_DIR = ROOT / "dist"

# ─── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list[str], check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=check,
                          capture_output=capture, text=True)


def require_tool(name: str) -> None:
    if not shutil.which(name):
        log.error("'%s' not found in PATH. Install it first.", name)
        log.error("  macOS: brew install android-platform-tools  (for adb)")
        log.error("         brew install heimdall                (for heimdall)")
        log.error("  Linux: sudo apt install adb heimdall-flash  ")
        sys.exit(1)


def find_latest_zip() -> Path:
    zips = sorted(DIST_DIR.glob("Exynos9810-*.zip"), key=lambda p: p.stat().st_mtime)
    if not zips:
        log.error("No flashable zip found in dist/. Run build.py first.")
        sys.exit(1)
    latest = zips[-1]
    log.info("Using latest zip: %s", latest.name)
    return latest


def wait_for_adb(timeout: int = 60) -> None:
    log.info("Waiting for ADB device (up to %ds)…", timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = run(["adb", "get-state"], check=False, capture=True)
        if r.returncode == 0 and r.stdout.strip() in ("device", "recovery"):
            log.info("ADB device found: %s", r.stdout.strip())
            return
        time.sleep(2)
    log.error("No ADB device found after %ds.", timeout)
    sys.exit(1)


# ─── Method A: TWRP ADB sideload ──────────────────────────────────────────────

def flash_twrp(zip_path: Path) -> None:
    """
    Flash via ADB sideload.  Device must already be in TWRP recovery.
    Steps:
      1. Check device is in recovery mode
      2. adb sideload <zip>
    If the device is not yet in recovery, we try rebooting into it.
    """
    require_tool("adb")
    log.info("━━━  TWRP Sideload  ━━━")
    log.info("Zip: %s", zip_path)

    wait_for_adb()

    state = run(["adb", "get-state"], capture=True, check=False).stdout.strip()
    if state == "device":
        log.info("Device is booted normally — rebooting to recovery…")
        run(["adb", "reboot", "recovery"])
        time.sleep(15)
        wait_for_adb()

    # Verify we are in TWRP
    state = run(["adb", "get-state"], capture=True, check=False).stdout.strip()
    if state != "recovery":
        log.error("Device is not in recovery mode (state=%s).", state)
        log.error("Please manually boot into TWRP and re-run this script.")
        sys.exit(1)

    log.info("Starting sideload…")
    run(["adb", "sideload", str(zip_path)])
    log.info("✅  Sideload complete. Tap 'Reboot System' in TWRP.")


# ─── Method B: Push to /sdcard then TWRP install ──────────────────────────────

def flash_push(zip_path: Path) -> None:
    """Push zip to /sdcard and trigger install via TWRP command file."""
    require_tool("adb")
    log.info("━━━  Push + TWRP Install  ━━━")

    wait_for_adb()

    remote = f"/sdcard/{zip_path.name}"
    log.info("Pushing zip to device…")
    run(["adb", "push", str(zip_path), remote])

    # Write TWRP openrecoveryscript to install the zip
    ors = f"install {remote}\nreboot"
    with tempfile.NamedTemporaryFile("w", suffix=".ors", delete=False) as f:
        f.write(ors)
        ors_path = f.name

    run(["adb", "push", ors_path, "/cache/recovery/openrecoveryscript"])
    os.unlink(ors_path)

    log.info("Rebooting into recovery to install…")
    run(["adb", "reboot", "recovery"])
    log.info("✅  TWRP will install the zip on next boot.")


# ─── Method C: Heimdall (Download Mode) ───────────────────────────────────────

def extract_boot_img(zip_path: Path, tmpdir: Path) -> Path:
    """
    Unpack the AnyKernel3 zip and try to extract or reassemble a boot.img.
    For Exynos 9810 with AnyKernel3, the Image is flashed directly as BOOT.
    We create a minimal placeholder that Heimdall can use; for a proper
    boot.img you should use magiskboot or unpackbootimg from your device.
    """
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmpdir)

    # AnyKernel3 contains Image (raw kernel), not a boot.img.
    # We flash Image directly to the BOOT partition via Heimdall.
    image = tmpdir / "Image"
    if not image.exists():
        log.error("Image not found inside AnyKernel3 zip.")
        sys.exit(1)

    return image


def flash_heimdall(zip_path: Path) -> None:
    """
    Flash via Heimdall in Download Mode.
    Samsung Exynos devices use Download Mode (Vol Down + Bixby/Home + Power)
    rather than fastboot.

    NOTE: Flashing a raw Image to BOOT may not work on all Samsung devices
    if the boot partition uses a custom format.  If it fails, use TWRP sideload.
    """
    require_tool("heimdall")
    log.info("━━━  Heimdall Flash (Download Mode)  ━━━")
    log.info("Zip: %s", zip_path)

    log.info("")
    log.info("  ┌─ MANUAL STEP REQUIRED ──────────────────────────────┐")
    log.info("  │ Put your device into Download Mode:                  │")
    log.info("  │   Power OFF → hold Vol Down + Bixby + Power         │")
    log.info("  │   (or: Vol Down + Home + Power on older models)      │")
    log.info("  │ Then connect USB and press Enter here.               │")
    log.info("  └──────────────────────────────────────────────────────┘")
    input("  Press Enter when device is in Download Mode… ")

    # Verify Heimdall can see the device
    detect = run(["heimdall", "detect"], check=False, capture=True)
    if detect.returncode != 0:
        log.error("Heimdall cannot detect device. Check USB connection and drivers.")
        log.error(detect.stderr)
        sys.exit(1)
    log.info("Device detected by Heimdall.")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        image = extract_boot_img(zip_path, tmp)
        log.info("Flashing Image → BOOT partition…")
        run(["heimdall", "flash", "--BOOT", str(image), "--no-reboot"])

    log.info("")
    log.info("✅  Heimdall flash complete.")
    log.info("   Disconnect USB, hold Power to reboot your device.")


# ─── List devices ─────────────────────────────────────────────────────────────

def list_devices() -> None:
    require_tool("adb")
    print("Connected ADB devices:")
    run(["adb", "devices", "-l"])


# ─── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Flash Exynos9810 KSU-Next+SUSFS kernel"
    )
    p.add_argument(
        "--method", "-m",
        choices=["twrp", "heimdall", "push"],
        default="twrp",
        help="Flash method (default: twrp sideload)",
    )
    p.add_argument(
        "--zip", "-z",
        type=Path,
        default=None,
        help="Path to AnyKernel3 zip (default: latest in dist/)",
    )
    p.add_argument(
        "--list-devices", action="store_true",
        help="List connected ADB devices and exit",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.list_devices:
        list_devices()
        return

    zip_path = args.zip or find_latest_zip()
    if not zip_path.exists():
        log.error("Zip not found: %s", zip_path)
        sys.exit(1)

    log.info("═══════════════════════════════════════════════════")
    log.info(" Exynos 9810 Kernel Flasher")
    log.info(" Method : %s", args.method)
    log.info(" Zip    : %s", zip_path.name)
    log.info("═══════════════════════════════════════════════════")

    if args.method == "twrp":
        flash_twrp(zip_path)
    elif args.method == "heimdall":
        flash_heimdall(zip_path)
    elif args.method == "push":
        flash_push(zip_path)


if __name__ == "__main__":
    main()
