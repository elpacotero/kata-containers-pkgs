#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Resolve ELF library dependencies using the target distro's package metadata."""

from pathlib import Path
import subprocess
import sys
import tempfile


def dependencies(root):
    root = Path(root).resolve()
    files = [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()
             and "DEBIAN" not in p.relative_to(root).parts]
    elf = []
    for path in files:
        with path.open("rb") as stream:
            if stream.read(4) == b"\x7fELF":
                elf.append(path)
    if not elf:
        return ""
    libraries = sorted({str(p.parent) for p in files if ".so" in p.name})
    command = ["dpkg-shlibdeps", "-O"] + [f"-l{p}" for p in libraries]
    command += [f"-e{path}" for path in elf]
    # dpkg-shlibdeps needs source-package metadata even for standalone staging.
    with tempfile.TemporaryDirectory(prefix="kata-shlibdeps-") as directory:
        debian = Path(directory) / "debian"
        debian.mkdir()
        (debian / "control").write_text(
            "Source: kata-containers\nSection: admin\nPriority: optional\n"
            "Maintainer: Kata Containers community <kata-dev@lists.katacontainers.io>\n\n"
            "Package: kata-containers\nArchitecture: any\nDescription: Kata Containers\n")
        result = subprocess.check_output(command, cwd=directory, text=True)
    for line in result.splitlines():
        if line.startswith("shlibs:Depends="):
            return line.split("=", 1)[1]
    return ""


if __name__ == "__main__":
    print("shlibs:Depends=" + dependencies(sys.argv[1]))
