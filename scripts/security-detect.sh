#!/bin/bash
#
# security-detect.sh - list the packages a security rebuild would upgrade.
#
# Reads the output of "apt-get -s upgrade" on stdin and keeps the packages
# whose candidate comes from the Debian security archive. A version change in
# the security archive is the only trigger of a rebuild: tracker entries such
# as no-dsa or postponed never change that archive, so they never trigger one.
#
# Inputs:
#   stdin: output of apt-get -s upgrade run inside the stable image
# Outputs:
#   stdout: one "<package> <old> <new>" line per security upgrade, sorted
# Returns:
#   0 always; an empty output means nothing to rebuild.
set -euo pipefail

#######################################
# Print the security upgrades found in a simulated apt upgrade.
# Inst lines look like: Inst pkg [old] (new Debian-Security:13/stable-security [arch])
# Globals:
#   none
# Arguments:
#   none
# Outputs:
#   One line per security upgrade, sorted.
#######################################
security_upgrades() {
  sed -nE 's/^Inst ([^ ]+) \[([^]]+)\] \(([^ ]+) [^)]*-security[^)]*\).*$/\1 \2 \3/p' | sort
}

security_upgrades
