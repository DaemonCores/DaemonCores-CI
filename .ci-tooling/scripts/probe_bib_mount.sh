#!/bin/sh
# Probe for the amd64-only osbuild mount EPERM (P-757).
# Replicates the osbuild-193 buildroot sandbox (bwrap, no userns) and the
# shipped mount sequence from osbuild/util/containers.py:
#   rbind RW of the store, then remount RW ("dirty fix").
set -u
BIB=ghcr.io/daemoncores/debian-bootc:$2
echo "== kernel =="
uname -r
sudo podman pull "$BIB" >/dev/null 2>&1 || true
mkdir -p run-store/overlay

echo "== PROBE F: bwrap sandbox, store bound RO, shipped sequence =="
sudo podman run --rm --privileged --security-opt apparmor=unconfined \
    -v "$(pwd)/run-store:/var/lib/containers/storage:rshared" \
    "$BIB" /bin/sh -s <<'EOF_BIB'
mkdir -p /tmp/orig /tmp/sb-in &&
mount --rbind -o ro,0755 --make-rprivate /var/lib/containers/storage /tmp/orig &&
bwrap --chdir / --die-with-parent --new-session \
      --unshare-ipc --unshare-pid --unshare-net \
      --ro-bind /tmp/orig /var/lib/containers/storage \
      --proc /proc --dev /dev \
      /bin/sh -c '
        mkdir -p /run/osbuild/containers/storage;
        mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /run/osbuild/containers/storage && echo F_RBIND_RW_OK || echo F_RBIND_RW_FAIL;
        mount -o remount,bind,rw /run/osbuild/containers/storage && echo F_REMOUNT_RW_OK || echo F_REMOUNT_RW_FAIL
      ' || echo F_BWRAP_FAIL
EOF_BIB

echo "== PROBE G: bwrap sandbox, store bound RW =="
sudo podman run --rm --privileged --security-opt apparmor=unconfined \
    -v "$(pwd)/run-store:/var/lib/containers/storage:rshared" \
    "$BIB" /bin/sh -s <<'EOF_BIB'
bwrap --chdir / --die-with-parent --new-session \
      --unshare-ipc --unshare-pid --unshare-net \
      --bind /var/lib/containers/storage /var/lib/containers/storage \
      --proc /proc --dev /dev \
      /bin/sh -c '
        mkdir -p /run/osbuild/containers/storage;
        mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /run/osbuild/containers/storage && echo G_RBIND_RW_OK || echo G_RBIND_RW_FAIL;
        mount -o remount,bind,rw /run/osbuild/containers/storage && echo G_REMOUNT_RW_OK || echo G_REMOUNT_RW_FAIL
      ' || echo G_BWRAP_FAIL
EOF_BIB

echo "== caps inside bwrap =="
sudo podman run --rm --privileged --security-opt apparmor=unconfined \
    -v "$(pwd)/run-store:/var/lib/containers/storage:rshared" \
    "$BIB" /bin/sh -s <<'EOF_BIB'
bwrap --chdir / --new-session --unshare-ipc --unshare-pid --unshare-net \
      --proc /proc --dev /dev \
      /bin/sh -c 'grep -A1 Cap /proc/self/status | head -6; echo user-ns-limit=$(cat /proc/sys/user/max_user_namespaces)'
EOF_BIB

echo "== kernel ring buffer =="
sudo dmesg | tail -40 || true