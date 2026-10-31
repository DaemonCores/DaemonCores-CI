#!/bin/bash
#
# image-test.sh - install a freshly built bootc image onto a virtual disk, boot
# it under QEMU/KVM (UEFI), and run the repo's image-tests manifest over SSH.
# Exits non-zero (blocking the image push) if any test fails.
#
# Sub-commands (see lib.sh for the functions and the consumed environment):
#   prepare                         once per job
#   test <image-ref> [<ram-max-mb>] once per image, as a child process
#   teardown                        once per job
#   (none)                          prepare + test $IMAGE + teardown, as the
#                                   action runs it for a single image
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

dispatch "$@"
