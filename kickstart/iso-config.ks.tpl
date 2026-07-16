# DaemonCores VE installer kickstart.
#
# Anaconda's own storage (blivet) cannot create our btrfs subvolume layout — a
# documented bootc+Anaconda limitation: on a btrfs root only / and /boot are
# configurable, custom mount points under /var (our varlog quota subvolume) are
# rejected, and preserving /var across a reinstall is an unsupported workflow.
# So we do the WHOLE install ourselves in %pre with `bootc install
# to-filesystem` (the same proven path used for the CLI install), then reboot —
# Anaconda only provides the live environment (kernel, tools, podman, the image).
#
# Layout: one btrfs pool "dcos" whose root/var/varlog subvolumes share it (no
# fixed sizing); /var/log capped 2G via a btrfs qgroup quota. A reinstall detects
# an existing dcos pool and PRESERVES its var subvolume (data survives) while
# replacing root+varlog.

# Minimal Anaconda config: we never reach its storage/payload (the %pre below
# does the whole install and reboots), but Anaconda still needs a syntactically
# valid kickstart WITH a payload to accept the file and reach %pre. This bootc
# line is that payload placeholder — it is never executed: on success %pre issues
# `reboot -f`, and on failure %pre exits non-zero (--erroronfail) so Anaconda
# aborts. No partitioning directives here (so no blivet /var subvolume rejection).
text
network --bootproto=dhcp --activate
zerombr
clearpart --none
reqpart
bootc --source-imgref={{ source_imgref }} --target-imgref=ghcr.io/{{ repo }}:latest

%pre --interpreter=/bin/bash --erroronfail
# Anaconda drives tty1 through tmux; reading/writing it directly garbles output
# and steals keystrokes. Move to a dedicated VT and make it the active console
# (Red Hat KB 273843); stty sane restores cooked mode.
exec < /dev/tty6 > /dev/tty6 2> /dev/tty6
chvt 6 2>/dev/null || true
stty sane 2>/dev/null || true

echo
echo "======================================================================"
echo "  {{ hostname }} — installer"
echo "======================================================================"
echo "  ONLY the disk you choose is wiped. Other disks (e.g. ZFS data pools)"
echo "  are left untouched. A dcos pool's /var is preserved on reinstall."
echo

# ── Interactive OS-disk selection ───────────────────────────────────────────
# Real OS-install targets only: filter zram (Fedora's installer enables a zram
# swap), loop, ram, sr, md and dm devices (all report TYPE=disk to lsblk).
mapfile -t DISKS < <(lsblk -dpno NAME,SIZE,TYPE,MODEL \
  | awk '$3=="disk" && $1 !~ /^\/dev\/(zram|loop|ram|fd|sr|md|dm-)/ { $3=""; sub(/  +/," "); print }')
