#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Stage profile-owned containerd registration after staging its actual assets."""

import argparse
import json
from pathlib import Path
import re
from elf_dependencies import dependencies as elf_dependencies


HERE = Path(__file__).resolve().parent.parent / "packaging/ubuntu-26.04"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    profiles = json.loads((HERE / "profiles/profiles.json").read_text())
    parser.add_argument("--format", choices=("deb", "rpm"), default="deb")
    parser.add_argument("profile", choices=profiles)
    parser.add_argument("--architecture", required=True, choices=("amd64", "arm64"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--root", required=True, type=Path, help="existing package staging root")
    args = parser.parse_args()
    profile = profiles[args.profile]
    if args.architecture not in profile["architectures"]:
        parser.error(f"{args.profile} is not available for {args.architecture}")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", args.version):
        parser.error("invalid Debian package version")
    config = args.root / "opt/kata/share/defaults/kata-containers" / f"configuration-{args.profile}.toml"
    if not config.is_file():
        parser.error(f"stage the profile payload and its configuration first: {config}")
    # The component recipe owns payload/verity validation; this helper stages
    # only registration and package dependency metadata, not VMM/guest builds.
    vendor = args.root / "usr/share/kata-containers/containerd" / f"{args.profile}.toml"
    vendor.parent.mkdir(parents=True, exist_ok=True)
    vendor.write_bytes((HERE / "profiles" / f"{args.profile}.toml").read_bytes())
    vendor.chmod(0o644)
    link = args.root / "etc/containerd/conf.d" / f"50-kata-{args.profile}.toml"
    link.parent.mkdir(parents=True, exist_ok=True)
    # A package-owned link is removed on `apt remove`, so an uninstalled profile
    # leaves no active handler. Admin overrides belong in separate 90-*.toml files.
    link.symlink_to(f"/usr/share/kata-containers/containerd/{args.profile}.toml")
    marker = args.root / "usr/share/kata-containers/installed-profiles" / f"{args.profile}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(profile, indent=2) + "\n")
    modules = args.root / "usr/lib/modules-load.d" / f"kata-{args.profile}.conf"
    modules.parent.mkdir(parents=True, exist_ok=True)
    modules.write_text("# Installed Kata profile modules\n" + "\n".join(profile["modules"]) + "\n")
    dependencies = [f"kata-containers-core (= {args.version})"] + [
        dep.replace("${binary:Version}", args.version)
        for dep in profile["extra_dependencies"]
    ]
    libraries = elf_dependencies(args.root) if args.format == "deb" else ""
    if libraries:
        dependencies.append(libraries)
    control = args.root / "DEBIAN"
    control.mkdir(exist_ok=True)
    postinst = control / "postinst"
    postinst.write_text(
        '#!/bin/sh\n# SPDX-License-Identifier: MIT\nset -eu\nif [ "$1" = configure ]; then\n'
        f'  /usr/libexec/kata-containers/load-modules --profile {args.profile} || '
        'echo "Kata: host module requirements remain unsatisfied; retry before workloads" >&2\nfi\n'
    )
    postinst.chmod(0o755)
    (control / "control").write_text(
        f"Package: kata-containers-{args.profile}\nVersion: {args.version}\n"
        f"Architecture: {args.architecture}\nSection: admin\nPriority: optional\n"
        "Maintainer: Kata Containers community <kata-dev@lists.katacontainers.io>\n"
        f"Depends: {', '.join(dependencies)}\n"
        f"Description: Kata Containers {args.profile} profile\n"
        f" {profile['assets']}.\n"
    )


if __name__ == "__main__":
    main()
