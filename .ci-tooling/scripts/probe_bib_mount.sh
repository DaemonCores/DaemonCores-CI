#!/bin/sh
# Probe final for the amd64-only bwrap+mount EPERM (P-757).
# Run 41564588280 proved via dmesg: AppArmor profile "unpriv_bwrap" denies
# CAP_SYS_ADMIN (mount) to the children of bwrap on the amd64 24.04 runner
# image; arm64 does not enforce it. This run: check who ships the profile,
# unload it, confirm the mounts then succeed.
set -u
BIB=ghcr.io/daemoncores/debian-bootc:bib_$1
echo "== kernel =="
uname -r
echo "== LSM =="
cat /sys/kernel/security/lsm 2>/dev/null || echo LSM_NONE
echo "== host apparmor profiles mentioning bwrap =="
ls /etc/apparmor.d/ | grep -i bwrap || echo none-in-etc
sudo apparmor_status 2>/dev/null | grep -i bwrap || true
echo "== probe BEFORE unload =="
sudo podman pull "$BIB" >/dev/null 2>&1 || true
mkdir -p run-store/overlay
sudo podman run -i --rm --privileged --security-opt apparmor=unconfined \
    --entrypoint /bin/sh \
    -v "$(pwd)/run-store:/var/lib/containers/storage:rshared" \
    "$BIB" -s <<'EOF_BIB'
bwrap --ro-bind / / --tmpfs /run --tmpfs /tmp --proc /proc --dev /dev \
      sh -c 'mkdir -p /tmp/osp && mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /tmp/osp && echo BEFORE_OK || echo BEFORE_FAIL'
EOF_BIB
echo "== try unloading the profile =="
if ls /etc/apparmor.d/ | grep -qi bwrap; then
  sudo apparmor_parser -R /etc/apparmor.d/unpriv_bwrap 2>&1 || true
  sudo apparmor_parser -R "$(ls /etc/apparmor.d/*bwrap* | head -1)" 2>&1 || true
else
  echo "no profile file on host — searching loaded profile"
  sudo apparmor_status 2>/dev/null | sed -n '1,30p'
fi
echo "== probe AFTER unload =="
sudo podman run -i --rm --privileged --security-opt apparmor=unconfined \
    --entrypoint /bin/sh \
    -v "$(pwd)/run-store:/var/lib/containers/storage:rshared" \
    "$BIB" -s <<'EOF_BIB'
bwrap --ro-bind / / --tmpfs /run --tmpfs /tmp --proc /proc --dev /dev \
      sh -c 'mkdir -p /tmp/osp && mount --rbind -o rw,0755 --make-rprivate /var/lib/containers/storage /tmp/osp && echo AFTER_OK || echo AFTER_FAIL'
EOF_BIB
echo "== kernel ring buffer =="
sudo dmesg | tail -25 || true