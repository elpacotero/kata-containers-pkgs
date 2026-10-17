#!/bin/sh
# SPDX-License-Identifier: MIT
# Worker entry point; requires network, Docker and privileged guest-image builds.
set -eu
exec python3 "$(dirname "$0")/../scripts/build.py" "$@"
