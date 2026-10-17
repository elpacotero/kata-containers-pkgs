# SPDX-License-Identifier: MIT
"""Validate immutable native-package build requests."""
import re


def validate_request_version(version):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9.]+", version):
        raise ValueError("invalid requested package version")
    return version


def validate_request(request):
    if request.get("schema_version") != 1 or request.get("channel") not in ("release", "development"):
        raise ValueError("unsupported build request")
    for name in ("source_revision", "packaging_revision"):
        if not re.fullmatch(r"[0-9a-f]{40}", request.get(name, "")):
            raise ValueError(f"invalid {name}")
    validate_request_version(request.get("package_version", ""))
    profiles = request.get('profiles', ['qemu'])
    if (not isinstance(profiles, list) or not profiles or any(not isinstance(profile, str) for profile in profiles)
            or len(set(profiles)) != len(profiles) or not set(profiles).issubset({'qemu', 'nvidia-cpu', 'nvidia-gpu', 'nvidia-gpu-tee'})):
        raise ValueError('invalid requested profiles')
    fingerprint = request.get("packaging_fingerprint")
    if fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("invalid packaging fingerprint")
    upstream = request.get("upstream")
    if upstream:
        if upstream.get("source_revision") != request["source_revision"] or upstream.get("source_ref") != request.get("source_ref"):
            raise ValueError("upstream payload provenance does not match the build request")
        if upstream.get("kind") != request["channel"]:
            raise ValueError("upstream payload channel mismatch")
    return request

