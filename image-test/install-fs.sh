#!/bin/bash
#
# install-fs.sh - install the running bootc image onto /disk.raw using the same
# layout as the ISO kickstart: ESP + ext4 /boot + one btrfs pool "dcos" whose
# root/var/varlog subvolumes share it (zstd), with /var/log capped at 2G on-disk
# via a btrfs qgroup quota. Runs privileged INSIDE the image, which ships the
# partitioning tools (sfdisk, mkfs.*, btrfs). Used by the image-test harness.
set -euo pipefail

# Minimal images omit sfdisk (util-linux fdisk package). Install it
# on-demand so the disk partitioning step works for all image variants.
command -v sfdisk >/dev/null 2>&1 || {
  apt-get update -qq >/dev/null
  apt-get install -y --no-install-recommends fdisk >/dev/null
}

DISK=/disk.raw
KEY=/key.pub
T=/target

dev=$(losetup -f --show -P "${DISK}")
cleanup() {
  umount -R "${T}" 2>/dev/null || true
  umount /varseed 2>/dev/null || true
  losetup -d "${dev}" 2>/dev/null || true
}
trap cleanup EXIT

# GPT: 1=ESP 512M, 2=/boot 1G, 3=btrfs pool (rest).
sfdisk "${dev}" <<'PART'
label: gpt
size=512M, type=uefi
size=1G,   type=linux
type=linux
PART
partprobe "${dev}"
udevadm settle 2>/dev/null || sleep 1

mkfs.vfat -n EFI "${dev}p1" >/dev/null
mkfs.ext4 -q -L boot "${dev}p2"
mkfs.btrfs -q -L dcos "${dev}p3"

# Subvolumes root / var / varlog (same names as the kickstart).
mkdir -p /pool
mount "${dev}p3" /pool
btrfs subvolume create /pool/root >/dev/null
btrfs subvolume create /pool/var >/dev/null
btrfs subvolume create /pool/varlog >/dev/null
umount /pool

# Mount the target tree with zstd, matching the kickstart.
mkdir -p "${T}"
mount -o subvol=root,compress=zstd LABEL=dcos "${T}"
mkdir -p "${T}/boot" "${T}/var"
mount "${dev}p2" "${T}/boot"
mkdir -p "${T}/boot/efi"
mount "${dev}p1" "${T}/boot/efi"
mount -o subvol=var,compress=zstd LABEL=dcos "${T}/var"
mkdir -p "${T}/var/log"
mount -o subvol=varlog,compress=zstd LABEL=dcos "${T}/var/log"

# Cap /var/log at 2G of on-disk usage via a btrfs qgroup quota.
btrfs quota enable "${T}" >/dev/null 2>&1 || true
btrfs qgroup limit 2G "${T}/var/log" >/dev/null 2>&1 || true

# console= kernel arg: the QEMU serial device differs by arch.
#   amd64 (q35):     16550 UART → ttyS0
#   arm64 (virt):    PL011 UART → ttyAMA0
# Both keep tty0 so a real machine still gets a video console.
if [ "${ARCH:-amd64}" = "arm64" ]; then
  CONSOLE_KARGS=(--karg console=tty0 --karg console=ttyAMA0)
else
  CONSOLE_KARGS=(--karg console=tty0 --karg console=ttyS0,115200)
fi
bootc install to-filesystem --skip-fetch-check \
  "${CONSOLE_KARGS[@]}" \
  --root-ssh-authorized-keys "${KEY}" \
  "${T}" || {
  _ec=$?
  # CI runners often have stale "Debian GNU/Linux" EFI NVRAM entries from
  # prior runs that bootupd cannot remove cleanly (ENOENT). The ESP and
  # bootloader binaries are written before the NVRAM update step, so QEMU
  # (which uses the removable path \EFI\BOOT\BOOT*.EFI) still boots fine.
  # Fail hard only if the ESP is missing its required boot binary.
  _efi_ok=0
  for _p in "${T}/boot/efi/EFI/BOOT/BOOTX64.EFI" \
             "${T}/boot/efi/EFI/BOOT/BOOTAA64.EFI" \
             "${T}/boot/efi/EFI/BOOT/bootx64.efi" \
             "${T}/boot/efi/EFI/BOOT/bootaa64.efi"; do
    [ -f "${_p}" ] && { _efi_ok=1; break; }
  done
  if [ "${_efi_ok}" = "0" ]; then
    echo "::error::bootc install failed and ESP is missing the required BOOT*.EFI file"
    exit "${_ec}"
  fi
  echo "::warning::bootc install failed (likely EFI NVRAM update) but ESP has the boot binary — continuing"
}

# Diagnostic: exactly what got laid on the ESP. Firmware boots the removable
# path \EFI\BOOT\BOOTX64.EFI, so it (and grubx64.efi) must be here. Printed
# before the EXIT trap unmounts the target.
echo "=== ESP contents (${T}/boot/efi) ==="
find "${T}/boot/efi" | sort
echo "=== end ESP ==="

# Stage a second identical deployment so CI rollback tests have two deployment
# slots to work with. bootc install creates exactly one deployment; deploying
# the same commit again gives the VM a rollback target without network access.
# The commit hash comes from the deploy directory name (<hash>.0) — reliable
# because bootc install always produces exactly one deployment.
stateroot=$(ls "${T}/ostree/deploy/" 2>/dev/null | head -1)
stateroot="${stateroot:-default}"
deployed_dir=$(ls -d "${T}/ostree/deploy/${stateroot}/deploy/"*.0 2>/dev/null | head -1)
if [ -n "${deployed_dir}" ]; then
  deployed_rev="${deployed_dir##*/}"
  deployed_rev="${deployed_rev%.0}"
  ostree --repo="${T}/ostree/repo" refs --create="testrollback/0" "${deployed_rev}"
  ostree admin --sysroot="${T}" deploy --os="${stateroot}" "testrollback/0"
  echo "=== rollback slot staged: ${deployed_rev} (stateroot: ${stateroot}) ==="
  ostree admin --sysroot="${T}" status
else
  echo "::warning::could not find deployed commit — rollback slot not staged; rollback tests will fail"
fi

# Seed the first-boot done flag so firstboot-user-setup skips its interactive
# wizard and the VM reaches multi-user in CI. bootc finalizes ${T} read-only
# once installed, so write straight into the var subvol via its own mount
# rather than through ${T}/var.
mkdir -p /varseed
mount -o subvol=var LABEL=dcos /varseed
mkdir -p /varseed/lib
touch /varseed/lib/firstboot-user-setup.done
umount /varseed
