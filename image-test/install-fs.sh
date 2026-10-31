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
#######################################
# Release the mounts and the loop device on exit.
# Globals:
#   T
#   dev
#######################################
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
# Minimal images omit partprobe (parted package). Install it on-demand, after
# refreshing the lists: the image's 99-apt-clean apt.conf.d hook wipes
# /var/lib/apt/lists/* on every apt Post-Invoke, so the lists populated by the
# sfdisk install above are already gone by the time we get here and a bare
# `apt-get install` fails with "Package 'parted' has no installation candidate".
command -v partprobe >/dev/null 2>&1 || {
  apt-get update -qq >/dev/null
  apt-get install -y --no-install-recommends parted >/dev/null
}
partprobe "${dev}"
udevadm settle 2>/dev/null || sleep 1

# Minimal images omit mkfs.vfat (dosfstools package). Install it on-demand,
# refreshing the lists again for the same 99-apt-clean reason as above.
command -v mkfs.vfat >/dev/null 2>&1 || {
  apt-get update -qq >/dev/null
  apt-get install -y --no-install-recommends dosfstools >/dev/null
}

# `bootc install to-filesystem` drives the install through two external tools
# that the minimal image deliberately does not ship:
#   - podman, to create the target's image store. bootc calls
#     CStorage::create unconditionally before deploying, and that spawns
#     `podman images` against the new store; with no podman on PATH the spawn
#     fails and bootc reports "Creating imgstorage: Initializing images:
#     No such file or directory (os error 2)".
#   - skopeo, which is the importer bootc uses to pull the source image into
#     the target ostree repo (deploy::pull_from_prepared). Our bootc package
#     only Recommends it, and the minimal image installs with
#     --no-install-recommends, so it is absent there.
# The ISO installer does not need this: it derives from the full image, which
# carries both. Installing them here keeps the published minimal image as
# designed (debian-bootc wiki docs/minimal.md lists Podman among its
# deliberate omissions) while
# giving the harness the same environment the installer runs in.
if ! command -v podman >/dev/null 2>&1 || ! command -v skopeo >/dev/null 2>&1; then
  apt-get update -qq >/dev/null
  apt-get install -y --no-install-recommends podman skopeo >/dev/null
fi

# The minimal image ships no SSH server (also a documented omission), so the
# harness would have nothing to connect to and every test would fail on the
# wait_ssh timeout. Install it as the harness's transport, in the container
# whose /usr bootc install copies into the target. openssh-server's postinst
# records `ssh` as enabled through deb-systemd-helper, which works without a
# running systemd, so the installed system starts it on boot.
command -v sshd >/dev/null 2>&1 || {
  apt-get update -qq >/dev/null
  apt-get install -y --no-install-recommends openssh-server >/dev/null
}

# bootupd is required by `bootc install to-filesystem` for UEFI boot data
# installation (it places the signed shim + grub into the ESP). The minimal
# image intentionally omits it (it is not needed at runtime); install it
# on-demand here so the harness gets the same bootloader path as the full image.
# bootupd pulls grub-efi-*-signed + shim-signed as dependencies, which provide
# the shim*.efi files bootupd copies into the ESP.
command -v bootupd >/dev/null 2>&1 || {
  apt-get update -qq >/dev/null
  apt-get install -y --no-install-recommends bootupd >/dev/null
}

mkfs.vfat -n EFI "${dev}p1" >/dev/null
mkfs.ext4 -q -L boot "${dev}p2"
mkfs.btrfs -q -f -L dcos "${dev}p3"

# Subvolumes root / var / varlog (same names as the kickstart).
mkdir -p /pool
mount "${dev}p3" /pool
btrfs subvolume create /pool/root >/dev/null
btrfs subvolume create /pool/var >/dev/null
btrfs subvolume create /pool/varlog >/dev/null
umount /pool

# Mount the target tree without compress= so the remount,rw step later
# (needed to stage the rollback slot) does not fail on the CI host kernel.
# Production deployments set compress=zstd via the kickstart/fstab; the
# test harness only needs a functional btrfs tree, not a compressed one.
mkdir -p "${T}"
mount -o subvol=root LABEL=dcos "${T}"
mkdir -p "${T}/boot" "${T}/var"
mount "${dev}p2" "${T}/boot"
mkdir -p "${T}/boot/efi"
mount "${dev}p1" "${T}/boot/efi"
mount -o subvol=var LABEL=dcos "${T}/var"
mkdir -p "${T}/var/log"
mount -o subvol=varlog LABEL=dcos "${T}/var/log"

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

