#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fetch Kata, build the QEMU installation from source, and assemble local DEBs or RPMs."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def run(*command, **kwargs):
    subprocess.run(list(map(str, command)), check=True, **kwargs)


def main():
    config = json.loads((ROOT / "config.json").read_text())
    request_file = ROOT / "build-request.json"
    request = None
    if request_file.exists():
        from build_request import validate_request
        request = validate_request(json.loads(request_file.read_text()))
        if request["source_repository"] != config["source_repository"] or request["source_revision"] != config["source_revision"]:
            raise ValueError("SCM snapshot config does not match its build request")
        if request["package_version"] != config["package_version"]:
            raise ValueError("SCM snapshot package version does not match its build request")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / "work")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", default=config["package_version"])
    parser.add_argument("--format", choices=("deb", "rpm"), default="deb")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--prepare-only", action="store_true", help="fetch and adapt source without compiling")
    args = parser.parse_args()
    if args.plan:
        print(f"Fetch {config['source_repository']} @ {config['source_revision']}", flush=True)
        run(sys.executable, HERE / "build_sources.py", "--plan")
        print(f"Assemble core + rootfs-generic + kernel-generic + virtiofsd + qemu + metapackage, version {args.version}; no uploads")
        return
    work = args.work_dir.resolve()
    source = work / "kata"
    work.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        source.mkdir()
        run("git", "init", source)
        run("git", "-C", source, "remote", "add", "origin", config["source_repository"])
        run("git", "-C", source, "fetch", "--depth=1", "origin", config["source_revision"])
        run("git", "-C", source, "checkout", "--detach", "FETCH_HEAD")
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != config["source_revision"]:
        parser.error("existing source checkout does not match config.json; choose a new work directory")
    # The upstream x86 helper otherwise downloads a released virtiofsd binary.
    # Apply an idempotent local adaptation so both architectures compile it.
    helper = source / "tools/packaging/static-build/virtiofsd/build-static-virtiofsd.sh"
    text = helper.read_text()
    old = "pull_virtiofsd_released_binary || build_virtiofsd_from_source"
    new = "# Kata packaging: always compile virtiofsd.\nbuild_virtiofsd_from_source"
    if old in text:
        helper.write_text(text.replace(old, new))
    elif new not in text:
        parser.error("upstream virtiofsd helper changed; review source-build adaptation")
    if args.prepare_only:
        print(f"Prepared source: {source}")
        return
    command = [sys.executable, HERE / "build_sources.py", "--source", source]
    if args.resume:
        command.append("--resume")
    run(*command)
    build = source / "tools/packaging/kata-deploy/local-build/build"
    inputs = work / "inputs"
    inputs.mkdir(exist_ok=True)
    status = json.loads((build / "ubuntu-source-build/status.json").read_text())
    from build_sources import COMPONENTS
    for component in COMPONENTS:
        archive = build / f"kata-static-{component}.tar.zst"
        target = inputs / archive.name
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(archive)
    if request:
        shutil.copyfile(request_file, inputs / "build-request.json")
    elif (inputs / "build-request.json").exists():
        (inputs / "build-request.json").unlink()
    (inputs / "source-build.json").write_text(json.dumps(status, indent=2) + "\n")
    (inputs / "source-patch.diff").write_bytes(subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=source))
    shutil.copyfile(source / "LICENSE", inputs / "LICENSE.upstream")
    shutil.copyfile(source / "versions.yaml", inputs / "versions.yaml")
    run(sys.executable, HERE / "assemble.py", "--inputs", inputs,
        "--output", args.output.resolve(), "--version", args.version, "--format", args.format)


if __name__ == "__main__":
    main()
