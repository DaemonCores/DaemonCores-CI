#!/bin/sh
# Probe bisection for the amd64-only bwrap+mount EPERM (P-757).
# Run 41563999100 proved: inside bwrap (ipc/pid/net) ANY rbind+make-rprivate
# mount of the WORKSPACE-bound store fails with EPERM on amd64 only, while
# arm64 does the same mounts fine. No userns involved (caps identical).
# This run: (1) which unshare flag flips amd64 to EPERM, (2) LSM state
# of both runner VMs.
set -u
BIB=ghcr.io/daemoncores/debian-bootc:bib_$1
echo "== kernel =="
uname -r
echo "== LSM =="
cat /sys/kernel/security/lsm 2>/dev/null || echo LSM_NONE
sudo podman pull "$BIB" >/dev/null 2>&1 || true
mkdir -p run-store/overlay
sudo podman run -i --rm --privileged --security-opt apparmor=unconfined \
    --entrypoint /bin/sh \
    -v "$(pwd)/run-store:/var/lib/containers/storage:rshared" \
    "$BIB" -s <<'EOF_BIB'
echo "== bwrap version =="; bwrap --version
BW="bwrap --ro-bind / / --tmpfs /run --tmpfs /tmp --proc /proc --dev /dev"
echo "== H0: no unshare =="
$BW sh -c 'mkdir -p /tmp/osp && mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /tmp/osp && echo H0_OK || echo H0_FAIL'
echo "== H1: unshare-ipc only =="
$BW --unshare-ipc sh -c 'mkdir -p /tmp/osp && mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /tmp/osp && echo H1_OK || echo H1_FAIL'
echo "== H2: unshare-pid only =="
$BW --unshare-pid sh -c 'mkdir -p /tmp/osp && mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /tmp/osp && echo H2_OK || echo H2_FAIL'
echo "== H3: unshare-net only =="
$BW --unshare-net sh -c 'mkdir -p /tmp/osp && mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /tmp/osp && echo H3_OK || echo H3_FAIL'
echo "== H4: unshare trio =="
$BW --unshare-net --unshare-pid --unshare-ipc sh -c 'mkdir -p /tmp/osp && mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /tmp/osp && echo H4_OK || echo H4_FAIL'
echo "== H5: mount ns alone, no bwrap =="
unshare -m sh -c 'mkdir -p /tmp/osp && mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /tmp/osp && echo H5_OK || echo H5_FAIL'
EOF_BIB
echo "== kernel ring buffer =="
sudo dmesg | tail -20 || true