# Extra kernel arguments of the manifest category (validated by the harness):
# one --karg per space-separated INSTALL_KARGS entry.
read -ra _extra_kargs <<<"${INSTALL_KARGS:-}"
EXTRA_KARGS=()
for _k in "${_extra_kargs[@]}"; do
  EXTRA_KARGS+=(--karg "${_k}")
done

# Sandbox EFI NVRAM so bootupd does not write to or read from the host firmware vars.
#
# bootc install to-filesystem does two things that defeat a naive "mount empty tmpfs" trick:
#   1. It unshares its mount namespace (CLONE_NEWNS), copying our mounts into the new ns.
#   2. setup_sys_mount("efivarfs", EFIVARFS) in install.rs then checks:
#      a. /proc/1/root/sys/firmware/efi/efivars (host efivars via PID-1 root) — if populated
#         on the host the function continues; if our copy is also populated it returns early.
#      b. If our /sys/firmware/efi/efivars is EMPTY (not populated), it mounts the REAL host
#         efivarfs over it, defeating our empty-tmpfs sandbox.
#   3. bootupd's update_firmware() then sees RDONLY OR empty efivars and returns Ok(()).
#
# Fix: mount tmpfs → seed one dummy EFI var file so setup_sys_mount sees our path as
# "already populated" and skips remounting the real host efivarfs → remount read-only so
# update_firmware()'s RDONLY check fires and it returns without ever calling efibootmgr.
mkdir -p /sys/firmware/efi/efivars
mount -t tmpfs tmpfs /sys/firmware/efi/efivars
# Dummy EFI Global Variable (BootOrder GUID) — content irrelevant, presence is what matters
touch /sys/firmware/efi/efivars/BootOrder-8be4df61-93ca-11d2-aa0d-00e098032b8c
mount -o remount,ro /sys/firmware/efi/efivars

bootc install to-filesystem --skip-fetch-check \
  "${CONSOLE_KARGS[@]}" "${EXTRA_KARGS[@]}" \
  --root-ssh-authorized-keys "${KEY}" \
  "${T}"

# Diagnostic: exactly what got laid on the ESP. Firmware boots the removable
# path \EFI\BOOT\BOOTX64.EFI, so it (and grubx64.efi) must be here. Printed
# before the EXIT trap unmounts the target.
echo "=== ESP contents (${T}/boot/efi) ==="
find "${T}/boot/efi" | sort
echo "=== end ESP ==="

# Diagnostic: probe the fstab and var layout that bootc generated, so we can
# confirm whether the btrfs var subvol gets an fstab entry (required for it to
# be mounted at /var at boot instead of the stateroot var inside the ro root
# subvol).
echo "=== generated fstab ==="
find "${T}/ostree/deploy" -name fstab 2>/dev/null | while read -r _f; do
  echo "--- $_f ---"
  cat "$_f"
