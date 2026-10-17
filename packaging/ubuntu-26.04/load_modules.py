#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Load base and installed-profile host modules without unloading shared modules."""

import argparse
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"), help="root for offline tests")
    parser.add_argument("--architecture", choices=("amd64", "arm64"))
    parser.add_argument("--profile")
    parser.add_argument("--all-installed", action="store_true")
    parser.add_argument("--modprobe", default=shutil.which("modprobe"))
    args = parser.parse_args()
    arch = args.architecture or {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    requirements = json.loads((args.root / "usr/share/kata-containers/host-requirements.json").read_text())
    if arch not in requirements:
        parser.error(f"unsupported architecture {arch}")
    required = list(requirements[arch]["required"])
    optional = list(requirements[arch]["optional"])
    cpuinfo = args.root / "proc/cpuinfo"
    if arch == "amd64" and cpuinfo.exists():
        cpu = cpuinfo.read_text()
        if "GenuineIntel" in cpu:
            optional.append("kvm_intel")
        elif "AuthenticAMD" in cpu:
            optional.append("kvm_amd")
    registry = args.root / "usr/share/kata-containers/installed-profiles"
    markers = sorted(registry.glob("*.json")) if args.all_installed else []
    if args.profile:
        if not re.fullmatch(r"[a-z0-9-]+", args.profile):
            parser.error("invalid profile name")
        markers.append(registry / f"{args.profile}.json")
    for marker in markers:
        profile = json.loads(marker.read_text())
        if arch not in profile["architectures"]:
            parser.error(f"{marker.stem} is unavailable for {arch}")
        required.extend(profile["modules"])
    builtin = args.root / "lib/modules" / platform.release() / "modules.builtin"
    builtin_names = set()
    if builtin.exists():
        builtin_names = {Path(line).name.split(".ko")[0].replace("-", "_")
                         for line in builtin.read_text().splitlines()}
    errors = []
    for module in dict.fromkeys(optional + required):
        if not re.fullmatch(r"[a-zA-Z0-9_]+", module):
            parser.error(f"invalid module name {module!r}")
        if (args.root / "sys/module" / module).exists() or module in builtin_names:
            continue
        if args.modprobe:
            result = subprocess.run([args.modprobe, module], capture_output=True, text=True)
            detail = result.stderr.strip()
            failed = result.returncode != 0
        else:
            failed, detail = True, "install kmod"
        if failed:
            print(f"Kata: cannot load {module}: {detail}", file=sys.stderr)
            if module in required:
                errors.append(module)
    if errors:
        print("Kata: required host modules unavailable: " + ", ".join(errors) +
              ". Install/enable them for the running kernel.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
