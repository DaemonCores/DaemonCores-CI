# Partitioning is chosen interactively (the operator picks the OS disk) and
# emitted to /tmp/dc-part.ks. ONLY the selected disk is wiped, so ZFS data pools
# on other disks survive a reinstall. Layout: small ext4 /var/log (caps log
# growth) + one btrfs whose root/var subvolumes share the pool (no fixed / vs
# /var sizing, image growth is a non-issue).
%pre --interpreter=/bin/bash --erroronfail
exec < /dev/tty1 > /dev/tty1 2>&1

echo
echo "======================================================================"
echo "  {{ hostname }} — target disk selection"
echo "======================================================================"
echo "  ONLY the disk you choose is wiped. Other disks (e.g. ZFS data pools)"
echo "  are left untouched."
echo

mapfile -t DISKS < <(lsblk -dpno NAME,SIZE,TYPE,MODEL | awk '$3=="disk"{ $3=""; sub(/  +/," "); print }')
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
part /var/log --fstype=ext4  --size=2048        --ondisk=${DEV}
part btrfs.01 --fstype=btrfs --size=1024 --grow --ondisk=${DEV}
btrfs none    --label=dcos btrfs.01
btrfs /       --subvol --name=root LABEL=dcos
btrfs /var    --subvol --name=var  LABEL=dcos
EOF
%end

%include /tmp/dc-part.ks

network --bootproto=dhcp --hostname={{ hostname }} --activate
# Prevent Anaconda's timezone module from enabling chronyd (it ignores
# services --disabled=chronyd). --nontp is the only effective knob.
timezone --nontp

bootc --source-imgref=registry:ghcr.io/{{ repo }}:latest --target-imgref=ghcr.io/{{ repo }}:latest

%post
# Enable zstd compression on the btrfs subvolumes (new writes inherit it).
btrfs property set / compression zstd 2>/dev/null || true
btrfs property set /var compression zstd 2>/dev/null || true

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