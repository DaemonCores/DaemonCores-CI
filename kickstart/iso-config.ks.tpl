# Partitioning is chosen interactively (the operator picks the OS disk) and
# emitted to /tmp/dc-part.ks. ONLY the selected disk is wiped, so ZFS data pools
# on other disks survive a reinstall. Layout: one btrfs pool whose root/var/
# varlog subvolumes share it (no fixed sizing); /var/log is a subvolume capped
# at 2G on-disk via a btrfs qgroup quota (compressed, so it holds far more).
%pre --interpreter=/bin/bash --erroronfail
# Anaconda drives tty1 through tmux, so reading/writing it directly garbles the
# output (an LF-without-CR staircase) and keystrokes never reach our `read`
# (they go to tmux), which soft-locks the prompt. The interactive-%pre pattern
# is to move to a dedicated, unused VT and make it the *active* console with
# chvt (so both the display and the keyboard are ours), then switch back to
# tty1 for Anaconda. Ref: Red Hat KB 273843. stty sane is belt-and-suspenders
# cooked mode on the fresh VT.
exec < /dev/tty6 > /dev/tty6 2> /dev/tty6
chvt 6 2>/dev/null || true
stty sane 2>/dev/null || true

echo
echo "======================================================================"
echo "  {{ hostname }} — target disk selection"
echo "======================================================================"
echo "  ONLY the disk you choose is wiped. Other disks (e.g. ZFS data pools)"
echo "  are left untouched."
echo

# Real OS-install targets only. lsblk reports zram (Fedora's installer enables a
# zram swap device), loop, ram, sr, md and dm devices as TYPE=disk too, so filter
# those pseudo/virtual devices out by name — otherwise e.g. /dev/zram0 shows up
# as a selectable (and dangerous) OS disk.
mapfile -t DISKS < <(lsblk -dpno NAME,SIZE,TYPE,MODEL \
  | awk '$3=="disk" && $1 !~ /^\/dev\/(zram|loop|ram|fd|sr|md|dm-)/ { $3=""; sub(/  +/," "); print }')
[[ ${#DISKS[@]} -gt 0 ]] || { echo "  No disk found — aborting."; exit 1; }

i=0
for d in "${DISKS[@]}"; do echo "  [$i] $d"; i=$((i+1)); done
echo
while :; do
    read -rp "  OS disk number: " sel
    [[ "$sel" =~ ^[0-9]+$ && -n "${DISKS[$sel]:-}" ]] && break
    echo "  Invalid selection."
done
DISK=$(awk '{print $1}' <<<"${DISKS[$sel]}")
read -rp "  Type ERASE to confirm wiping ${DISK}: " ok
[[ "$ok" == "ERASE" ]] || { echo "  Not confirmed — aborting."; exit 1; }

DEV=${DISK##*/}
cat > /tmp/dc-part.ks <<EOF
zerombr
clearpart --all --initlabel --disklabel=gpt --drives=${DEV}
reqpart --add-boot
part btrfs.01 --fstype=btrfs --size=1024 --grow --ondisk=${DEV}
btrfs none     --label=dcos btrfs.01
btrfs /        --subvol --name=root   LABEL=dcos
btrfs /var     --subvol --name=var    LABEL=dcos
btrfs /var/log --subvol --name=varlog LABEL=dcos
EOF

# Hand the console back to Anaconda's tty1.
chvt 1 2>/dev/null || true
exec < /dev/tty1 > /dev/tty1 2>&1
%end

%include /tmp/dc-part.ks

network --bootproto=dhcp --hostname={{ hostname }} --activate
# Prevent Anaconda's timezone module from enabling chronyd (it ignores
# services --disabled=chronyd). --nontp is the only effective knob.
timezone --nontp

bootc --source-imgref={{ source_imgref }} --target-imgref=ghcr.io/{{ repo }}:latest

%post
# Enable zstd compression on the btrfs subvolumes (new writes inherit it).
btrfs property set / compression zstd 2>/dev/null || true
btrfs property set /var compression zstd 2>/dev/null || true
btrfs property set /var/log compression zstd 2>/dev/null || true
# Cap /var/log at 2G of on-disk (compressed) usage via a btrfs qgroup quota on
# its subvolume — no fixed partition. ENOSPC there leaves the rest untouched.
btrfs quota enable / 2>/dev/null || true
btrfs qgroup limit 2G /var/log 2>/dev/null || true

# Default root password for debug if first boot setup don't run
echo 'root:BootcDebug@0' | chpasswd
chage -d 0 root

# User creation and SSH root login are handled by firstboot-user-setup.service on first boot.
# Remove any SSH config Anaconda may have set.
rm -f /etc/ssh/sshd_config.d/01-permitrootlogin.conf

# Queue Secure Boot MOK enrollment for the first reboot
# The administrator will be prompted at the blue MokManager screen.
# Password: root password set above (changed by firstboot-user-setup on first login).
CERT=/usr/share/debian-bootc/sb_signing.crt
if [ -f "$CERT" ] && command -v mokutil >/dev/null 2>&1; then
    if ! mokutil --test-key "$CERT" 2>/dev/null; then
        echo 'BootcDebug@0' | mokutil --import "$CERT" --root-pw
        echo "Secure Boot: MOK enrollment queued. Confirm at next reboot (MokManager)."
    fi
fi
%end