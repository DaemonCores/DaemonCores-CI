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
# find lists the profile files itself: no ls | grep, so a profile whose name
# carries an unusual character is still reported.
profiles=$(find /etc/apparmor.d -maxdepth 1 -name '*bwrap*' -print 2>/dev/null)
first_profile=$(find /etc/apparmor.d -maxdepth 1 -name '*bwrap*' -print -quit 2>/dev/null)
if [ -n "$profiles" ]; then printf '%s\n' "$profiles"; else echo none-in-etc; fi
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
if [ -n "$profiles" ]; then
  sudo apparmor_parser -R /etc/apparmor.d/unpriv_bwrap 2>&1 || true
  # a refused unload is reported, not hidden: the probe continues either way
  sudo apparmor_parser -R "$first_profile" 2>&1 || echo "unload of $first_profile failed"
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