#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
#
# Builds the semantic_autonomy image. The GitHub token of the private repositories is GITHUB_TOKEN, or the one of the
# gh CLI: it goes to the build as a secret, never into the image layers.
#   docker/build.sh                 YARP 3.12, tag semantic_autonomy:latest
#   docker/build.sh --yarp4         YARP 4,    tag semantic_autonomy:yarp4
#   docker/build.sh --refresh       clones the repositories again (new commits), keeping the cache of the rest
# Branches and other build args go through as they are, for example:
#   docker/build.sh --build-arg MA_BRANCH=main --no-cache
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="$ROOT/docker/Dockerfile"
TAG="semantic_autonomy:latest"
extra=()
while [ $# -gt 0 ]; do
    case "$1" in
        --yarp4)   DOCKERFILE="$ROOT/docker/Dockerfile_yarp4"; TAG="semantic_autonomy:yarp4" ;;
        --refresh) extra+=(--build-arg "REPOS_REV=$(date +%s)") ;;
        --tag)     TAG="$2"; shift ;;
        -h|--help) sed -n '5,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         extra+=("$1") ;;
    esac
    shift
done
TAG="${IMAGE:-$TAG}"

if [ -z "${GITHUB_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then
    GITHUB_TOKEN="$(gh auth token 2>/dev/null || true)"
fi
if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "Set GITHUB_TOKEN (or log in with gh): visual-language-navigation and yarp-mcp* are private" >&2
    exit 1
fi
export GITHUB_TOKEN

echo "Building $TAG from ${DOCKERFILE#$ROOT/}"
DOCKER_BUILDKIT=1 docker build --secret id=github_token,env=GITHUB_TOKEN \
    -t "$TAG" -f "$DOCKERFILE" "${extra[@]}" "$ROOT"
echo "Done: start it with IMAGE=$TAG docker/run.sh"
