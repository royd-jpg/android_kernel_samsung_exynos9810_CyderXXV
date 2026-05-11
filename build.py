#!/usr/bin/env python3
"""
Exynos 9810 Kernel Builder — KernelSU-Next + SUSFS
Supports: Samsung Galaxy S9 (starlte), S9+ (star2lte), Note9 (crownlte)
Targets : OrbStack (local Linux) and GitHub Actions CI

Usage (OrbStack / any Linux):
    python3 build.py --device starlte
    python3 build.py --device star2lte --clean
    python3 build.py --device crownlte --skip-patch   # if already patched

Usage (GitHub Actions):  triggered via workflow, same flags via env vars
"""

import argparse
import json
import logging
import multiprocessing
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from datetime import datetime
from pathlib import Path

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kernel-builder")

# ─── Paths ─────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent.resolve()
WORK_DIR    = ROOT / "workspace"
KERNEL_DIR  = WORK_DIR / "kernel"
TOOLCHAIN   = WORK_DIR / "toolchain"
CLANG_DIR   = TOOLCHAIN / "clang"
GCC64_DIR   = TOOLCHAIN / "gcc64"
GCC32_DIR   = TOOLCHAIN / "gcc32"
OUT_DIR     = WORK_DIR / "out"
DIST_DIR    = ROOT / "dist"
AK3_DIR     = WORK_DIR / "AnyKernel3"

# ─── Device table ──────────────────────────────────────────────────────────────
DEVICES = {
    "starlte": {
        "name":      "Galaxy S9",
        "defconfig": "exynos9810-starlte_defconfig",
        "dtb_path":  "arch/arm64/boot/dts/exynos/exynos9810-starlte*.dtb",
    },
    "star2lte": {
        "name":      "Galaxy S9+",
        "defconfig": "exynos9810-star2lte_defconfig",
        "dtb_path":  "arch/arm64/boot/dts/exynos/exynos9810-star2lte*.dtb",
    },
    "crownlte": {
        "name":      "Galaxy Note9",
        "defconfig": "exynos9810-crownlte_defconfig",
        "dtb_path":  "arch/arm64/boot/dts/exynos/exynos9810-crownlte*.dtb",
    },
}

# ─── Toolchain URLs ────────────────────────────────────────────────────────────
# Using LineageOS prebuilt toolchains (proven to work with this kernel tree)
TOOLCHAIN_URLS = {
    "clang": {
        "url":  "https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/refs/heads/main/clang-r416183b.tar.gz",
        # Fallback: ZyC clang (smaller, faster download for CI)
        "fallback": "https://github.com/ZyCromerZ/Clang/releases/download/18.0.0git-20240101-release/Clang-18.0.0git-20240101.tar.gz",
    },
    "gcc64": {
        "url": "https://github.com/LineageOS/android_prebuilts_gcc_linux-x86_aarch64_aarch64-linux-android-4.9/archive/refs/heads/lineage-19.1.tar.gz",
    },
    "gcc32": {
        "url": "https://github.com/LineageOS/android_prebuilts_gcc_linux-x86_arm_arm-linux-androideabi-4.9/archive/refs/heads/lineage-19.1.tar.gz",
    },
}

KERNEL_REPO   = "https://github.com/Cyderxxv/android_kernel_samsung_exynos9810.git"
KERNEL_BRANCH = "lineage_ksu"
KSUN_SETUP    = "https://raw.githubusercontent.com/rifsxd/KernelSU-Next/next/kernel/setup.sh"
SUSFS_PATCH   = "https://raw.githubusercontent.com/galaxybuild-project/tools/refs/heads/main/Patches/0001susfs157forksunext.patch"
AK3_REPO      = "https://github.com/osm0sis/AnyKernel3.git"


# ─── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, streaming its output live."""
    merged_env = {**os.environ, **(env or {})}
    log.info("$ %s", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, cwd=cwd, env=merged_env, check=check)
    return result


def run_shell(script: str, cwd: Path | None = None,
              env: dict | None = None) -> None:
    """Run a shell script string."""
    merged_env = {**os.environ, **(env or {})}
    subprocess.run(script, shell=True, cwd=cwd, env=merged_env, check=True,
                   executable="/bin/bash")


