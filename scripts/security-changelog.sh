#!/bin/bash
#
# security-changelog.sh - render the security upgrades as release notes.
#
# Inputs:
#   stdin: output of security-detect.sh, one "<package> <old> <new>" per line
#   $1: date of the build (YYYYMMDD)
# Outputs:
#   stdout: Markdown release notes with one table row per upgraded package
# Returns:
#   0 on success; 1 when the date is malformed or the list is empty.
set -euo pipefail

#######################################
# Print the release notes read from stdin.
# Globals:
#   none
# Arguments:
#   $1: build date, YYYYMMDD
# Outputs:
#   Markdown on stdout.
#######################################
changelog() {
  local day="${1:?date required}"
  [[ "${day}" =~ ^[0-9]{8}$ ]] || {
    echo "invalid date: ${day}" >&2
    return 1
  }
  local rows
  rows=$(awk 'NF == 3 { printf "| %s | %s | %s |\n", $1, $2, $3 }')
  [[ -n "${rows}" ]] || {
    echo "no upgrades on stdin" >&2
    return 1
  }
  printf '# Security update %s\n\n' "${day}"
  printf '| Package | Previous | Fixed |\n| --- | --- | --- |\n%s\n' "${rows}"
}

changelog "$@"
