#!/bin/bash
# build-img.sh - build a flashable .img disk image from a published bootc
# container image.
#
# Usage: build-img.sh <target> <arch> <img_file> <img_ref>
#   target   : rpi | gallium
#   arch     : amd64 | arm64
#   img_file : output filename (e.g. debian-bootc-rpi-arm64.img)
#   img_ref  : container image ref to pull (e.g. ghcr.io/owner/repo:latest_arm64_autoupdate)
#
# Targets:
#   rpi     - Raspberry Pi bootable .img (arm64 only).
#             GPT with a FAT32 /boot partition (kernel + DTB + firmware blobs
#             + config.txt + cmdline.txt) and an ext4 / root partition laid
#             from the bootc ostree deployment via `bootc install to-
#             filesystem`. The Pi firmware (bootcode.bin/start*.elf/fixup*.dat
#             for Pi 3/4, embedded EEPROM for Pi 5) is fetched at build time
#             from github.com/raspberrypi/firmware/boot.
#   gallium - Linux Gallium raw .img.
#             amd64: GPT with a single ext4 root partition laid from the
#             container image (raw image, no phone-specific config).
#             arm64: phone target — GPT with a raw boot partition holding an
#             Android boot.img (kernel + DTB + initramfs packed with
#             mkbootimg) and an ext4 root partition laid from the bootc
#             ostree deployment. The boot.img is also exported alongside the
#             .img so it can be flashed via `fastboot flash boot boot.img`.
#
# The script expects to run inside a privileged container (almalinux:10) with
# loop mount, sfdisk, mkfs.vfat, mkfs.ext4 and podman available. The script
# pulls and tags the bootc image as local-bootc:latest itself (idempotent).
#
# Returns 0 on success, non-zero on failure. The .img is written to
# ./out/<img_file> (the out/ directory is created if missing).
#
# Idempotent: re-running overwrites the .img. The loop device is cleaned up
# via a trap on EXIT.
#
# Rollback: the script creates no persistent host state outside ./out/. To
# revert a run, delete ./out/<img_file>. The container image tagged
# local-bootc:latest is left in podman's storage (it is a tag, not a new
# image) and is garbage-collected by podman's normal storage reclamation.
set -euo pipefail

TARGET="${1:?target required (rpi|gallium)}"
ARCH="${2:?arch required (amd64|arm64)}"
IMG_FILE="${3:?img_file required}"
IMG_REF="${4:?img_ref required}"

# Sanity-check the target/arch combination. rpi only makes sense on arm64
# (Pi 3/4/5 are aarch64); gallium is valid on both arches (amd64 = raw image,
# arm64 = phone with an Android boot.img). Refuse early rather than building
# an image that can never boot.
if [ "$TARGET" = "rpi" ] && [ "$ARCH" != "arm64" ]; then
  echo "::error::rpi target requires arch=arm64 (got ${ARCH})" >&2
  exit 1
fi

OUT_DIR="out"
mkdir -p "${OUT_DIR}"
IMG_PATH="${OUT_DIR}/${IMG_FILE}"

# Image size: rpi 4G (small OS + FAT32 boot), gallium 8G.
if [ "$TARGET" = "rpi" ]; then
  SIZE_MB=4096
else
  SIZE_MB=8192
fi

# Pull and tag the container image (idempotent: podman pull refreshes, tag is
# a no-op if the tag already points at the same image).
echo ">>> Pulling ${IMG_REF}..."
podman pull "${IMG_REF}"
podman tag "${IMG_REF}" "local-bootc:latest"

# Create a sparse .img file of the requested size (idempotent: truncate
# overwrites if the file exists).
echo ">>> Creating ${IMG_PATH} (${SIZE_MB}M, sparse)..."
truncate -s "${SIZE_MB}M" "${IMG_PATH}"

# Attach to a loop device with partition scanning (-P).
DEV=$(losetup -f --show -P "${IMG_PATH}")
echo ">>> Loop device: ${DEV}"

cleanup() {
  umount -R /target 2>/dev/null || true
  umount /mnt-boot 2>/dev/null || true
  losetup -d "${DEV}" 2>/dev/null || true
}
trap cleanup EXIT

if [ "$TARGET" = "rpi" ]; then
  # ── Raspberry Pi: FAT32 /boot + ext4 / ───────────────────────────────────
  # GPT: p1 = FAT32 /boot (512M, Linux filesystem type GUID), p2 = ext4 /
  # (rest). The FAT32 partition is the only one the Pi firmware reads directly
  # (config.txt, kernel, DTB); the ext4 root holds the ostree deployment that
  # bootc lays.
  echo ">>> Partitioning (rpi: FAT32 /boot 512M + ext4 /)..."
  sfdisk "${DEV}" <<'PART'
