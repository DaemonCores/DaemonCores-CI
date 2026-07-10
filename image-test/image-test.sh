#!/bin/bash
#
# image-test.sh - install the freshly built bootc image onto a virtual disk,
# boot it under QEMU/KVM (UEFI), and run the repo's image-tests manifest over
# SSH. Exits non-zero (blocking the image push) if any test fails.
#
# Consumed environment (set by action.yml):
#   IMAGE       local image ref to test (already in podman storage)
#   TESTS_FILE  path to the tests manifest (yaml)
#   MEM         VM memory in MiB (default 4096)
#   VCPUS       VM vCPUs (default 2)
#   RUNNER_TEMP scratch dir provided by Actions
set -euo pipefail

WORK="${RUNNER_TEMP:-/tmp}"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DISK="${WORK}/test-disk.raw"
KEY="${WORK}/test-key"
SERIAL="${WORK}/serial.log"
SSH_PORT=2222
SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=3
  -i "${KEY}"
  -p "${SSH_PORT}"
)

#######################################
# Install QEMU/OVMF/yq and grant the runner access to /dev/kvm.
#######################################
install_deps() {
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends qemu-system-x86 ovmf jq
  command -v yq >/dev/null 2>&1 || {
    sudo curl -fsSL \
      https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 \
      -o /usr/local/bin/yq
    sudo chmod +x /usr/local/bin/yq
  }
  echo 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"' \
    | sudo tee /etc/udev/rules.d/99-kvm4all.rules >/dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger --name-match=kvm
}

#######################################
# Install the image to DISK, injecting a generated root SSH key.
# Globals:
#   IMAGE, DISK, KEY, WORK
#######################################
install_disk() {
  ssh-keygen -t ed25519 -N '' -f "${KEY}" -q
  truncate -s 20G "${DISK}"
  # The disk is partitioned + installed by install-fs.sh, run privileged INSIDE
  # the image so it can use the image's own tools and bootc install
  # to-filesystem into the same layout as the ISO (btrfs pool + varlog quota).
  # bootc-build.yml builds with sudo podman, so this root podman sees the freshly
  # built image directly (no registry pull).
  sudo podman run --rm --privileged --pid=host --security-opt label=disable \
    -v /dev:/dev -v /var/lib/containers:/var/lib/containers \
    -v "${DISK}:/disk.raw" \
    -v "${KEY}.pub:/key.pub:ro" \
    -v "${SCRIPT_DIR}/install-fs.sh:/install-fs.sh:ro" \
    "${IMAGE}" \
    bash /install-fs.sh
}

#######################################
# Boot the disk under QEMU/KVM with UEFI and an SSH port-forward.
# Globals:
#   DISK, SERIAL, SSH_PORT, WORK, MEM, VCPUS
#######################################
boot_vm() {
  local code vars
  code=$(find /usr/share/OVMF -name 'OVMF_CODE*.fd' | head -1)
  vars=$(find /usr/share/OVMF -name 'OVMF_VARS*.fd' | head -1)
  cp "${vars}" "${WORK}/OVMF_VARS.fd"
  sudo qemu-system-x86_64 \
    -enable-kvm -m "${MEM:-4096}" -smp "${VCPUS:-2}" -machine q35 \
    -drive "if=pflash,format=raw,readonly=on,file=${code}" \
    -drive "if=pflash,format=raw,file=${WORK}/OVMF_VARS.fd" \
    -drive "file=${DISK},format=raw,if=virtio" \
    -netdev "user,id=n0,hostfwd=tcp::${SSH_PORT}-:22" \
    -device virtio-net,netdev=n0 \
    -nographic -serial "file:${SERIAL}" \
    -daemonize -pidfile "${WORK}/qemu.pid"
}

#######################################
# Poll SSH until the VM answers or the timeout elapses.
# Arguments:
#   $1 - timeout in seconds
# Returns:
#   0 when reachable, 1 on timeout.
#######################################
wait_ssh() {
  local timeout="$1" i
  for ((i = 0; i < timeout; i++)); do
    ssh "${SSH_OPTS[@]}" root@localhost true 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

#######################################
# Run every test in TESTS_FILE over SSH. A test passes on exit 0, or when its
# optional `expect` string is found in the output.
# Globals:
#   TESTS_FILE, SSH_OPTS
# Returns:
#   0 if all tests pass, 1 otherwise.
#######################################
run_tests() {
  local n i name cmd expect out rc fail=0 pass
  n=$(yq '.tests | length' "${TESTS_FILE}")
  for ((i = 0; i < n; i++)); do
    name=$(yq ".tests[${i}].name" "${TESTS_FILE}")
    cmd=$(yq ".tests[${i}].run" "${TESTS_FILE}")
    expect=$(yq ".tests[${i}].expect // \"\"" "${TESTS_FILE}")
    # cmd is the manifest's test command; expanding it here into the ssh
    # argument (to run on the VM) is intentional.
    # shellcheck disable=SC2029
    out=$(ssh "${SSH_OPTS[@]}" root@localhost "${cmd}" 2>&1) && rc=0 || rc=$?
    pass=0
    if [ -n "${expect}" ]; then
      printf '%s' "${out}" | grep -qF "${expect}" && pass=1
    elif [ "${rc}" -eq 0 ]; then
      pass=1
    fi
    if [ "${pass}" -eq 1 ]; then
      echo "  PASS  ${name}"
    else
      echo "::error::image test failed: ${name}"
      printf '%s\n' "${out}" | sed 's/^/      /'
      fail=1
    fi
  done
  return "${fail}"
}

#######################################
# Kill the QEMU VM on exit.
#######################################
cleanup() {
  if [ -f "${WORK}/qemu.pid" ]; then
    sudo kill "$(cat "${WORK}/qemu.pid")" 2>/dev/null || true
  fi
}

main() {
  trap cleanup EXIT
  install_deps
  install_disk
  boot_vm
  local timeout
  timeout=$(yq '.boot_timeout // 600' "${TESTS_FILE}")
  if ! wait_ssh "${timeout}"; then
    echo "::error::VM did not become reachable within ${timeout}s"
    sudo tail -n 50 "${SERIAL}" 2>/dev/null || true
    exit 1
  fi
  run_tests
}

main "$@"