[[ ${#DISKS[@]} -gt 0 ]] || { echo "  No disk found — aborting."; exit 1; }

while :; do
    i=0
    for d in "${DISKS[@]}"; do echo "  [$i] $d"; i=$((i+1)); done
    echo
    while :; do
        read -rp "  OS disk number: " sel
        [[ "$sel" =~ ^[0-9]+$ && -n "${DISKS[$sel]:-}" ]] && break
        echo "  Invalid selection."
    done
    DISK=$(awk '{print $1}' <<<"${DISKS[$sel]}")
    read -rp "  Install onto ${DISK}? [y/N]: " ok
    [[ "$ok" =~ ^[Yy]$ ]] && break
    echo "  Cancelled — choose again."
    echo
done

# ── Obtain the bootc image (offline archive if embedded, else pull) ──────────
echo
echo "  Preparing the DaemonCores VE image..."
if [ -f /run/install/repo/image.tar ]; then
    IMG=$(podman load -i /run/install/repo/image.tar 2>&1 | sed -n 's/^Loaded image: //p' | tail -1)
else
    nmcli networking on 2>/dev/null || true
    IMG="ghcr.io/{{ repo }}:latest"
    podman pull "${IMG}"
fi
[ -n "${IMG}" ] || { echo "  Could not obtain the image — aborting."; exit 1; }

# ── The installer, run inside the image (ships sfdisk/mkfs/btrfs/bootc) ──────
cat > /tmp/dc-install.sh <<'INSTALL'
#!/bin/bash
set -euo pipefail
DEV="$1"; T=/target
case "${DEV}" in *[0-9]) P="p";; *) P="";; esac
p1="${DEV}${P}1"; p2="${DEV}${P}2"; p3="${DEV}${P}3"
cleanup() { umount -R "${T}" 2>/dev/null || true; umount /pool 2>/dev/null || true; }
trap cleanup EXIT

reinstall=0
if [ -e "${p3}" ] && blkid "${p3}" 2>/dev/null | grep -q 'LABEL="dcos"'; then
  mkdir -p /pool; mount "${p3}" /pool
  btrfs subvolume list /pool | awk '{print $NF}' | grep -qx var && reinstall=1
  umount /pool
fi

if [ "${reinstall}" -eq 0 ]; then
  echo ">>> FRESH install on ${DEV}."
  wipefs -a "${DEV}" >/dev/null 2>&1 || true
  sfdisk "${DEV}" <<'PART'
label: gpt
size=512M, type=uefi
size=1G,   type=linux
type=linux
PART
  partprobe "${DEV}"; udevadm settle 2>/dev/null || sleep 1
  mkfs.vfat -n EFI "${p1}" >/dev/null
  mkfs.ext4 -q -L boot "${p2}"
  mkfs.btrfs -q -L dcos "${p3}"
  mkdir -p /pool; mount "${p3}" /pool
  btrfs subvolume create /pool/root   >/dev/null
  btrfs subvolume create /pool/var    >/dev/null
  btrfs subvolume create /pool/varlog >/dev/null
  umount /pool
else
  echo ">>> REINSTALL on ${DEV} — preserving var, replacing root+varlog."
  mkfs.vfat -n EFI "${p1}" >/dev/null
  mkfs.ext4 -q -L boot "${p2}"
  mkdir -p /pool; mount "${p3}" /pool
  while read -r sv; do
    [ -n "${sv}" ] && btrfs subvolume delete "/pool/${sv}" >/dev/null 2>&1 || true
  done < <(btrfs subvolume list /pool | awk '{print $NF}' \
             | grep -E '^root(/|$)|^varlog$' | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-)
  btrfs subvolume sync /pool
  btrfs subvolume create /pool/root   >/dev/null
  btrfs subvolume create /pool/varlog >/dev/null
  umount /pool
fi

mkdir -p "${T}"
mount -o subvol=root,compress=zstd LABEL=dcos "${T}"
mkdir -p "${T}/boot" "${T}/var"
mount "${p2}" "${T}/boot"
mkdir -p "${T}/boot/efi"; mount "${p1}" "${T}/boot/efi"
mount -o subvol=var,compress=zstd LABEL=dcos "${T}/var"
mkdir -p "${T}/var/log"
mount -o subvol=varlog,compress=zstd LABEL=dcos "${T}/var/log"
btrfs quota enable "${T}" >/dev/null 2>&1 || true
btrfs qgroup limit 2G "${T}/var/log" >/dev/null 2>&1 || true

# --target-imgref pins the upgrade source to the registry so `bootc upgrade`
# works whether this install came from a registry pull or the offline archive.
bootc install to-filesystem --skip-fetch-check \
  --target-imgref ghcr.io/{{ repo }}:latest "${T}"
echo ">>> DaemonCores VE installed on ${DEV} (reinstall=${reinstall})."
INSTALL

echo "  Installing DaemonCores VE onto ${DISK} (this wipes it)..."
if podman run --rm --privileged --pid=host --security-opt label=disable \
     -v /dev:/dev -v /var/lib/containers:/var/lib/containers \
     -v /tmp/dc-install.sh:/dc-install.sh:ro \
     "${IMG}" bash /dc-install.sh "${DISK}"; then
    echo
    echo "  Installation complete. Rebooting into DaemonCores VE."
    echo "  (First boot runs the setup wizard on this console.)"
    sleep 3
    chvt 1 2>/dev/null || true
    sync
    reboot -f
    # Defensive: if reboot -f ever returns, abort so Anaconda does NOT fall
    # through to the bootc payload placeholder and install a second time.
    sleep 60
    exit 1
else
    echo "  INSTALL FAILED — see the output above. Not rebooting."
    exit 1
fi
%end
