#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assemble generic QEMU and NVIDIA CPU/GPU installation packages from verified source outputs."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from elf_dependencies import dependencies
from packages import PROFILES

HERE = Path(__file__).resolve().parent
PACKAGING = HERE.parent / "packaging/ubuntu-26.04"
PAYLOADS = {"qemu": ("qemu", "ovmf"), "nvidia-cpu": ("qemu-no-shared-fs",), "nvidia-gpu": ("ovmf",)}
SHARED = {
    "rootfs-generic": ("rootfs-image", "Kata Containers generic guest root filesystem"),
    "kernel-generic": ("kernel", "Kata Containers generic guest kernel"),
    "virtiofsd": ("virtiofsd", "Kata Containers shared virtio-fs daemon"),
    "rootfs-nvidia": ("rootfs-image-nvidia", "Kata Containers NVIDIA base guest root filesystem"),
    "kernel-nvidia": ("kernel-nvidia-gpu", "Kata Containers NVIDIA guest kernel"),
    "rootfs-nvidia-gpu": ("rootfs-image-nvidia-gpu-extension", "Kata Containers NVIDIA GPU driver guest extension"),
}


def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def install(source, root, target, mode=0o644):
    dest = root / target.lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    dest.chmod(mode)
    return dest


def stage_runtime_config(source, root, profile):
    base = Path('opt/kata/share/defaults/kata-containers')
    filename = f'configuration-{profile}.toml'
    relative = Path('runtimes') / profile / filename
    config = install(source, root, str(base / relative))
    config.write_text(
        f'# Managed by kata-containers-{profile}.\n'
        '# Customize through config.d/*.toml alongside this file.\n'
        '# Drop-ins are processed alphabetically; later files override earlier settings.\n\n'
        + config.read_text())
    (config.parent / 'config.d').mkdir()
    entry = root / base / filename
    entry.symlink_to(relative)
    return entry


def stage_gpu_firmware(root, config):
    # GPU reuses the CPU VMM package. Keep its UEFI files private so a generic
    # QEMU installation can coexist without duplicate file ownership.
    text = config.read_text()
    for name in ('ovmf', 'aavmf'):
        source = root / 'opt/kata/share' / name
        destination = root / 'opt/kata/share/kata-nvidia-gpu/firmware' / name
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), destination)
            text = text.replace('/opt/kata/share/' + name + '/',
                                '/opt/kata/share/kata-nvidia-gpu/firmware/' + name + '/')
    # Upstream 4.2 leaves this undefined token in the exported GPU config.
    # It ships a single firmware image, with no separate NV variable store.
    text = text.replace('"@FIRMWAREVOLUMEPATH_NV@"', '""')
    config.write_text(text)


def validate_gpu_extension(hypervisor, stage):
    images = hypervisor.get('guest_extension_images', [])
    if len(images) != 1 or images[0].get('name') != 'gpu':
        raise ValueError('NVIDIA GPU requires exactly one GPU guest extension')
    extension = images[0]
    expected_path = '/opt/kata/share/kata-containers/kata-containers-nvidia-gpu-extension.img'
    measurement = stage / 'opt/kata/share/kata-containers/root_hash_nvidia-gpu-extension.txt'
    if (extension.get('path') != expected_path or not (stage / expected_path.lstrip('/')).is_file()
            or extension.get('verity_params', '').strip() != measurement.read_text().strip()):
        raise ValueError('GPU extension image/config verity measurement mismatch')


def extract(archive, destination):
    with subprocess.Popen(["zstd", "-dc", str(archive.resolve())], stdout=subprocess.PIPE) as proc:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unsafe archive member: {member.name}")
                if path.parts and path.parts[0] != "opt":
                    raise ValueError(f"unexpected component payload: {member.name}")
                tar.extract(member, destination, filter="data")
        if proc.wait():
            raise ValueError(f"cannot decompress {archive}")


