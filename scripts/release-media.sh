#!/bin/bash
#
# release-media.sh - attach install media to the release of the version.
#
# The media (ISO, disk images) are built before the green pipeline that makes
# a release, so they cannot be uploaded to a tag that does not exist yet. They
# go to a draft release named pending-<sha>; on a green pipeline the release
# workflow turns that draft into the version release (tag, notes, flags), or
# creates the release when no media were built. A draft has no tag, hence the
# release id and the uploads API instead of "gh release upload <tag>".
#
# Usage:
#   release-media.sh draft <sha>                    print the draft release id
#   release-media.sh upload <id> <file>...          (re)upload assets
#   release-media.sh publish <sha> <tag> <channel>  make the release public
# Environment:
#   GITHUB_REPOSITORY  owner/name
#   GH_TOKEN           token with contents: write
#   RELEASE_NOTES      notes of an edge release (default: generic)
set -euo pipefail

repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
api="repos/${repo}/releases"

#######################################
# Print the id of the draft release of a commit, empty when there is none.
# Arguments:
#   $1: commit sha
#######################################
find_draft() {
  gh api --paginate "${api}" \
    --jq ".[] | select(.draft and .tag_name == \"pending-$1\") | .id" | head -n1
}

#######################################
# Print the draft release id of a commit, creating the draft when needed.
# Arguments:
#   $1: commit sha
#######################################
cmd_draft() {
  local id
  id=$(find_draft "$1")
  if [[ -z "${id}" ]]; then
    id=$(gh api -X POST "${api}" -f "tag_name=pending-$1" \
      -f "target_commitish=$1" -f "name=pending-$1" \
      -f "body=Install media of $1, published with the next release." \
      -F draft=true --jq .id)
  fi
  echo "${id}"
}

#######################################
# Upload assets to a release, replacing any asset of the same name: an
# interrupted upload leaves a pending asset that a plain re-upload rejects
# with HTTP 422 already_exists.
# Arguments:
#   $1: release id
#   $2...: files
#######################################
cmd_upload() {
  local id="$1" file name old
  shift
  for file in "$@"; do
    name=$(basename "${file}")
    old=$(gh api --paginate "${api}/${id}/assets" \
      --jq ".[] | select(.name == \"${name}\") | .id")
    if [[ -n "${old}" ]]; then
      gh api -X DELETE "${api}/assets/${old}" >/dev/null
    fi
    curl -fsS -X POST \
      -H "Authorization: Bearer ${GH_TOKEN}" \
      -H "Content-Type: application/octet-stream" \
      --data-binary "@${file}" \
      "https://uploads.github.com/${api}/${id}/assets?name=${name}" >/dev/null
  done
}

#######################################
# Publish the release of a version: promote the draft of the commit, or create
# the release when the commit has no media.
# Arguments:
#   $1: commit sha
#   $2: tag
#   $3: channel (edge or stable)
#######################################
cmd_publish() {
  local sha="$1" tag="$2" channel="$3" id notes prerelease latest
  case "${channel}" in
    edge)
      prerelease=true
      latest=false
      notes="${RELEASE_NOTES:-Automatic edge build of ${sha}.}"
      ;;
    stable)
      prerelease=false
      latest=true
      notes=$(gh api -X POST "${api}/generate-notes" -f "tag_name=${tag}" \
        -f "target_commitish=${sha}" --jq .body)
      ;;
    *)
      echo "::error::Unknown channel '${channel}'." >&2
      return 1
      ;;
  esac
  id=$(find_draft "${sha}")
  if [[ -n "${id}" ]]; then
    gh api -X PATCH "${api}/${id}" -f "tag_name=${tag}" -f "name=${tag}" \
      -f "target_commitish=${sha}" -f "body=${notes}" \
      -F draft=false -F "prerelease=${prerelease}" \
      -f "make_latest=${latest}" >/dev/null
  else
    gh api -X POST "${api}" -f "tag_name=${tag}" -f "name=${tag}" \
      -f "target_commitish=${sha}" -f "body=${notes}" \
      -F "prerelease=${prerelease}" -f "make_latest=${latest}" >/dev/null
  fi
}

main() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "${cmd}" in
    draft) cmd_draft "$@" ;;
    upload) cmd_upload "$@" ;;
    publish) cmd_publish "$@" ;;
    *)
      echo "usage: release-media.sh draft|upload|publish ..." >&2
      return 2
      ;;
  esac
}

main "$@"
