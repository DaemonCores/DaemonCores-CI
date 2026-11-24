#!/bin/bash
#
# channel-published.sh - tell whether a release channel already has a live APT
# repository on the Pages site.
#
# A channel's public key only exists on the site once its repository was
# published. A pipeline that skips the debs stage (nothing changed) can then
# never publish it, and everything that derives the key pin from that URL is
# stuck. The plan calls this to owe the publication in that case.
#
# Arguments:
#   $1 - site base URL (no trailing slash)
#   $2 - channel (edge, stable, security)
#   $3 - APT suite (default: trixie)
# Returns:
#   0 the channel has a Release file on the site
#   1 the channel is not published (the server answered that it is absent)
#   2 unknown: the site could not be read; never to be read as published
set -euo pipefail

site="${1:?site base URL required}"
channel="${2:?channel required}"
suite="${3:-trixie}"

rc=0
curl --retry 3 -fsSL -o /dev/null "${site}/${channel}/dists/${suite}/Release" \
  2>/dev/null || rc=$?
case "${rc}" in
  0) exit 0 ;;
  22 | 37) exit 1 ;; # HTTP 4xx, or the file is absent (file:// base)
  *) exit 2 ;;
esac