done
echo "=== end generated fstab ==="
echo "=== stateroot var vs btrfs var subvol ==="
echo "--- ${T}/ostree/deploy/default/var (stateroot var, inside root subvol) ---"
ls -la "${T}/ostree/deploy/default/var" 2>/dev/null || echo "(not found)"
echo "--- ${T}/var (btrfs var subvol mount) ---"
ls -la "${T}/var" 2>/dev/null || echo "(not found)"
echo "=== end var layout ==="

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
  # Unlock the btrfs root subvolume so ostree can stage the testrollback
  # deployment. bootc install to-filesystem sets the subvolume ro=true AND
  # remounts the active mount with SB_RDONLY. Both layers must be cleared:
  #  1. btrfs property set via the raw pool (no subvol= filter) clears the
  #     per-subvolume BTRFS_ROOT_READONLY flag.
  #  2. mount -o remount,rw clears the VFS-level SB_RDONLY that bootc added.
  echo "=== btrfs ro state before unlock ==="
  btrfs property get "${T}" ro 2>&1 || true
  mount "${dev}p3" /pool
  btrfs property set /pool/root ro false 2>&1; echo "  btrfs property set exit=$?"
  umount /pool
  # Re-examine via the active mount after property set.
  btrfs property get "${T}" ro 2>&1 || true
  # Clear mount-level SB_RDONLY if bootc also remounted the active mount ro.
  # Do not repeat compress=: the option is already in effect from the initial
  # mount and btrfs rejects it on this remount ("Bad value for 'compress'"),
  # which left the target read-only.
  mount -o remount,rw "${T}" 2>&1 || true
  # bootc also remounts the ext4 /boot partition read-only; ostree deploy needs
  # to write BLS entries there (boot/loader.0), so unlock it too.
  mount -o remount,rw "${T}/boot" 2>&1 || true
  # Verify write access before handing off to ostree.
  if ! touch "${T}/.rw-probe" 2>/dev/null; then
    echo "::warning::${T} is still read-only after btrfs unlock + remount — testrollback will fail"
  else
    rm -f "${T}/.rw-probe"
    echo "=== ${T} is writable — proceeding with ostree deploy ==="
  fi
  if ostree --repo="${T}/ostree/repo" refs --create="testrollback/0" "${deployed_rev}" \
     && ostree admin --sysroot="${T}" deploy --os="${stateroot}" "testrollback/0"; then
    echo "=== rollback slot staged: ${deployed_rev} (stateroot: ${stateroot}) ==="
    ostree admin --sysroot="${T}" status || true
  else
    echo "::warning::testrollback slot not staged (sysroot locked after bootc install) — rollback tests will fail"
  fi
  # Inject CI-only journald config while the target is still rw (before re-lock).
  # Also write /etc/fstab into every deployment so btrfs var/varlog subvolumes
  # are mounted rw at boot. bootc install to-filesystem does not generate fstab;
  # without it, ostree-prepare-root binds /var from the stateroot var directory
  # (inside the ro btrfs root subvol), leaving /var and /etc read-only.
  for _deploy_etc in "${T}/ostree/deploy"/*/deploy/*/etc; do
    [ -d "${_deploy_etc}" ] || continue
    mkdir -p "${_deploy_etc}/systemd/journald.conf.d"
    printf '[Journal]\nForwardToConsole=yes\n' \
      > "${_deploy_etc}/systemd/journald.conf.d/99-ci-debug.conf"
    # Write the fstab that bootc to-filesystem omits. Without /var and /var/log
    # entries pointing at the writable btrfs subvolumes, ostree-prepare-root
    # binds /var from the stateroot var directory inside the read-only root
    # subvol, leaving the entire /var tree (and any etc overlay backed by it)
    # read-only at runtime. The /boot and /boot/efi entries are included for
    # completeness; they are not load-bearing for the writability fix.
    cat > "${_deploy_etc}/fstab" <<'FSTAB'
# Written by install-fs.sh — bootc install to-filesystem does not generate fstab.
# NOTE: no "/" entry — composefs is enabled (prepare-root.conf), and the bootc
# docs explicitly state that a "/" fstab entry does not work after the initrd
# with composefs (the root is a composefs overlay, not the raw btrfs subvol).
LABEL=dcos  /var       btrfs  subvol=var,x-systemd.device-timeout=0     0 0
LABEL=dcos  /var/log   btrfs  subvol=varlog,x-systemd.device-timeout=0  0 0
LABEL=boot  /boot      ext4   defaults                                   0 0
LABEL=EFI   /boot/efi  vfat   umask=0077                                 0 2
FSTAB
    echo "=== fstab written to ${_deploy_etc}/fstab ==="
    # CI-only diagnostic oneshot: dump the real /etc and /etc/ssh state
    # (existence, mount flags, live access() probe, live systemd condition
    # evaluation, the shipped keygen unit) after ostree-remount — the same
    # boot window where sshd-keygen.service's ConditionPathIsReadWrite fails
    # in CI. Reaches the serial console through the ForwardToConsole config
    # above. Diagnostic only, never ships in published images.
    mkdir -p "${_deploy_etc}/systemd/system/multi-user.target.wants"
    cat > "${_deploy_etc}/systemd/system/ci-ssh-debug.service" <<'UNIT'
