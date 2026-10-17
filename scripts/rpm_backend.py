#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build native RPMs from the same staged payloads used by the DEB assembler."""
from email.parser import Parser
from pathlib import Path
import re
import shutil
import subprocess


def file_entries(stage):
    result = []
    for path in sorted(stage.rglob("*")):
        relative = path.relative_to(stage)
        if relative.parts[0] == "DEBIAN":
            continue
        target = '/' + relative.as_posix()
        if any(c in target for c in ('%', '\n', '"')):
            raise ValueError(f"unsupported RPM payload filename: {target}")
        directory = path.is_dir() and not path.is_symlink()
        if directory:
            # AlmaLinux's filesystem package owns system directories and some
            # have modes different from the source tarballs (e.g. /usr/lib).
            # Own only Kata-specific directories and the profile drop-in dirs.
            private = (target == "/opt/kata" or target.startswith("/opt/kata/") or
                       target.startswith("/usr/share/kata-containers") or
                       target.startswith("/usr/share/doc/kata-containers") or
                       target == "/etc/containerd/conf.d")
            if not private:
                continue
        directive = "%dir " if directory else ""
        if target == "/etc/kata-containers/nydus-snapshotter.toml":
            directive = "%config(noreplace) "
        if relative.parts[:3] == ("usr", "share", "doc") and path.name.startswith("LICENSE"):
            directive = "%license "
        result.append(directive + '"' + target + '"')
    return result


def build(stage, output, version, architecture):
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)-([0-9.]+)", version)
    if not match:
        raise ValueError("RPM versions must have form MAJOR.MINOR.PATCH-REVISION")
    package_version, release = match.groups()
    release += ".el10"
    metadata = Parser().parsestr((stage / "DEBIAN/control").read_text())
    name = metadata["Package"]
    top = stage.parent / (name + "-rpmbuild")
    top.mkdir()
    requires = []
    for dependency in (metadata["Depends"] or "").split(","):
        dependency = dependency.strip()
        if not dependency:
            continue
        dependency = re.sub(r" \(= ([^)]+)\)", lambda m: " = " + m[1] + ".el10", dependency)
        dependency = re.sub(r" \((>=|<=|>|<) ([^)]+)\)", r" \1 \2", dependency)
        requires.append(dependency)
    build_arch = "noarch" if metadata["Architecture"] == "all" else architecture
    lines = ["%global debug_package %{nil}", "%global _build_id_links none",
             # Preserve kernel/firmware/guest and prebuilt Rust payloads exactly.
             "%global __os_install_post %{nil}",
             f"Name: {name}", f"Version: {package_version}", f"Release: {release}",
             f"Summary: {metadata['Description'].splitlines()[0]}",
             "License: MIT AND Apache-2.0" if name in ("kata-containers", "kata-containers-core") else
             "License: MIT AND Apache-2.0 AND GPL-2.0-only AND BSD-2-Clause AND BSD-3-Clause",
             "# Bundled-component license inventory is provisional; publication remains blocked on completion.",
             f"BuildArch: {build_arch}"]
    lines += [f"Requires: {dependency}" for dependency in requires]
    lines += ["", "%description", str(metadata["Description"]), "", "%prep", "", "%build", "",
              "%install", f"cp -a '{stage}/.' '%{{buildroot}}/'", "rm -rf '%{buildroot}/DEBIAN'", ""]
    postinst = stage / "DEBIAN/postinst"
    if postinst.exists():
        post = postinst.read_text().replace('"$1" = configure', '"$1" -ge 1')
        lines += ["%post", post.replace("%", "%%"), ""]
    for filename, section in (('prerm', '%preun'), ('postrm', '%postun')):
        script = stage / 'DEBIAN' / filename
        if script.exists():
            text = script.read_text().replace('"$1" = remove', '"$1" -eq 0')
            lines += [section, text.replace('%', '%%'), '']
    lines += ["%files"]
    lines += file_entries(stage)
    spec = top / (name + ".spec")
    spec.write_text("\n".join(lines) + "\n")
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    log = logs / (name + ".log")
    with log.open("w") as stream:
        result = subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", str(spec)],
                                stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"rpmbuild failed for {name}; see {log}")
    built = list((top / "RPMS").rglob("*.rpm"))
    if len(built) != 1:
        raise ValueError(f"expected one RPM for {name}, found {built}")
    shutil.copyfile(built[0], output / built[0].name)
    specs = output / "specs"
    specs.mkdir(exist_ok=True)
    shutil.copyfile(spec, specs / spec.name)
