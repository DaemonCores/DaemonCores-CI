#!/bin/bash
#
# release-channel.sh - print the release channel a git ref publishes to.
#
# Git flow: develop is the edge channel, main the stable one. The security
# channel is rebuilt from stable by its own workflow, never from a branch. Any
# other ref has no channel and must not publish: it would overwrite one.
#
# Arguments:
#   $1 - branch name (default: GITHUB_REF_NAME)
# Outputs:
#   The channel on stdout.
# Returns:
#   1 when the ref has no channel.
set -euo pipefail

ref="${1:-${GITHUB_REF_NAME:-}}"
case "${ref}" in
  develop) echo edge ;;
  main) echo stable ;;
  *)
    echo "::error::ref '${ref}' has no release channel (develop = edge, main = stable)" >&2
    exit 1
    ;;
esac
