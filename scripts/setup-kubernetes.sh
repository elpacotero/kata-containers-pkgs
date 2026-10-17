#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Reuse the pinned upstream Kata CI setup on disposable test hosts.
set -euxo pipefail
cd "$1"
INSTALL_IN_GOPATH=false bash ci/install_yq.sh
# shellcheck disable=SC1091
source tests/gha-run-k8s-common.sh

if [[ "${KUBERNETES}" == rke2 ]]; then
    # The redirect channel service can return 404 and its installer then treats
    # "stable" as a release tag. Resolve the same channel's authoritative data.
    curl --fail --show-error --silent --location --retry 3 --max-time 120 \
        -o "$2/rke2-channels.yaml" \
        https://raw.githubusercontent.com/rancher/rke2/master/channels.yaml
    INSTALL_RKE2_VERSION=$(yq -r '.channels[] | select(.name == "stable") | .latest' "$2/rke2-channels.yaml")
    [[ "${INSTALL_RKE2_VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+\+rke2r[0-9]+$ ]] || die 'Invalid RKE2 stable channel version'
    export INSTALL_RKE2_VERSION
    printf '%s\n' "${INSTALL_RKE2_VERSION}" > "$2/rke2-version.txt"
    # Preserve only this selection across upstream's "sudo sh" invocation.
    sudo() { command sudo --preserve-env=INSTALL_RKE2_VERSION "$@"; }
fi

if [[ "${KUBERNETES}" == vanilla && "${KATA_PACKAGE_PROFILE:-qemu}" == nvidia-cpu ]]; then
    # Resolve the exact latest stable tag once. Keep upstream Kata's installer,
    # but prevent its patch-release lookup from selecting a prerelease.
    curl --fail --show-error --silent --location --retry 3 --max-time 120 \
        -o "$2/containerd-release.json" \
        https://api.github.com/repos/containerd/containerd/releases/latest
    KATA_CONTAINERD_TAG=$(python3 - "$2/containerd-release.json" <<'PY'
import json, re, sys
release = json.load(open(sys.argv[1]))
tag = release["tag_name"]
if release.get("prerelease") or release.get("draft") or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
    raise SystemExit("Expected a stable containerd release")
if tuple(map(int, tag[1:].split("."))) < (2, 3, 0):
    raise SystemExit("NVIDIA CPU requires containerd >= 2.3.0")
print(tag)
PY
    )
    # Save the upstream resolver for other dependencies such as cri-tools.
    resolver=$(declare -f get_latest_patch_release_from_a_github_project)
    eval "${resolver/get_latest_patch_release_from_a_github_project/kata_original_patch_release}"
    get_latest_patch_release_from_a_github_project() {
        if [[ "$1" == containerd/containerd ]]; then
            printf '%s\n' "${KATA_CONTAINERD_TAG}"
        else
            kata_original_patch_release "$@"
        fi
    }
    export CONTAINER_ENGINE_VERSION="${KATA_CONTAINERD_TAG%.*}"
elif [[ "${KUBERNETES}" == vanilla ]]; then
    # The POC tests the distribution's containerd, rather than downloading
    # the upstream binary bundle in Kata CI's default implementation.
    install_cri_containerd() {
        sudo apt-get install -y containerd runc
        [[ "$(command -v containerd)" == /usr/bin/containerd ]]
        sudo mkdir -p /etc/containerd
        /usr/bin/containerd config default |
            sed -E 's/^([[:space:]]*SystemdCgroup[[:space:]]*=[[:space:]]*)false/\1true/' |
            sudo tee /etc/containerd/config.toml >/dev/null
        sudo systemctl enable containerd
        dpkg-query -W containerd runc
        dpkg-query -S /usr/bin/containerd
    }
fi

if [[ "${KATA_CONTAINERD_ONLY:-false}" == true ]]; then
    [[ "${KATA_PACKAGE_PROFILE:-}" == nvidia-cpu ]] || die 'Standalone containerd setup requires the NVIDIA base profile'
    install_cri_containerd "${CONTAINER_ENGINE_VERSION}"
    containerd --version > "$2/containerd-version.txt"
else
    deploy_k8s
fi
