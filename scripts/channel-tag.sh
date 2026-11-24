#!/bin/bash
#
# channel-tag.sh - print the channel alias of an image tag.
#
# A channel alias is a movable tag (stable, stable-16k...) that points at the
# manifest list of the last green build of that channel, so a later workflow
# can rebuild FROM <image>:stable@sha256:<digest>. The "latest" tag maps to the
# bare channel name; any other tag keeps its suffix after the channel.
#
# Arguments:
#   $1 - release channel (edge, stable)
#   $2 - image tag (latest, latest-16k, ...)
# Outputs:
#   The alias tag on stdout.
# Returns:
#   1 when an argument is missing.
set -euo pipefail

channel="${1:?channel required}"
tag="${2:?tag required}"

case "${tag}" in
  latest) echo "${channel}" ;;
  latest-*) echo "${channel}-${tag#latest-}" ;;
  *) echo "${channel}-${tag}" ;;
esac
