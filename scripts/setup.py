#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Explicitly enable installed Kata profiles for the selected Kubernetes distro."""
import argparse
from datetime import datetime, timezone
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib

CONFIG = Path("/etc/containerd/config.toml")
IMPORT = "/etc/containerd/conf.d/*.toml"


def command(*args):
    return subprocess.check_output(args, text=True)


DISTROS = ("kubeadm", "k0s", "k3s", "microk8s", "rke2")
BEGIN, END = "# BEGIN kata-containers-setup", "# END kata-containers-setup"


def managed_block(old, payload):
    """Replace only this tool's block, retaining other administrator configuration."""
    if old.count(BEGIN) != old.count(END) or old.count(BEGIN) > 1:
        raise ValueError("malformed Kata managed block")
    if BEGIN in old:
        before, rest = old.split(BEGIN, 1)
        _, after = rest.split(END, 1)
        return before + BEGIN + "\n" + payload.rstrip() + "\n" + END + after
    return old.rstrip() + "\n\n" + BEGIN + "\n" + payload.rstrip() + "\n" + END + "\n"


def containerd_binary(distro):
    if distro == 'kubeadm':
        binary = shutil.which('containerd')
        if binary:
            return binary
    elif distro == 'microk8s':
        return '/snap/microk8s/current/bin/containerd'
    else:
        base = Path('/var/lib/k0s' if distro == 'k0s' else '/var/lib/rancher/' + distro)
        candidates = [base / 'bin/containerd', base / 'data/current/bin/containerd']
        candidates += sorted(base.glob('data/*/bin/containerd'), reverse=True)
        for path in candidates:
            if path.is_file():
                return str(path)
    raise ValueError('cannot locate ' + distro + "'s containerd binary")