def control(root, name, version, arch, depends, description):
    folder = root / "DEBIAN"
    folder.mkdir(exist_ok=True)
    (folder / "control").write_text(
        f"Package: {name}\nVersion: {version}\nArchitecture: {arch}\n"
        "Section: admin\nPriority: optional\n"
        "Maintainer: Kata Containers community <kata-dev@lists.katacontainers.io>\n" +
        (f"Depends: {depends}\n" if depends else "") + f"Description: {description}\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("deb", "rpm"), default="deb")
    parser.add_argument("--stage-only", action="store_true", help="export verified package payloads and metadata for native OBS source recipes")
    parser.add_argument("--inputs", dest="build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory, never overwritten")
    parser.add_argument("--version", default=json.loads((HERE.parent / "config.json").read_text())["package_version"])
    args = parser.parse_args()
    if args.format == "rpm":
        release = platform.freedesktop_os_release()
        if release.get("ID") != "almalinux" or release.get("VERSION_ID") != "10.2":
            parser.error("assemble RPMs natively on AlmaLinux 10.2")
    if platform.system() != "Linux":
        parser.error("assemble on the native target Linux builder")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", args.version):
        parser.error("invalid Debian version")
    arch = ({"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine()) if args.format == "rpm" else
            subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip())
    if arch not in ("amd64", "arm64"):
        parser.error("unsupported architecture")
    profiles = tuple(PROFILES)
    status = json.loads((args.build / "source-build.json").read_text())
    if status["inputs"]["architecture"] != {"amd64": "x86_64", "arm64": "aarch64"}[arch]:
        parser.error("build architecture does not match the packaging host")
    components = {"shim-v2-rust"} | {component for component, _ in SHARED.values()}
    for profile in profiles:
        components.update(PAYLOADS[profile])
    for component in components:
        record = status["components"].get(component, {})
        if record.get("state") not in ("complete", "imported"):
            parser.error(f"source component not completed: {component}")
        for name, sha in record["artifacts"].items():
            if digest(args.build / name) != sha:
                parser.error(f"source output checksum changed: {name}")
    source_patch = (args.build / "source-patch.diff").read_bytes()
    expected_patch = status["inputs"].get("source_patch_sha256")
    if expected_patch and hashlib.sha256(source_patch).hexdigest() != expected_patch:
        parser.error("source patch changed since component build")
    status["packaging"] = {"version": args.version, "assembler_sha256": digest(Path(__file__)),
                           "source_patch_sha256": hashlib.sha256(source_patch).hexdigest()}
    request_file = args.build / "build-request.json"
    if request_file.exists():
        from build_request import validate_request
        request = validate_request(json.loads(request_file.read_text()))
        if request["source_revision"] != status["inputs"]["source_revision"] or request["package_version"] != args.version:
            parser.error("component source/version does not match CI build request")
        status["packaging"]["build_request"] = request
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(dir=args.output, prefix="staging-") as temp:
        work = Path(temp)
        shim = work / "shim"
        shim.mkdir()
        extract(args.build / "kata-static-shim-v2-rust.tar.zst", shim)
        stages = {}
        core = work / "kata-containers-core"
        core.mkdir()
        install(shim / "opt/kata/runtime-rs/bin/containerd-shim-kata-v2", core,
                "/opt/kata/bin/containerd-shim-kata-v2", 0o755)
        install(HERE / "setup.py", core, "/usr/bin/kata-containers-setup", 0o755)
        for source, dest in (("load_modules.py", "/usr/libexec/kata-containers/load-modules"),):
            install(PACKAGING / source, core, dest, 0o755)
        for source, dest in (("host-requirements.json", "/usr/share/kata-containers/host-requirements.json"),
                             ("modules.conf", "/usr/lib/modules-load.d/kata-containers.conf"),
                             ("modules.service", "/usr/lib/systemd/system/kata-containers-modules.service")):
            install(PACKAGING / source, core, dest)
        unit_link = core / "usr/lib/systemd/system/multi-user.target.wants/kata-containers-modules.service"
        unit_link.parent.mkdir(parents=True)
        unit_link.symlink_to("../kata-containers-modules.service")
        libs = dependencies(core) if args.format == "deb" else ""
        depends = "kmod, python3 (>= 3.11)"
        control(core, core.name, args.version, arch, depends + (", " + libs if libs else ""),
                "Kata Containers shared Rust runtime")
        install(PACKAGING / "debian/postinst", core, "/DEBIAN/postinst", 0o755)
        stages[core.name] = core
        for suffix, (component, description) in SHARED.items():
            stage = work / ("kata-containers-" + suffix)
            stage.mkdir()
            extract(args.build / f"kata-static-{component}.tar.zst", stage)
            control(stage, stage.name, args.version, arch, (dependencies(stage) if args.format == "deb" else ""), description)
            stages[stage.name] = stage
        configs = shim / "opt/kata/share/defaults/kata-containers/runtime-rs"
        for profile in profiles:
            name = "kata-containers-" + profile
            stage = work / name
            stage.mkdir()
            for component in PAYLOADS[profile]:
                extract(args.build / f"kata-static-{component}.tar.zst", stage)
            config_name = "qemu" if profile == "qemu" else "qemu-" + profile
            config = stage_runtime_config(configs / f"configuration-{config_name}-runtime-rs.toml", stage, profile)
            if profile == "nvidia-gpu":
                stage_gpu_firmware(stage, config)
            subprocess.run(["python3", str(HERE / "stage_profile.py"), profile, "--root", str(stage),
                            "--architecture", arch, "--version", args.version, "--format", args.format], cwd=PACKAGING, check=True)
            stages[name] = stage
        # Check all runtime asset paths and image/config measurement pairing.
        for profile in profiles:
            stage = stages["kata-containers-" + profile]
            config = stage / f"opt/kata/share/defaults/kata-containers/configuration-{profile}.toml"
            text = config.read_text()
            if re.search(r"@[A-Z][A-Z0-9_]*@", text):
                raise ValueError(f"unresolved configuration substitutions: {profile}")
            hypervisor = tomllib.loads(text)["hypervisor"]["qemu"]
            measurement = 'root_hash_nvidia.txt' if profile.startswith('nvidia-') else 'root_hash_base.txt'
            image_stage = stages['kata-containers-rootfs-' + ('nvidia' if profile.startswith('nvidia-') else 'generic')]
            expected_measurement = (image_stage / 'opt/kata/share/kata-containers' / measurement).read_text().strip()
            if profile.startswith('nvidia-') and hypervisor.get('kernel_verity_params', '').strip() != expected_measurement:
                raise ValueError(f'guest image/config verity measurement mismatch: {profile}')
            if profile == 'nvidia-gpu':
                validate_gpu_extension(hypervisor, stages['kata-containers-rootfs-nvidia-gpu'])
            asset_roots = list(stages.values())
            for key in ("path", "kernel", "image", "firmware", "firmware_volume", "virtio_fs_daemon"):
                if key == "virtio_fs_daemon" and hypervisor.get("shared_fs") in (None, "none", "inline-virtio-fs"):
                    continue
                value = hypervisor.get(key)
                if value and not any((root / value.lstrip("/")).is_file() for root in asset_roots):
                    raise ValueError(f"missing {profile} {key}: {value}")
        meta = work / "kata-containers"
        meta.mkdir()
        control(meta, meta.name, args.version, "all", f"kata-containers-qemu (= {args.version})",
                "Kata Containers default QEMU installation")
        stages[meta.name] = meta
        ownership = {}
        for name, stage in stages.items():
            for path in stage.rglob("*"):
                relative = path.relative_to(stage).as_posix()
                if relative.startswith("DEBIAN/") or (path.is_dir() and not path.is_symlink()):
                    continue
                if relative in ownership:
                    raise ValueError(f"payload collision: {relative} in {ownership[relative]} and {name}")
                ownership[relative] = name
            install(HERE.parent / "LICENSE", stage, f"/usr/share/doc/{name}/LICENSE")
            install(args.build / "LICENSE.upstream", stage, f"/usr/share/doc/{name}/LICENSE.upstream")
            (stage / f"usr/share/doc/{name}/copyright").write_text(
                "Kata Containers packaging POC.\n"
                "The accompanying LICENSE covers this MIT-licensed packaging code.\n"
                "LICENSE.upstream covers Kata source; bundled components retain their own licenses.\n"
                "Bundled QEMU, Linux, firmware and guest distro components\n"
                "retain their original licenses; MIT covers the packaging code only.\n"
                "Source versions are recorded in source-build.json and versions.yaml.\n"
                "The complete bundled-component notice inventory is still pending.\n"
            )
            install(args.build / "versions.yaml", stage, f"/usr/share/doc/{name}/versions.yaml")
            manifest = stage / f"usr/share/doc/{name}/source-build.json"
            manifest.write_text(json.dumps(status, indent=2) + "\n")
            (manifest.parent / "source-patch.diff").write_bytes(source_patch)
            md5sums = []
            for file in sorted(stage.rglob("*")):
                if file.is_file() and not file.is_symlink() and "DEBIAN" not in file.relative_to(stage).parts:
                    md5sums.append(f"{digest(file, 'md5')}  {file.relative_to(stage)}")
            size = sum(p.stat().st_size for p in stage.rglob("*")
                       if p.is_file() and not p.is_symlink() and "DEBIAN" not in p.relative_to(stage).parts)
            with (stage / "DEBIAN/control").open("a") as stream:
                stream.write(f"Installed-Size: {(size + 1023) // 1024}\n")
            (stage / "DEBIAN/md5sums").write_text("\n".join(md5sums) + "\n")
            if args.stage_only:
                shutil.copytree(stage, args.output / name, symlinks=True)
                continue
            package_arch = "all" if name == "kata-containers" else arch
            if args.format == "rpm":
                from rpm_backend import build
                build(stage, args.output, args.version, {"amd64": "x86_64", "arm64": "aarch64"}[arch])
                continue
            subprocess.run(["dpkg-deb", "--root-owner-group", "-Zzstd", "-z6", "--build", str(stage),
                            str(args.output / f"{name}_{args.version}_{package_arch}.deb")], check=True)
    if args.stage_only:
        print(f"Staged {len(stages)} verified package payloads in {args.output}")
        return
    hashes = [f"{digest(p)}  {p.name}" for p in sorted(args.output.glob("*." + args.format))]
    (args.output / "SHA256SUMS").write_text("\n".join(hashes) + "\n")
    print(f"Built {len(hashes)} source-derived packages in {args.output}; nothing published")


if __name__ == "__main__":
    main()
