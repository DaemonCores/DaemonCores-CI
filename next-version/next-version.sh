#!/usr/bin/env bash
# Compute the next version from Conventional Commits.
#
# Reads the commits between the last stable tag (vMAJOR.MINOR.PATCH, no
# pre-release suffix) and REF, derives the bump from their subjects and bodies
# and prints:
#   last   last stable version (0.0.0 when there is none)
#   bump   major | minor | patch | none
#   next   version a stable release would get (equals last when bump is none)
#   edge   pre-release version of REF: <next>-edge.<commits since last>
#
# Environment:
#   REF     commit-ish to evaluate (default HEAD)
#   PREFIX  tag prefix (default v)
# Outputs go to $GITHUB_OUTPUT when set, to stdout otherwise.
set -euo pipefail

REF="${REF:-HEAD}"
PREFIX="${PREFIX:-v}"

last_tag=$(git tag --list "${PREFIX}[0-9]*.[0-9]*.[0-9]*" --merged "$REF" --sort=-v:refname |
	grep -E "^${PREFIX}[0-9]+\.[0-9]+\.[0-9]+$" | head -n1 || true)
last="${last_tag#"$PREFIX"}"
last="${last:-0.0.0}"

range="$REF"
[ -n "$last_tag" ] && range="${last_tag}..${REF}"

re_breaking='^[a-z]+(\([^)]*\))?!:'
re_feat='^feat(\([^)]*\))?:'
re_fix='^(fix|perf|revert)(\([^)]*\))?:'

bump="none"
count=0
while IFS= read -r -d $'\x1e' record; do
	record="${record#$'\n'}"
	[ -z "$record" ] && continue
	count=$((count + 1))
	subject="${record%%$'\x1f'*}"
	body="${record#*$'\x1f'}"
	if [[ "$subject" =~ $re_breaking ]] || grep -qE '^BREAKING[ -]CHANGE:' <<<"$body"; then
		bump=major
	elif [[ "$subject" =~ $re_feat ]]; then
		if [ "$bump" != major ]; then bump=minor; fi
	elif [[ "$subject" =~ $re_fix ]]; then
		if [ "$bump" = "none" ]; then bump="patch"; fi
	fi
done < <(git log "$range" --format='%s%x1f%b%x1e')

IFS=. read -r major minor patch <<<"$last"
case "$bump" in
major) next="$((major + 1)).0.0" ;;
minor) next="${major}.$((minor + 1)).0" ;;
patch) next="${major}.${minor}.$((patch + 1))" ;;
*) next="$last" ;;
esac
edge="${next}-edge.${count}"

#######################################
# Print a result as name=value, to GITHUB_OUTPUT when it is set.
# Globals:
#   GITHUB_OUTPUT
# Arguments:
#   $1: output name
#   $2: output value
# Outputs:
#   Appends to GITHUB_OUTPUT, or writes to stdout.
#######################################
emit() {
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"
	else
		printf '%s=%s\n' "$1" "$2"
	fi
}
emit last "$last"
emit bump "$bump"
emit next "$next"
emit edge "$edge"