def validate_profile_requirements(distro):
    profiles = [json.loads(path.read_text()) for path in
                Path('/usr/share/kata-containers/installed-profiles').glob('*.json')]
    minimum = max((profile.get('minimum_containerd', '') for profile in profiles), default='')
    if not minimum:
        return
    output = command(containerd_binary(distro), '--version')
    version = re.search(r'\bv?([0-9]+)\.([0-9]+)\.([0-9]+)', output)
    if not version or tuple(map(int, version.groups())) < tuple(map(int, minimum.split('.'))):
        raise ValueError(f'installed NVIDIA profile requires {distro} containerd >= {minimum}')
    binary = shutil.which('mkfs.erofs')
    if not binary:
        raise ValueError('install erofs-utils >= 1.8.2 for NVIDIA profiles')
    result = subprocess.run([binary, '--help'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not all(option in result.stdout for option in ('--mkfs-time', '--sort=')):
        raise ValueError('mkfs.erofs lacks required --mkfs-time/--sort options; install erofs-utils >= 1.8.2')


def profile_tables(version):
    chunks = []
    for marker in sorted(Path("/usr/share/kata-containers/installed-profiles").glob("*.json")):
        path = Path("/etc/containerd/conf.d") / ("50-kata-" + marker.stem + ".toml")
        text = path.read_text()
        # The vendor drop-ins use schema 3. Older bundled runtimes use schema 2.
        text = re.sub(r"(?m)^version\s*=.*\n", "", text)
        if version == 2 and json.loads(marker.read_text()).get('minimum_containerd'):
            raise ValueError('NVIDIA profile requires containerd >= 2.3.0 and configuration schema 3')
        if version == 2:
            text = text.replace("io.containerd.cri.v1.runtime", "io.containerd.grpc.v1.cri")
        chunks.append(text)
    return "\n".join(chunks)


def atomic_update(path, old, proposed):
    if path.is_symlink():
        raise ValueError(f"refusing symlink configuration: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = path.stat() if path.exists() else None
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(proposed)
            stream.flush()
            os.fsync(stream.fileno())
            if (path.read_text() if path.exists() else "") != old:
                raise ValueError("configuration changed during setup; retry")
            if metadata:
                backup = path.with_name(path.name + ".kata-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
                with backup.open("x") as dest:
                    os.chmod(backup, 0o600)
                    dest.write(old)
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            else:
                os.fchmod(stream.fileno(), 0o644)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def bundled_setup(args):
    validate_profile_requirements(args.k8s_distro)
    distro = args.k8s_distro
    if distro in ("k3s", "rke2"):
        directory = Path(f"/var/lib/rancher/{distro}/agent/etc/containerd")
        rendered = (directory / "config.toml").read_text()
        version = tomllib.loads(rendered).get("version", 2)
        path = directory / ("config-v3.toml.tmpl" if version >= 3 else "config.toml.tmpl")
        old = path.read_text() if path.exists() else ""
        base = old or '{{ template "base" . }}\n'
        proposed = managed_block(base, profile_tables(3 if version >= 3 else 2))
        service = distro if distro == "k3s" else "rke2-server (or rke2-agent)"
    elif distro == "microk8s":
        path = Path("/var/snap/microk8s/current/args/containerd-template.toml")
        old = path.read_text()
        match = re.search(r"(?m)^version\s*=\s*([0-9]+)", old)
        version = int(match[1]) if match else 2
        proposed = managed_block(old, profile_tables(3 if version >= 3 else 2))
        service = "snap.microk8s.daemon-containerd"
    else:
        managed = Path("/etc/k0s/containerd.toml").read_text()
        if not managed.startswith("# k0s_managed=true"):
            raise ValueError("k0s containerd configuration must be k0s managed")
        version = tomllib.loads(managed).get("version", 2)
        path = Path("/etc/k0s/containerd.d/50-kata-containers.toml")
        old = path.read_text() if path.exists() else ""
        proposed = f"version = {version}\n" + profile_tables(3 if version >= 3 else 2)
        service = "k0scontroller (or k0sworker)"
    if path.is_symlink():
        raise ValueError(f"refusing symlink configuration: {path}")
    if args.status:
        print(json.dumps({"k8s_distro": distro, "config": str(path), "configured": old == proposed}, indent=2))
    elif old != proposed:
        print("".join(difflib.unified_diff(old.splitlines(True), proposed.splitlines(True),
                                         fromfile=str(path), tofile=str(path) + " (proposed)")), end="")
        if args.apply:
            atomic_update(path, old, proposed)
            print(f"Kata profiles updated. Restart {service} when ready.")
    else:
        print("Installed Kata profiles are already configured.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k8s-distro", required=True, choices=DISTROS)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.apply and os.geteuid():
        parser.error("--apply requires root")
    if args.k8s_distro != "kubeadm":
        bundled_setup(args)
        return
    if CONFIG.is_symlink():
        parser.error("containerd config is a symlink; configure its imports explicitly")
    validate_profile_requirements(args.k8s_distro)
    old = CONFIG.read_text() if CONFIG.exists() else ""
    current = tomllib.loads(old)
    profiles = sorted(p.stem for p in Path("/usr/share/kata-containers/installed-profiles").glob("*.json"))
    configured = current.get("version") in (3, 4) and IMPORT in current.get("imports", [])
    if args.status:
        print(json.dumps({"k8s_distro": args.k8s_distro, "config": str(CONFIG),
                          "version": current.get("version"), "imports_configured": configured,
                          "installed_profiles": profiles}, indent=2))
        return
    if args.apply and os.geteuid():
        parser.error("--apply requires root")
    if configured:
        print("Containerd configuration and Kata drop-in imports are already configured.")
        return
    migrated = command("containerd", "--config", str(CONFIG), "config", "migrate") if old else command("containerd", "config", "default")
    data = tomllib.loads(migrated)
    if data.get("version") not in (3, 4):
        parser.error("containerd must support configuration version 3 or 4")
    imports = data.get("imports", [])
    if IMPORT not in imports:
        imports.append(IMPORT)
    if "imports" in data:
        migrated, count = re.subn(r"(?m)^imports\s*=.*$", "imports = " + json.dumps(imports), migrated, count=1)
        if count != 1:
            parser.error("unexpected imports format in containerd migration output")
    else:
        migrated = "imports = " + json.dumps(imports) + "\n" + migrated
    expected = dict(data, imports=imports)
    if tomllib.loads(migrated) != expected:
        parser.error("cannot safely edit imports in containerd migration output")
    print("".join(difflib.unified_diff(old.splitlines(True), migrated.splitlines(True),
                                     fromfile=str(CONFIG), tofile=str(CONFIG) + " (proposed)")), end="")
    if args.dry_run:
        return
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    metadata = CONFIG.stat() if CONFIG.exists() else None
    with tempfile.NamedTemporaryFile(mode="w", prefix=".kata-config-", dir=CONFIG.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(migrated)
            stream.flush()
            os.fsync(stream.fileno())
            # Validate the effective handler table before replacing the config.
            effective = tomllib.loads(command("containerd", "--config", str(temp), "config", "dump"))
            runtimes = effective["plugins"]["io.containerd.cri.v1.runtime"]["containerd"]["runtimes"]
            for profile in profiles:
                if "kata-" + profile not in runtimes:
                    parser.error(f"containerd did not load installed profile {profile}")
            if (CONFIG.read_text() if CONFIG.exists() else "") != old:
                parser.error("containerd config changed during setup; retry")
            if metadata:
                backup = CONFIG.with_name(CONFIG.name + ".kata-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
                with backup.open("x") as dest:
                    os.chmod(backup, 0o600)
                    dest.write(old)
                print(f"Backup: {backup}")
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            else:
                os.fchmod(stream.fileno(), 0o644)
            os.replace(temp, CONFIG)
        finally:
            temp.unlink(missing_ok=True)
    print("Containerd configuration updated. Restart containerd when ready.")


if __name__ == "__main__":
    main()
