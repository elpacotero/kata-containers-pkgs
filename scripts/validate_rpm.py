#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install the AlmaLinux RPM metapackage and verify its complete dependency graph."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import tomllib

from validate import PACKAGES, validate_tee
from packages import build_packages, installed_packages, PROFILES, profile_handlers, profile_configurations, profiles_for_arch, profile_hypervisor


def run(*command):
    return subprocess.check_output(command, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="qemu")
    parser.add_argument("--install", action="store_true", required=True)
    parser.add_argument("--fresh", action="store_true", help="remove POC packages before installing")
    args = parser.parse_args()
    if args.profile not in profiles_for_arch(platform.machine()):
        parser.error('selected profile is not available on this architecture')
    selected = installed_packages(args.profile)
    entry = 'kata-containers' if args.profile == 'qemu' else 'kata-containers-' + args.profile
    release = platform.freedesktop_os_release()
    if os.geteuid() or release.get("ID") != "almalinux" or release.get("VERSION_ID") != "10.2":
        parser.error("run as root on a disposable AlmaLinux 10.2 host/container with EPEL enabled")
    if not shutil.which("containerd"):
        parser.error("install system containerd before running the standard-path validator; Kata no longer supplies the cluster CRI")
    packages = args.packages.resolve()
    rpms = sorted(packages.glob("*.rpm"))
    names = {run("rpm", "-qp", "--qf", "%{NAME}", str(p)) for p in rpms}
    versions = {run("rpm", "-qp", "--qf", "%{VERSION}-%{RELEASE}", str(p)) for p in rpms}
    if len(rpms) != len(names) or not set(selected).issubset(names) or names not in (set(PACKAGES), set(build_packages(['qemu', 'nvidia-cpu'])), set(build_packages(['qemu', 'nvidia-cpu', 'nvidia-gpu'])), set(build_packages(architecture=platform.machine()))) or len(versions) != 1:
        parser.error('expected a complete native Kata package set at one version')
    version = versions.pop()
    if args.fresh:
        installed = [name for name in build_packages() if subprocess.run(
            ["rpm", "-q", name], capture_output=True).returncode == 0]
        if installed:
            subprocess.run(["dnf", "-y", "--setopt=clean_requirements_on_remove=False", "remove", *installed], check=True)
    subprocess.run(["createrepo_c", str(packages)], check=True)
    with tempfile.TemporaryDirectory(prefix="kata-dnf-") as directory:
        repo = Path(directory) / "kata-poc.repo"
        repo.write_text(f"[kata-poc]\nname=Local Kata POC\nbaseurl=file://{packages}\nenabled=1\ngpgcheck=0\n")
        subprocess.run(["dnf", "-y", "--setopt=install_weak_deps=False", "--setopt=reposdir=/etc/yum.repos.d," + directory,
                        "install", f"{entry}-{version}"], check=True)
    installed = {}
    requirements = {}
    for name in selected:
        installed[name] = run("rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", name)
        assert installed[name] == version
        assert not run("rpm", "-V", name).strip(), f"installed files changed: {name}"
        requirements[name] = run("rpm", "-qR", name).splitlines()
    profile = Path('/opt/kata/share/defaults/kata-containers/configuration-' + profile_configurations(args.profile)[0] + '.toml')
    hypervisor = tomllib.loads(profile.read_text())['hypervisor'][profile_hypervisor(args.profile)]
    binaries = {'shim': '/opt/kata/bin/containerd-shim-kata-v2'}
    if hypervisor.get('path'):
        binaries[profile_hypervisor(args.profile)] = hypervisor['path']
    if hypervisor.get('virtio_fs_daemon') and hypervisor.get('shared_fs') == 'virtio-fs':
        binaries['virtiofsd'] = hypervisor['virtio_fs_daemon']
    if args.profile == 'nvidia-gpu-tee':
        binaries['qemu-tdx'] = '/opt/kata/bin/qemu-system-x86_64-tdx-experimental'
    outputs = {name: run(path, '--version').strip() for name, path in binaries.items()}
    expected = {"path": "kata-containers-qemu", "kernel": "kata-containers-kernel-generic",
                "image": "kata-containers-rootfs-generic", "virtio_fs_daemon": "kata-containers-virtiofsd",
                "firmware": "kata-containers-qemu", "firmware_volume": "kata-containers-qemu"}
    if args.profile == 'dragonball':
        expected = {'kernel': 'kata-containers-kernel-dragonball',
                    'image': 'kata-containers-rootfs-generic'}
    if args.profile.startswith('nvidia-'):
        expected.update(path='kata-containers-nvidia-cpu', kernel='kata-containers-kernel-nvidia',
                        image='kata-containers-rootfs-nvidia')
        expected.pop('virtio_fs_daemon')
    if args.profile == 'nvidia-gpu':
        expected.update(firmware='kata-containers-nvidia-gpu', firmware_volume='kata-containers-nvidia-gpu')
    if args.profile == 'nvidia-gpu-tee':
        expected.update(path='kata-containers-nvidia-gpu-tee', firmware='kata-containers-nvidia-gpu-tee', firmware_volume='kata-containers-nvidia-gpu-tee')
    owners = {}
    for key, owner in expected.items():
        path = hypervisor.get(key)
        if path:
            assert Path(path).is_file(), f"missing asset: {path}"
            actual = run("rpm", "-qf", "--qf", "%{NAME}", path)
            assert actual == owner, f"wrong owner: {path}: {actual}"
            owners[key] = {"path": path, "package": actual}
    setup = ["kata-containers-setup", "--k8s-distro", "kubeadm"]
    before = Path("/etc/containerd/config.toml").read_bytes()
    preview = run(*setup, "--dry-run")
    assert Path("/etc/containerd/config.toml").read_bytes() == before, "dry-run changed config"
    applied = run(*setup, "--apply")
    assert "already configured" in run(*setup, "--apply"), "setup is not idempotent"
    packages.joinpath("setup-dry-run.diff").write_text(preview)
    packages.joinpath("setup-apply.txt").write_text(applied)
    config = tomllib.loads(run("containerd", "config", "dump"))
    runtimes = config["plugins"]["io.containerd.cri.v1.runtime"]["containerd"]["runtimes"]
    present = [p for p in PROFILES if Path("/usr/share/kata-containers/installed-profiles/" + p + ".json").is_file()]
    assert {name for name in runtimes if name.startswith("kata")} == {handler for p in present for handler in profile_handlers(p)}
    assert runtimes[PROFILES[args.profile]["handler"]]["options"]["ConfigPath"] == str(profile)
    assert "cri" not in config.get("disabled_plugins", []), "the installed CRI plugin is disabled"
    if 'nvidia-gpu-tee' in present:
        validate_tee(config, lambda path: run('rpm', '-qf', '--qf', '%{NAME}', path))
    subprocess.run(["python3", str(Path(__file__).parent / "verify_image.py")], check=True)
    subprocess.run(["dnf", "check"], check=True)
    result = {"target": release["PRETTY_NAME"], "installation": "clean" if args.fresh else "upgrade-or-install", "packages": installed, "requirements": requirements,
              "dnf_requested": f"{entry}-{version}", "binaries": outputs, "asset_owners": owners,
              "installed_files_verified": True, "guest_verity_trees_verified": True,
              "containerd_profiles": [handler for p in present for handler in profile_handlers(p)],
              "limits": "AlmaLinux container on Ubuntu kernel; no AlmaLinux host/module, SELinux or KVM/CRI workload validation"}
    (packages / "validation-rpm.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
