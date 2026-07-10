#!/bin/bash
#
# install-fs.sh - install the running bootc image onto /disk.raw using the same
# layout as the ISO kickstart: ESP + ext4 /boot + one btrfs pool "dcos" whose
# root/var/varlog subvolumes share it (zstd), with /var/log capped at 2G on-disk
# via a btrfs qgroup quota. Runs privileged INSIDE the image, which ships the
# partitioning tools (sfdisk, mkfs.*, btrfs). Used by the image-test harness.
set -euo pipefail

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

# console=ttyS0 makes the guest kernel log to the serial port QEMU captures to
# serial.log (default is tty0, which the boot-test can't see). Keep tty0 too so
# a real machine still gets a video console. Ref: bootc.dev kernel-arguments.
bootc install to-filesystem --skip-fetch-check \
  --karg console=tty0 --karg console=ttyS0,115200 \
  --root-ssh-authorized-keys "${KEY}" \
  "${T}"

# Seed the first-boot done flag so firstboot-user-setup skips its interactive
# wizard and the VM reaches multi-user in CI. bootc finalizes ${T} read-only
# once installed, so write straight into the var subvol via its own mount
# rather than through ${T}/var.
mkdir -p /varseed
mount -o subvol=var LABEL=dcos /varseed
mkdir -p /varseed/lib
touch /varseed/lib/firstboot-user-setup.done
umount /varseed
