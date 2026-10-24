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
from packages import build_packages, installed_packages, PROFILES, profile_handlers, profile_configurations, profiles_for_arch, profile_hypervisor

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



def validate_tee(config, owner):
    """Exercise installed binaries and both handlers without creating a guest."""
    for handler, configuration in zip(profile_handlers('nvidia-gpu-tee'), profile_configurations('nvidia-gpu-tee')):
        runtime = config['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes'][handler]
        path = '/opt/kata/share/defaults/kata-containers/configuration-' + configuration + '.toml'
        assert runtime['options']['ConfigPath'] == path
        assert runtime['snapshotter'] == 'nydus-for-kata-tee'
        hypervisor = tomllib.loads(Path(path).read_text())['hypervisor']['qemu']
        for key in ('path', 'firmware', 'firmware_volume'):
            if hypervisor.get(key):
                assert owner(hypervisor[key]) == 'kata-containers-nvidia-gpu-tee'
        run(hypervisor['path'], '--version')
        assert {entry['name'] for entry in hypervisor['guest_extension_images']} == {'gpu', 'coco'}
        for entry in hypervisor['guest_extension_images']:
            suffix = 'nvidia-gpu-extension' if entry['name'] == 'gpu' else 'coco-extension'
            assert owner(entry['path']) == ('kata-containers-rootfs-nvidia-gpu' if entry['name'] == 'gpu' else 'kata-containers-rootfs-coco')
            measurement = Path('/opt/kata/share/kata-containers/root_hash_' + suffix + '.txt').read_text().strip()
            assert entry['verity_params'].strip() == measurement
    proxy = config['proxy_plugins']['nydus-for-kata-tee']
    assert proxy['type'] == 'snapshot'
    assert proxy['address'] == '/run/nydus-for-kata-tee/containerd-nydus-grpc.sock'
    assert proxy['exports']['root'] == '/var/lib/nydus-for-kata-tee'
    assert not config['plugins']['io.containerd.cri.v1.images']['disable_snapshot_annotations']
    nydus = tomllib.loads(Path('/etc/kata-containers/nydus-snapshotter.toml').read_text())
    assert nydus['daemon']['fs_driver'] == 'proxy'
    assert nydus['snapshot']['enable_kata_volume']
    for binary in ('containerd-nydus-grpc', 'nydus-overlayfs'):
        path = '/opt/kata/nydus-for-kata-tee/' + binary
        assert owner(path) == 'kata-containers-nydus-snapshotter'
        run(path, '--version')
    unit = Path('/usr/lib/systemd/system/nydus-for-kata-tee.service').read_text()
    assert '--config /etc/kata-containers/nydus-snapshotter.toml' in unit
    assert 'StateDirectory=nydus-for-kata-tee' in unit

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="qemu")
    parser.add_argument("--install", action="store_true", required=True,
                        help="explicitly install on this disposable builder")
    parser.add_argument("--fresh", action="store_true", help="remove existing POC packages before installing")
    args = parser.parse_args()
    if args.profile not in profiles_for_arch(run('dpkg', '--print-architecture').strip()):
        parser.error('selected profile is not available on this architecture')
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
    if len(debs) != len(names) or not set(selected).issubset(names) or names not in (set(PACKAGES), set(build_packages(['qemu', 'nvidia-cpu'])), set(build_packages(['qemu', 'nvidia-cpu', 'nvidia-gpu'])), set(build_packages(architecture=run('dpkg', '--print-architecture').strip()))):
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
    profile_path = Path('/opt/kata/share/defaults/kata-containers/configuration-' + profile_configurations(args.profile)[0] + '.toml')
    hypervisor = tomllib.loads(profile_path.read_text())['hypervisor'][profile_hypervisor(args.profile)]
    binaries = {'shim': '/opt/kata/bin/containerd-shim-kata-v2'}
    if hypervisor.get('path'):
        binaries[profile_hypervisor(args.profile)] = hypervisor['path']
    if hypervisor.get('virtio_fs_daemon') and hypervisor.get('shared_fs') == 'virtio-fs':
        binaries['virtiofsd'] = hypervisor['virtio_fs_daemon']
    if args.profile == 'nvidia-gpu-tee':
        binaries['qemu-tdx'] = '/opt/kata/bin/qemu-system-x86_64-tdx-experimental'
    outputs = {name: run(path, '--version').strip() for name, path in binaries.items()}
    expected_owners = {"path": "kata-containers-qemu", "kernel": "kata-containers-kernel-generic",
                       "image": "kata-containers-rootfs-generic", "virtio_fs_daemon": "kata-containers-virtiofsd",
                       "firmware": "kata-containers-qemu", "firmware_volume": "kata-containers-qemu"}
    if args.profile == 'dragonball':
        expected_owners = {'kernel': 'kata-containers-kernel-dragonball',
                    'image': 'kata-containers-rootfs-generic'}
    if args.profile == 'cloud-hypervisor':
        expected_owners.update(path='kata-containers-cloud-hypervisor')
    if args.profile.startswith('nvidia-'):
        expected_owners.update(path='kata-containers-nvidia-cpu', kernel='kata-containers-kernel-nvidia',
                               image='kata-containers-rootfs-nvidia')
        expected_owners.pop('virtio_fs_daemon')
    if args.profile == 'nvidia-gpu':
        expected_owners.update(firmware='kata-containers-nvidia-gpu', firmware_volume='kata-containers-nvidia-gpu')
    if args.profile == 'nvidia-gpu-tee':
        expected_owners.update(path='kata-containers-nvidia-gpu-tee', firmware='kata-containers-nvidia-gpu-tee', firmware_volume='kata-containers-nvidia-gpu-tee')
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
    assert {name for name in runtimes if name.startswith("kata")} == {handler for p in present for handler in profile_handlers(p)}
    assert runtimes[PROFILES[args.profile]["handler"]]["options"]["ConfigPath"] == str(profile_path)
    assert config["plugins"]["io.containerd.cri.v1.runtime"]["containerd"]["default_runtime_name"] == "runc"
    if 'nvidia-gpu-tee' in present:
        validate_tee(config, lambda path: run('dpkg-query', '-S', path).strip().split(': ', 1)[0])
    subprocess.run(["python3", str(HERE / "verify_image.py")], check=True)
    subprocess.run(["apt-get", "check"], check=True)
    result = {"packages": installed_versions, "binaries": outputs, "asset_owners": owners,
              "installation": "clean" if args.fresh else "upgrade-or-install",
              "apt_requested": f"{entry}={version}", "installed_files_verified": True,
              "guest_verity_trees_verified": True, "containerd_profiles": [handler for p in present for handler in profile_handlers(p)],
              "limits": "No KVM or full CRI workload validation"}
    report = "validation-clean.json" if args.fresh else "validation-upgrade.json"
    (args.packages / report).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
