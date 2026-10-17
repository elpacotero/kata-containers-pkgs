#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate metapackage dependency resolution on a disposable Ubuntu 26.04 host."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
from packages import build_packages, installed_packages, PROFILES

HERE = Path(__file__).resolve().parent
PACKAGES = tuple("kata-containers" + suffix for suffix in (
    "", "-core", "-qemu", "-rootfs-generic", "-kernel-generic", "-virtiofsd"))
ENV = dict(os.environ, DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="l")


def run(*command):
    return subprocess.check_output(command, text=True)


@contextmanager
def apt_repository(debs):
    # Keep APT sources and indexes private; no persistent repository registration.
    with tempfile.TemporaryDirectory(prefix="kata-apt-", dir="/var/tmp") as directory:
        root = Path(directory)
        root.chmod(0o755)
        repo = root / "repo"
        repo.mkdir(mode=0o755)
        for deb in debs:
            shutil.copyfile(deb, repo / deb.name)
            (repo / deb.name).chmod(0o644)
        with (repo / "Packages").open("w") as stream:
            subprocess.run(["dpkg-scanpackages", "--multiversion", ".", "/dev/null"],
                           cwd=repo, stdout=stream, check=True)
        sources = root / "sources"
        sources.mkdir(mode=0o755)
        for source in Path("/etc/apt/sources.list.d").glob("*"):
            if source.suffix in (".list", ".sources"):
                shutil.copyfile(source, sources / source.name)
        if Path("/etc/apt/sources.list").exists():
            shutil.copyfile("/etc/apt/sources.list", sources / "system.list")
        (sources / "kata-poc.list").write_text(f"deb [trusted=yes] file:{repo} ./\n")
        lists = root / "lists"
        lists.mkdir(mode=0o755)
        options = ["-o", "Acquire::Languages=none", "-o", "Dir::Etc::sourcelist=/dev/null", "-o", f"Dir::Etc::sourceparts={sources}",
                   "-o", f"Dir::State::lists={lists}"]
        subprocess.run(["apt-get", *options, "update"], env=ENV, check=True)
        yield options


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="qemu")
    parser.add_argument("--install", action="store_true", required=True,
                        help="explicitly install on this disposable builder")
    parser.add_argument("--fresh", action="store_true", help="remove existing POC packages before installing")
    args = parser.parse_args()
    selected = installed_packages(args.profile)
    entry = 'kata-containers' if args.profile == 'qemu' else 'kata-containers-' + args.profile
    args.packages = args.packages.resolve()
    release = Path("/etc/os-release").read_text()
    if os.geteuid() or 'VERSION_ID="26.04"' not in release or 'ID=ubuntu\n' not in release:
        parser.error("run as root on a disposable Ubuntu 26.04 Linux builder")
    if not shutil.which("containerd"):
        parser.error("install system containerd before running the standard-path validator; Kata no longer supplies the cluster CRI")
    debs = sorted(args.packages.glob("*.deb"))
    names = {run("dpkg-deb", "-f", str(p), "Package").strip() for p in debs}
    if len(debs) != len(names) or not set(selected).issubset(names) or names not in (set(PACKAGES), set(build_packages(['qemu', 'nvidia-cpu'])), set(build_packages())):
        parser.error('expected a complete native Kata package set containing the selected profile')
    versions = {run("dpkg-deb", "-f", str(p), "Version").strip() for p in debs}
    if len(versions) != 1:
        parser.error("all packages must have the same version")
    version = versions.pop()
    if args.fresh:
        installed = [name for name in build_packages() if subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", name], capture_output=True, text=True).stdout == "install ok installed"]
        if installed:
            subprocess.run(["apt-get", "remove", "-y", *installed], env=ENV, check=True)
    with apt_repository(debs) as options:
        # Only the metapackage is requested. Its dependency graph must bring all
        # other Kata packages and Ubuntu library/host dependencies into the plan.
        subprocess.run(["apt-get", *options, "install", "-y", "--allow-downgrades",
                        "--no-install-recommends", f"{entry}={version}"], env=ENV, check=True)
    installed_versions = {}
    for name in selected:
        assert run("dpkg-query", "-W", "-f=${Status}", name) == "install ok installed"
        installed_versions[name] = run("dpkg-query", "-W", "-f=${Version}", name)
        assert installed_versions[name] == version, f"unexpected version: {name}"
        assert not run("dpkg", "--verify", name).strip(), f"installed files changed: {name}"
    arch = run("dpkg", "--print-architecture").strip()
    binaries = {"shim": "/opt/kata/bin/containerd-shim-kata-v2",
                "qemu": "/opt/kata/bin/qemu-system-" + {"arm64": "aarch64", "amd64": "x86_64"}[arch] + ("-no-shared-fs" if args.profile.startswith("nvidia-") else "")}
    if args.profile == "qemu":
        binaries["virtiofsd"] = "/opt/kata/libexec/virtiofsd"
    outputs = {name: run(path, "--version").strip() for name, path in binaries.items()}
    profile_path = Path("/opt/kata/share/defaults/kata-containers/configuration-" + args.profile + ".toml")
    hypervisor = tomllib.loads(profile_path.read_text())["hypervisor"]["qemu"]
    expected_owners = {"path": "kata-containers-qemu", "kernel": "kata-containers-kernel-generic",
                       "image": "kata-containers-rootfs-generic", "virtio_fs_daemon": "kata-containers-virtiofsd",
                       "firmware": "kata-containers-qemu", "firmware_volume": "kata-containers-qemu"}
    if args.profile.startswith('nvidia-'):
        expected_owners.update(path='kata-containers-nvidia-cpu', kernel='kata-containers-kernel-nvidia',
                               image='kata-containers-rootfs-nvidia')
        expected_owners.pop('virtio_fs_daemon')
    if args.profile == 'nvidia-gpu':
        expected_owners.update(firmware='kata-containers-nvidia-gpu', firmware_volume='kata-containers-nvidia-gpu')
    owners = {}
    for key, owner in expected_owners.items():
        path = hypervisor.get(key)
        if not path:
            continue
        assert Path(path).is_file(), f"missing runtime asset: {path}"
        actual = run("dpkg-query", "-S", path).strip().split(": ", 1)[0]
        assert actual == owner, f"wrong owner for {path}: {actual}"
        owners[key] = {"path": path, "package": actual}
    setup = ["kata-containers-setup", "--k8s-distro", "kubeadm"]
    before = Path("/etc/containerd/config.toml").read_bytes()
    preview = run(*setup, "--dry-run")
    assert Path("/etc/containerd/config.toml").read_bytes() == before, "dry-run changed config"
    applied = run(*setup, "--apply")
    assert "already configured" in run(*setup, "--apply"), "setup is not idempotent"
    args.packages.joinpath("setup-dry-run.diff").write_text(preview)
    args.packages.joinpath("setup-apply.txt").write_text(applied)
    config = tomllib.loads(run("containerd", "config", "dump"))
    runtimes = config["plugins"]["io.containerd.cri.v1.runtime"]["containerd"]["runtimes"]
    present = [p for p in PROFILES if Path("/usr/share/kata-containers/installed-profiles/" + p + ".json").is_file()]
    assert {name for name in runtimes if name.startswith("kata")} == {PROFILES[p]["handler"] for p in present}
    assert runtimes[PROFILES[args.profile]["handler"]]["options"]["ConfigPath"] == str(profile_path)
    assert config["plugins"]["io.containerd.cri.v1.runtime"]["containerd"]["default_runtime_name"] == "runc"
    subprocess.run(["python3", str(HERE / "verify_image.py")], check=True)
    subprocess.run(["apt-get", "check"], check=True)
    result = {"packages": installed_versions, "binaries": outputs, "asset_owners": owners,
              "installation": "clean" if args.fresh else "upgrade-or-install",
              "apt_requested": f"{entry}={version}", "installed_files_verified": True,
              "guest_verity_trees_verified": True, "containerd_profiles": [PROFILES[p]["handler"] for p in present],
              "limits": "No KVM or full CRI workload validation"}
    report = "validation-clean.json" if args.fresh else "validation-upgrade.json"
    (args.packages / report).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