def download(url: str, dest: Path) -> None:
    """Download a file with a simple progress indicator."""
    log.info("Downloading %s → %s", url, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _reporthook(count, block_size, total_size):
        if total_size > 0:
            pct = count * block_size * 100 // total_size
            print(f"\r  {pct:3d}%", end="", flush=True)

    urllib.request.urlretrieve(url, dest, reporthook=_reporthook)
    print()  # newline after progress


def extract_tar(archive: Path, dest: Path, strip: int = 0) -> None:
    """Extract a tar archive, optionally stripping leading path components."""
    dest.mkdir(parents=True, exist_ok=True)
    log.info("Extracting %s → %s", archive.name, dest)
    with tarfile.open(archive) as tf:
        if strip == 0:
            tf.extractall(dest)
        else:
            members = tf.getmembers()
            for m in members:
                parts = Path(m.name).parts
                if len(parts) > strip:
                    m.name = str(Path(*parts[strip:]))
                    tf.extract(m, dest)


def git_clone(url: str, dest: Path, branch: str | None = None,
              depth: int = 1) -> None:
    cmd = ["git", "clone", "--depth", str(depth)]
    if branch:
        cmd += ["-b", branch]
    cmd += [url, str(dest)]
    run(cmd)


def nproc() -> int:
    return multiprocessing.cpu_count()


# ─── Step 1: Toolchains ────────────────────────────────────────────────────────

def setup_clang() -> Path:
    """Download and extract Clang. Returns bin/ directory."""
    bin_dir = CLANG_DIR / "bin"
    if (bin_dir / "clang").exists():
        log.info("Clang already present, skipping download.")
        return bin_dir

    # Try ZyC clang first (smaller tar, reliable for this kernel era)
    archive = TOOLCHAIN / "clang.tar.gz"
    try:
        download(TOOLCHAIN_URLS["clang"]["fallback"], archive)
        extract_tar(archive, CLANG_DIR, strip=0)
    except Exception as e:
        log.warning("ZyC clang download failed (%s), trying AOSP clang…", e)
        archive.unlink(missing_ok=True)
        download(TOOLCHAIN_URLS["clang"]["url"], archive)
        extract_tar(archive, CLANG_DIR, strip=0)
    finally:
        archive.unlink(missing_ok=True)

    # Some tarballs wrap inside a directory — unwrap if needed
    subdirs = [d for d in CLANG_DIR.iterdir() if d.is_dir()]
    if subdirs and not (CLANG_DIR / "bin").exists():
        for item in subdirs[0].iterdir():
            shutil.move(str(item), CLANG_DIR)
        subdirs[0].rmdir()

    return bin_dir


def setup_gcc(which: str) -> Path:
    """Download and extract GCC (gcc64 or gcc32). Returns bin/ directory."""
    gcc_dir = GCC64_DIR if which == "gcc64" else GCC32_DIR
    prefix  = "aarch64-linux-android-" if which == "gcc64" else "arm-linux-androideabi-"
    bin_dir = gcc_dir / "bin"

    if any(bin_dir.glob(f"{prefix}gcc")):
        log.info("GCC (%s) already present, skipping.", which)
        return bin_dir

    url     = TOOLCHAIN_URLS[which]["url"]
    archive = TOOLCHAIN / f"{which}.tar.gz"
    download(url, archive)
    extract_tar(archive, gcc_dir, strip=1)
    archive.unlink(missing_ok=True)
    return bin_dir


def setup_toolchains() -> dict:
    """Returns dict of env vars / paths for the build."""
    log.info("━━━  Setting up toolchains  ━━━")
    clang_bin = setup_clang()
    gcc64_bin = setup_gcc("gcc64")
    gcc32_bin = setup_gcc("gcc32")

    path_prefix = f"{clang_bin}:{gcc64_bin}:{gcc32_bin}:{os.environ.get('PATH', '')}"
    return {
        "clang_bin": clang_bin,
        "gcc64_bin": gcc64_bin,
        "gcc32_bin": gcc32_bin,
        "PATH":      path_prefix,
    }


# ─── Step 2: Kernel source ─────────────────────────────────────────────────────

def setup_kernel_source() -> None:
    log.info("━━━  Cloning kernel source  ━━━")
    if KERNEL_DIR.exists() and (KERNEL_DIR / "Makefile").exists():
        log.info("Kernel source already cloned — pulling latest…")
        run(["git", "pull"], cwd=KERNEL_DIR)
        return
    git_clone(KERNEL_REPO, KERNEL_DIR, branch=KERNEL_BRANCH, depth=1)


# ─── Step 3: Apply KernelSU-Next ───────────────────────────────────────────────

def apply_ksun() -> None:
    log.info("━━━  Applying KernelSU-Next  ━━━")

    ksun_marker = KERNEL_DIR / "KernelSU-Next" / "kernel" / "ksu.c"
    if ksun_marker.exists():
        log.info("KernelSU-Next already applied, skipping.")
        return

    # The setup.sh script from rifsxd/KernelSU-Next automatically:
    #  • Clones KernelSU-Next into ./KernelSU-Next/
    #  • Applies the kernel hook patches
    #  • Works for non-GKI kernels >= 4.4
    setup_script = WORK_DIR / "ksun_setup.sh"
    download(KSUN_SETUP, setup_script)

    run_shell(f"bash {setup_script}", cwd=KERNEL_DIR)
    setup_script.unlink(missing_ok=True)
    log.info("✅  KernelSU-Next applied.")


# ─── Step 4: Apply SUSFS ───────────────────────────────────────────────────────

def apply_susfs() -> None:
    log.info("━━━  Applying SUSFS patch  ━━━")

    # For non-GKI (< 5.10) kernels: the SUSFS patch targets the KernelSU-Next
    # subdirectory, not the top-level kernel tree.
    ksun_dir    = KERNEL_DIR / "KernelSU-Next"
    susfs_marker = ksun_dir / "kernel" / "susfs.c"
    if susfs_marker.exists():
        log.info("SUSFS already applied, skipping.")
        return

    patch_file = WORK_DIR / "susfs.patch"
    download(SUSFS_PATCH, patch_file)

    # Apply the patch inside the KernelSU-Next directory
    try:
        run(["patch", "-p1", "--input", str(patch_file)], cwd=ksun_dir)
    except subprocess.CalledProcessError:
        log.warning("SUSFS patch did not apply cleanly — attempting with --forward…")
        run(["patch", "-p1", "--forward", "--input", str(patch_file)],
            cwd=ksun_dir, check=False)

    patch_file.unlink(missing_ok=True)
    log.info("✅  SUSFS patch applied.")


# ─── Step 5: Enable SUSFS in Kconfig ──────────────────────────────────────────

SUSFS_CONFIGS = [
    "CONFIG_KSU=y",
    "CONFIG_KSU_SUSFS=y",
    "CONFIG_KSU_SUSFS_SUS_PATH=y",
    "CONFIG_KSU_SUSFS_SUS_MOUNT=y",
    "CONFIG_KSU_SUSFS_SUS_KSTAT=y",
    "CONFIG_KSU_SUSFS_SUS_OVERLAYFS=n",  # not needed for non-GKI
    "CONFIG_KSU_SUSFS_TRY_UMOUNT=y",
    "CONFIG_KSU_SUSFS_SPOOF_UNAME=y",
    "CONFIG_KSU_SUSFS_ENABLE_LOG=y",
    "CONFIG_KSU_SUSFS_OPEN_REDIRECT=y",
]


def patch_defconfig(device: str) -> None:
    """Append KSU / SUSFS config options to the device defconfig."""
    log.info("━━━  Patching defconfig  ━━━")
    defconfig_path = KERNEL_DIR / "arch" / "arm64" / "configs" / DEVICES[device]["defconfig"]

    if not defconfig_path.exists():
        log.error("defconfig not found: %s", defconfig_path)
        sys.exit(1)

    existing = defconfig_path.read_text()

    new_lines = []
    for cfg in SUSFS_CONFIGS:
        key = cfg.split("=")[0]
        if key not in existing:
            new_lines.append(cfg)

    if new_lines:
        with defconfig_path.open("a") as f:
            f.write("\n# KernelSU-Next + SUSFS\n")
            f.write("\n".join(new_lines) + "\n")
        log.info("Added %d config options.", len(new_lines))
    else:
        log.info("defconfig already has KSU/SUSFS options.")


# ─── Step 6: Build ─────────────────────────────────────────────────────────────

def build_kernel(device: str, toolchains: dict) -> Path:
    log.info("━━━  Building kernel for %s (%s)  ━━━",
             DEVICES[device]["name"], device)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    make_env = {
        "PATH":               toolchains["PATH"],
        "ARCH":               "arm64",
        "SUBARCH":            "arm64",
        "CC":                 "clang",
        "CLANG_TRIPLE":       "aarch64-linux-gnu-",
        "CROSS_COMPILE":      "aarch64-linux-android-",
        "CROSS_COMPILE_ARM32": "arm-linux-androideabi-",
        "LD":                 "ld.lld",
        "NM":                 "llvm-nm",
        "OBJCOPY":            "llvm-objcopy",
        "OBJDUMP":            "llvm-objdump",
        "STRIP":              "llvm-strip",
    }
    make_env = {**os.environ, **make_env, "PATH": toolchains["PATH"]}

    base_make = [
        "make",
        f"-j{nproc()}",
        f"O={OUT_DIR}",
        "ARCH=arm64",
        "CC=clang",
        "CLANG_TRIPLE=aarch64-linux-gnu-",
        "CROSS_COMPILE=aarch64-linux-android-",
        "CROSS_COMPILE_ARM32=arm-linux-androideabi-",
        "LD=ld.lld",
        "NM=llvm-nm",
        "OBJCOPY=llvm-objcopy",
    ]

    # Generate .config from defconfig
    run(base_make + [DEVICES[device]["defconfig"]], cwd=KERNEL_DIR, env=make_env)

    # Compile
    run(base_make, cwd=KERNEL_DIR, env=make_env)

    image = OUT_DIR / "arch" / "arm64" / "boot" / "Image"
    if not image.exists():
        log.error("Build failed — Image not found at %s", image)
        sys.exit(1)

    log.info("✅  Kernel built: %s", image)
    return image


# ─── Step 7: Package with AnyKernel3 ──────────────────────────────────────────

def package_anykernel3(device: str, image: Path) -> Path:
    log.info("━━━  Packaging with AnyKernel3  ━━━")

    if not AK3_DIR.exists():
        git_clone(AK3_REPO, AK3_DIR, depth=1)

    # Copy kernel Image
    shutil.copy2(image, AK3_DIR / "Image")

    # Copy any DTB files (Exynos kernels may embed dtb in Image already;
    # copy if present for safety)
    dtb_dest = AK3_DIR / "dtb"
    dtb_dest.mkdir(exist_ok=True)
    for dtb in (OUT_DIR / "arch" / "arm64" / "boot" / "dts" / "exynos").glob("*.dtb"):
        shutil.copy2(dtb, dtb_dest)

    # Write AnyKernel3 config
    ak3_cfg = AK3_DIR / "anykernel.sh"
    ak3_cfg.write_text(f"""# AnyKernel3 — generated by build.py
properties() {{
kernel.string=Exynos9810 KSU-Next+SUSFS by build.py
do.devicecheck=1
do.modules=0
do.systemless=1
do.cleanup=1
do.cleanuponabort=0
device.name1={device}
device.name2=
supported.versions=
}}

### DO NOT CHANGE BEYOND THIS POINT ###
# shell variables
block=/dev/block/platform/13500000.dwmmc0/by-name/BOOT;
is_slot_device=0;
ramdisk_compression=auto;

. tools/ak3-core.sh;

# boot image
split_boot;

flash_boot;
""")

    # Build zip name
    timestamp  = datetime.now().strftime("%Y%m%d-%H%M")
    zip_name   = f"Exynos9810-{device}-KSUN-SUSFS-{timestamp}.zip"
    zip_path   = DIST_DIR / zip_name
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # Zip up AnyKernel3
    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in AK3_DIR.rglob("*"):
            if f.is_file() and ".git" not in f.parts:
                zf.write(f, f.relative_to(AK3_DIR))

    log.info("✅  AnyKernel3 zip: %s", zip_path)
    return zip_path


# ─── Step 8: Write build metadata ─────────────────────────────────────────────

def write_metadata(device: str, zip_path: Path) -> None:
    meta = {
        "device":      device,
        "device_name": DEVICES[device]["name"],
        "kernel_repo": KERNEL_REPO,
        "branch":      KERNEL_BRANCH,
        "ksun":        "KernelSU-Next (rifsxd)",
        "susfs":       "v1.5.7+ (galaxybuild-project patch)",
        "built_at":    datetime.utcnow().isoformat() + "Z",
        "artifact":    str(zip_path.name),
    }
    meta_path = DIST_DIR / "build_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    log.info("Build metadata → %s", meta_path)


# ─── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build Exynos9810 kernel with KernelSU-Next + SUSFS"
    )
    p.add_argument(
        "--device", "-d",
        choices=list(DEVICES.keys()),
        default=os.environ.get("DEVICE", "starlte"),
        help="Target device codename (default: starlte / $DEVICE env var)",
    )
    p.add_argument(
        "--clean", action="store_true",
        help="Wipe OUT_DIR before building",
    )
    p.add_argument(
        "--skip-patch", action="store_true",
        help="Skip KSU/SUSFS patching (if already applied)",
    )
    p.add_argument(
        "--skip-toolchain", action="store_true",
        help="Skip toolchain download if already in PATH",
    )
    p.add_argument(
        "--only-patch", action="store_true",
        help="Apply patches only, do not compile",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    device = args.device

    log.info("═══════════════════════════════════════════════════")
    log.info(" Exynos 9810 Kernel Builder  ·  Device: %s (%s)",
             device, DEVICES[device]["name"])
    log.info("═══════════════════════════════════════════════════")

    # Guard: must run on Linux
    if platform.system() != "Linux":
        log.error("This script must run on Linux (OrbStack VM or GitHub Actions).")
        log.error("On macOS: start your OrbStack VM and run this inside it.")
        sys.exit(1)

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Source ─────────────────────────────────────────────────────────────
    setup_kernel_source()

    # ── 2. Patches ───────────────────────────────────────────────────────────
    if not args.skip_patch:
        apply_ksun()
        apply_susfs()
        patch_defconfig(device)
    else:
        log.info("Skipping patching (--skip-patch).")

    if args.only_patch:
        log.info("Patching complete. Exiting (--only-patch).")
        return

    # ── 3. Toolchains ─────────────────────────────────────────────────────────
    if args.skip_toolchain:
        log.info("Using system toolchains (--skip-toolchain).")
        toolchains = {"PATH": os.environ["PATH"]}
    else:
        toolchains = setup_toolchains()

    # ── 4. Clean ──────────────────────────────────────────────────────────────
    if args.clean and OUT_DIR.exists():
        log.info("Cleaning out/ …")
        shutil.rmtree(OUT_DIR)

    # ── 5. Build ──────────────────────────────────────────────────────────────
    image = build_kernel(device, toolchains)

    # ── 6. Package ────────────────────────────────────────────────────────────
    zip_path = package_anykernel3(device, image)

    # ── 7. Metadata ───────────────────────────────────────────────────────────
    write_metadata(device, zip_path)

    log.info("")
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║  BUILD COMPLETE                                  ║")
    log.info("║  Flashable zip: %-34s ║", zip_path.name)
    log.info("║  See dist/ for all artifacts.                   ║")
    log.info("╚══════════════════════════════════════════════════╝")
    log.info("")
    log.info("Flash instructions:")
    log.info("  Option A (TWRP)  — see flash.py --method twrp")
    log.info("  Option B (Heimdall) — see flash.py --method heimdall")


if __name__ == "__main__":
    main()