[Unit]
Description=CI diagnostic: mount layout, writability, sshd keygen, network state
After=networking.service ssh.service
Wants=networking.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c "\
  echo '| ci-dbg: findmnt /var / /etc'; \
  findmnt /var / /etc 2>&1 | sed 's/^/| /'; \
  echo '| ci-dbg: touch /etc/ssh probe'; \
  touch /etc/ssh/.ci-rw-probe 2>&1 && echo '| ci-dbg: /etc/ssh RW OK' && rm -f /etc/ssh/.ci-rw-probe || echo '| ci-dbg: /etc/ssh NOT writable'; \
  echo '| ci-dbg: touch /var probe'; \
  touch /var/.ci-rw-probe 2>&1 && echo '| ci-dbg: /var RW OK' && rm -f /var/.ci-rw-probe || echo '| ci-dbg: /var NOT writable'; \
  echo '| ci-dbg: ip addr'; \
  ip addr show 2>&1 | sed 's/^/| /'; \
  echo '| ci-dbg: ip route'; \
  ip route show 2>&1 | sed 's/^/| /'; \
  echo '| ci-dbg: /etc/network/interfaces'; \
  cat /etc/network/interfaces 2>&1 | sed 's/^/| /'; \
  echo '| ci-dbg: networking.service drop-ins'; \
  ls -la /usr/lib/systemd/system/networking.service.d/ 2>&1 | sed 's/^/| /'; \
  echo '| ci-dbg: ifupdown2-autoconf script'; \
  ls -la /usr/sbin/ifupdown2-autoconf 2>&1 | sed 's/^/| /'; \
  echo '| ci-dbg: systemctl cat networking.service'; \
  systemctl cat networking.service 2>&1 | sed 's/^/| /'; \
  echo '| ci-dbg: journalctl -u networking'; \
  journalctl -u networking -b --no-pager -o short-precise 2>&1 | tail -40 | sed 's/^/| /'; \
  echo '| ci-dbg: networking.service timings'; \
  systemctl show networking.service -p ExecMainStartTimestampMonotonic -p ExecMainExitTimestampMonotonic -p TimeoutStartUSec -p Result 2>&1 | sed 's/^/| /'; \
  echo '| ci-dbg: /root/.ssh/authorized_keys'; \
  ls -la /root/.ssh/ 2>&1 | sed 's/^/| /'; \
  cat /root/.ssh/authorized_keys 2>&1 | head -3 | sed 's/^/| /'; \
  echo '| ci-dbg: sshd status'; \
  systemctl status ssh.service 2>&1 | tail -8 | sed 's/^/| /'"
UNIT
    ln -sf "../ci-ssh-debug.service" \
      "${_deploy_etc}/systemd/system/multi-user.target.wants/ci-ssh-debug.service"
  done
  # Populate the btrfs var subvol (mounted at ${T}/var) with the content that
  # bootc placed in the stateroot var directory (inside the root subvol).
  # The fstab entries above make systemd mount the btrfs var subvol at /var
  # at runtime; for that to work, the subvol must have the same initial content
  # as the stateroot var.  Without this copy, the runtime /var would be empty
  # and tmpfiles / package state would be missing.
  _stateroot_var="${T}/ostree/deploy/${stateroot}/var"
  if [ -d "${_stateroot_var}" ] && [ -d "${T}/var" ]; then
    cp -a "${_stateroot_var}/." "${T}/var/"
    echo "=== var subvol populated from ${_stateroot_var} ==="
  fi
  # Do NOT re-lock the btrfs root subvol with ro=true here.
  # The CI diag (run 39776149934) shows btrfs property ro=false BEFORE our
  # unlock step — bootc install to-filesystem does not set btrfs-level ro on
  # this runner (it may only set VFS SB_RDONLY, which remount,rw already
  # cleared). Setting ro=true here makes the stateroot var (inside the root
  # subvol) read-only, which prevents ostree-prepare-root from creating a
  # writable upper dir for the /etc overlay → /etc and /var are ro at boot.
  # The deployment content is already immutable via composefs (prepare-root.conf
  # has composefs=enabled=yes); btrfs-level ro on the root subvol adds nothing
  # and breaks the write paths that ostree sysroot.readonly=true depends on.
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
# Create the journal directory in the varlog subvolume so systemd-journald
# uses persistent storage (Storage=auto writes to /var/log/journal only when
# the dir exists). The varlog subvolume overlays /var/log at runtime, so the
# directory must live there — not in the var subvolume above.
mkdir -p /varlog
mount -o subvol=varlog LABEL=dcos /varlog
mkdir -p /varlog/journal
umount /varlog