label: gpt
size=512M, type=0fc63daf-8483-4772-8e79-3d69d8477de4, name="boot-se"
type=0fc63daf-8483-4772-8e79-3d69d8477de4, name="root"
PART
  partprobe "${DEV}" 2>/dev/null || true
  udevadm settle 2>/dev/null || sleep 1

  mkfs.vfat -n BOOT "${DEV}p1" >/dev/null
  mkfs.ext4 -q -L root "${DEV}p2"

  # Lay the root filesystem from the bootc image. Run inside the image so
  # bootc + ostree are available. --skip-fetch-check because the image is
  # already pulled locally. --karg adds the serial console (Pi UART) and the
  # root label so the kernel mounts the ext4 root on first boot.
  echo ">>> Laying root filesystem via bootc install to-filesystem..."
  mkdir -p /target
  mount "${DEV}p2" /target
  podman run --rm --privileged --pid=host \
    --security-opt label=disable \
    -v /dev:/dev \
    -v /var/lib/containers:/var/lib/containers \
    -v /target:/target \
    local-bootc:latest \
    bootc install to-filesystem --skip-fetch-check \
      --karg console=serial0,115200 \
      --karg root=LABEL=root \
      --karg rw \
      /target

  # Populate the FAT32 /boot with the kernel, DTB and Pi config.
  echo ">>> Populating FAT32 /boot (kernel + DTB + config.txt + cmdline.txt)..."
  mkdir -p /mnt-boot
  mount "${DEV}p1" /mnt-boot

  # Find the deployed kernel inside the ostree tree. bootc installs the kernel
  # at /usr/lib/modules/<ver>/vmlinuz (Debian layout). The same vmlinuz is
  # laid as both kernel8.img (Pi 3/4 firmware loads it directly) and
  # kernel_2712.img (Pi 5 firmware loads it directly — Pi 5 dropped the
  # start*.elf/fixup*.dat stage and boots a raw kernel image named
  # kernel_2712.img).
  KERNEL=$(find /target/usr/lib/modules -maxdepth 2 -name vmlinuz \
    2>/dev/null | sort -V | tail -1)
  if [ -z "$KERNEL" ]; then
    echo "::error::No kernel (vmlinuz) found in the bootc image" >&2
    exit 1
  fi
  cp "$KERNEL" /mnt-boot/kernel8.img
  cp "$KERNEL" /mnt-boot/kernel_2712.img

  # DTBs: copy the broadcom bcm2711 (Pi 4), bcm2712 (Pi 5) and bcm2837 (Pi 3)
  # blobs if present. The Debian arm64 kernel package lays them at
  # /usr/lib/linux-image-<ver>/broadcom/bcm*.dtb.
  DTB_DIR=$(find /target/usr/lib -maxdepth 3 -type d -name broadcom \
    2>/dev/null | sort -V | tail -1)
  if [ -n "$DTB_DIR" ]; then
    mkdir -p /mnt-boot/overlays
    cp "${DTB_DIR}"/bcm*.dtb /mnt-boot/ 2>/dev/null || true
    # Pi overlay DTBs (for HATs) if present.
    cp "${DTB_DIR}"/overlays/*.dtbo /mnt-boot/overlays/ 2>/dev/null || true
  else
    echo "::warning::No broadcom DTB directory found; Pi may not boot without a DTB." >&2
  fi

  # ── Raspberry Pi closed-source firmware blobs ───────────────────────────
  # The Pi firmware is NOT in the Debian bootc image (closed source, Broadcom
  # binary). Fetch the per-model files from the upstream raspberrypi/firmware
  # boot tree at build time:
  #   Pi 3 — start.elf + fixup.dat + bootcode.bin (the bootcode.bin is the Pi 3
  #          ROM stage1; without it the Pi 3 does not chain start.elf).
  #   Pi 4 — start4.elf + fixup4.dat (no bootcode.bin: the Pi 4 EEPROM holds
  #          the stage1 firmware; bootcode.bin is ignored on Pi 4).
  #   Pi 5 — NO .elf/.dat at all: the Pi 5 EEPROM holds the whole bootloader;
  #          it loads kernel_2712.img + the bcm2712 DTB directly.
  # Fetching all five files is harmless — the firmware only loads the files
  # its model needs, the others are dead weight (a few hundred KB total).
  FW_BASE="https://github.com/raspberrypi/firmware/raw/master/boot"
  echo ">>> Fetching Pi firmware blobs from ${FW_BASE}..."
  for fw in bootcode.bin start.elf fixup.dat start4.elf fixup4.dat; do
    echo "    - ${fw}"
    curl -fsSL "${FW_BASE}/${fw}" -o "/mnt-boot/${fw}"
  done

  # config.txt — Pi firmware config. Enable UART (enable_uart=1) and boot in
  # 64-bit mode (arm_64bit=1). Per-model [pi3]/[pi4]/[pi5] sections point the
  # firmware at the right kernel image and (for Pi 3/4) the right start/fixup
  # pair. The firmware picks the section matching the running SoC; the other
  # sections are ignored. The kernel= and arm_64bit=1 keys stay at the top
  # level as the safe defaults for an unknown model.
  cat > /mnt-boot/config.txt <<'EOF'
# Raspberry Pi boot config (generated by build-img.sh).
# UART on the GPIO header (pins 8/10) for serial console + debug.
enable_uart=1
# Boot in 64-bit mode on all models that support it (Pi 3/4/5).
arm_64bit=1
# Default kernel image (used by Pi 3/4 firmware and as a fallback).
kernel=kernel8.img

# ── Pi 3 (BCM2837, aarch64) ───────────────────────────────────────────────
# Pi 3 ROM -> bootcode.bin -> start.elf -> fixup.dat -> kernel8.img.
[pi3]
kernel=kernel8.img
start_file=start.elf
fixup_file=fixup.dat

# ── Pi 4 (BCM2711, aarch64) ───────────────────────────────────────────────
# Pi 4 EEPROM -> start4.elf -> fixup4.dat -> kernel8.img. No bootcode.bin.
[pi4]
kernel=kernel8.img
start_file=start4.elf
fixup_file=fixup4.dat

# ── Pi 5 (BCM2712, aarch64) ───────────────────────────────────────────────
# Pi 5 EEPROM loads kernel_2712.img + the bcm2712 DTB directly. No .elf/.dat
# stage, no kernel8.img. The [pi5] section only sets the kernel name.
[pi5]
kernel=kernel_2712.img
EOF

  # cmdline.txt — kernel command line. Must match the kargs passed to bootc
  # install (root=LABEL=root, serial console). The Pi firmware passes this
  # single line to the kernel.
  printf 'root=LABEL=root rw console=serial0,115200 console=tty1\n' \
    > /mnt-boot/cmdline.txt

  sync
  umount /mnt-boot

else
  # ── gallium ────────────────────────────────────────────────────────────
  # amd64: single ext4 / (raw image). No ESP, no FAT32, no phone config —
  #   just the raw image laid from the container image, ready to flash or
  #   convert. bootc install writes grub to the disk.
  # arm64: phone target — GPT with a raw boot partition (p1, 64M, holds an
  #   Android boot.img) + an ext4 root partition (p2, ostree deployment).
  #   The boot.img is packed with mkbootimg (kernel + DTB + initramfs) and
  #   also exported as a separate artifact so the operator can flash it via
  #   `fastboot flash boot boot.img`. The lk2nd bootloader is expected to be
  #   packed into boot.img by the OS image; this script only packs the
  #   kernel/initramfs layer that lk2nd chain-loads.
  if [ "$ARCH" = "amd64" ]; then
    echo ">>> Partitioning (gallium/amd64: single ext4 /)..."
    sfdisk "${DEV}" <<'PART'
label: gpt
type=0fc63daf-8483-4772-8e79-3d69d8477de4, name="root"
PART
    partprobe "${DEV}" 2>/dev/null || true
    udevadm settle 2>/dev/null || sleep 1

    mkfs.ext4 -q -L root "${DEV}p1"

    echo ">>> Laying root filesystem via bootc install to-filesystem..."
    mkdir -p /target
    mount "${DEV}p1" /target
    podman run --rm --privileged --pid=host \
      --security-opt label=disable \
      -v /dev:/dev \
      -v /var/lib/containers:/var/lib/containers \
      -v /target:/target \
      local-bootc:latest \
      bootc install to-filesystem --skip-fetch-check \
        --karg root=LABEL=root \
        --karg rw \
        /target
  else
    # arm64 phone: p1 = raw boot (64M, Android boot.img), p2 = ext4 root.
    echo ">>> Partitioning (gallium/arm64 phone: raw boot 64M + ext4 /)..."
    sfdisk "${DEV}" <<'PART'
label: gpt
size=64M, type=0fc63daf-8483-4772-8e79-3d69d8477de4, name="boot"
type=0fc63daf-8483-4772-8e79-3d69d8477de4, name="root"
PART
    partprobe "${DEV}" 2>/dev/null || true
    udevadm settle 2>/dev/null || sleep 1

    mkfs.ext4 -q -L root "${DEV}p2"

    # Lay the root filesystem from the bootc image (ostree deployment). The
    # boot partition (p1) is left raw — it is written with the packed boot.img
    # below, not a filesystem.
    echo ">>> Laying root filesystem via bootc install to-filesystem..."
    mkdir -p /target
    mount "${DEV}p2" /target
    podman run --rm --privileged --pid=host \
      --security-opt label=disable \
      -v /dev:/dev \
      -v /var/lib/containers:/var/lib/containers \
      -v /target:/target \
      local-bootc:latest \
      bootc install to-filesystem --skip-fetch-check \
        --karg root=LABEL=root \
        --karg rw \
        --karg console=ttyMSM0,115200 \
        /target

    # ── Pack the Android boot.img (kernel + DTB + initramfs) ───────────
    # mkbootimg packs a boot image from a kernel, an optional initramfs and
    # a DTB. The kernel lives in the ostree deployment at
    # /usr/lib/modules/<ver>/vmlinuz. The initramfs is generated by bootc
    # install at /target/boot/initramfs-<ver>.img (Debian layout). The DTB
    # is at /usr/lib/linux-image-<ver>/<vendor>/<board>.dtb — the phone
    # board DTB is selected by the device tree name baked into the kernel
    # cmdline; here we pass the first DTB found (single-board phone).
    KERNEL=$(find /target/usr/lib/modules -maxdepth 2 -name vmlinuz \
      2>/dev/null | sort -V | tail -1)
    if [ -z "$KERNEL" ]; then
      echo "::error::No kernel (vmlinuz) found in the bootc image" >&2
      exit 1
    fi

    INITRAMFS=$(find /target/boot -maxdepth 1 -name 'initramfs-*.img' \
      2>/dev/null | sort -V | tail -1)
    if [ -z "$INITRAMFS" ]; then
      echo "::error::No initramfs found in /target/boot (bootc install did not lay one)" >&2
      exit 1
    fi

    # DTB: the phone board DTB. Use the first .dtb under the linux-image
    # tree that is NOT a broadcom one (broadcom = RPi). On a phone target the
    # relevant vendor is qcom/sdm845 or similar.
    DTB=$(find /target/usr/lib/linux-image-* -type f -name '*.dtb' \
      2>/dev/null | grep -v '/broadcom/' | sort -V | head -1)
    if [ -z "$DTB" ]; then
      echo "::warning::No phone DTB found; packing boot.img without a DTB." >&2
    fi

    # Pack the boot.img. --pagesize 2048 and --base 0x80000000 are the
    # canonical Android boot image defaults; --cmdline passes the root=
    # label + serial console so the kernel mounts the ext4 root on boot.
    CMDLINE="root=LABEL=root rw console=ttyMSM0,115200"
    BOOT_IMG="${OUT_DIR}/boot-${IMG_FILE%.img}.img"

    echo ">>> Packing Android boot.img with mkbootimg..."
    if [ -n "$DTB" ]; then
      mkbootimg \
        --kernel    "$KERNEL" \
        --ramdisk   "$INITRAMFS" \
        --dtb       "$DTB" \
        --cmdline   "$CMDLINE" \
        --pagesize  2048 \
        --base      0x80000000 \
        --output    "$BOOT_IMG"
    else
      mkbootimg \
        --kernel    "$KERNEL" \
        --ramdisk   "$INITRAMFS" \
        --cmdline   "$CMDLINE" \
        --pagesize  2048 \
        --base      0x80000000 \
        --output    "$BOOT_IMG"
    fi

    # Write the packed boot.img to the raw boot partition (p1) so the .img
    # is self-contained, AND keep the standalone boot.img as a separate
    # artifact for `fastboot flash boot boot.img`.
    echo ">>> Writing boot.img to boot partition ${DEV}p1..."
    dd if="$BOOT_IMG" of="${DEV}p1" bs=1M conv=fsync status=none
  fi
fi

sync
# Detach the loop device (the trap also does this, but we want a clean state
# before the release upload).
umount -R /target 2>/dev/null || true
losetup -d "${DEV}" 2>/dev/null || true
trap - EXIT

# Sparsify the .img so it takes only the used blocks on disk and in the
# release artifact store. cp --sparse=always rewrites the file with holes for
# the zero regions.
echo ">>> Sparsifying ${IMG_PATH}..."
cp --sparse=always "${IMG_PATH}" "${IMG_PATH}.sparse"
mv "${IMG_PATH}.sparse" "${IMG_PATH}"

ls -lh "${IMG_PATH}"
echo ">>> Done: ${IMG_PATH}"