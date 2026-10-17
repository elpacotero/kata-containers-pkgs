#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Download one exact OBS build and smoke-test its packages on a disposable KVM host."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time
import traceback
import tomllib
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

from ci import OBS, ROOT, validate_request
from validate import PACKAGES
from packages import build_packages, installed_packages, PROFILES

IMAGE = "docker.io/library/busybox:1.37.0"


def run(*args, **kwargs):
    return subprocess.check_output(list(map(str, args)), text=True, **kwargs).strip()


def package_metadata(path, package_format):
    if package_format == "deb":
        values = [run("dpkg-deb", "-f", path, key) for key in ("Package", "Version", "Architecture")]
    else:
        values = run("rpm", "-qp", "--qf", "%{NAME}\n%{VERSION}-%{RELEASE}\n%{ARCH}", path).splitlines()
    return dict(zip(("name", "version", "arch"), values))


def check_packages(folder, request, target):
    paths = sorted(folder.glob("*." + target["format"]))
    metadata = [package_metadata(path, target["format"]) for path in paths]
    expected_version = request["package_version"] + (".el10" if target["format"] == "rpm" else "")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}[target["arch"]] if target["format"] == "deb" else target["arch"]
    expected = build_packages(request.get('profiles', ['qemu']), target['arch'])
    if len(paths) != len(expected) or {item['name'] for item in metadata} != set(expected):
        raise ValueError('expected exactly the Kata packages declared by the build request')
    for item in metadata:
        expected_arch = ("all" if target["format"] == "deb" else "noarch") if item['name'] == "kata-containers" else arch
        if item["version"] != expected_version or item["arch"] != expected_arch:
            raise ValueError(f"package belongs to another build or architecture: {item}")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def download(request, receipt, target, output):
    if receipt["request_id"] != request["request_id"]:
        raise ValueError("build request and receipt differ")
    client = OBS(receipt["project"], receipt["package"])
    path = "/build/" + "/".join(urllib.parse.quote(part, safe="") for part in
        (receipt["project"], target["repository"], target["arch"], receipt["package"]))
    def current():
        source, md5 = client.source()
        if not source or source["request_id"] != request["request_id"] or md5 != receipt["source_md5"]:
            raise ValueError("OBS source changed; refusing packages from another build")
    current()
    output.mkdir(parents=True, exist_ok=False)
    listing = ET.fromstring(client.api(path))
    for binary in listing.findall("binary"):
        name = binary.attrib["filename"]
        if name != Path(name).name or name in (".", ".."):
            raise ValueError("unsafe OBS binary filename")
        if name.startswith("kata-containers") and name.endswith("." + target["format"]) and not name.endswith(".src.rpm"):
            (output / name).write_bytes(client.api(path + "/" + urllib.parse.quote(name, safe="")))
    hashes = check_packages(output, request, target)
    current()
    (output / "SHA256SUMS").write_text("\n".join(f"{sha}  {name}" for name, sha in sorted(hashes.items())) + "\n")


def workload(distro, output, profile="qemu"):
    """Create one RuntimeClass and one ordinary-network smoke Pod, then clean up."""
    from kubernetes import KUBECTL
    kubectl = KUBECTL[distro]
    identifier = 'kata-smoke-' + uuid.uuid4().hex[:12]
    manifest = {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'node.k8s.io/v1', 'kind': 'RuntimeClass',
         'metadata': {'name': identifier}, 'handler': PROFILES[profile]['handler']},
        {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': identifier, 'namespace': 'default'},
         'spec': {'runtimeClassName': identifier, 'restartPolicy': 'Never', 'activeDeadlineSeconds': 180,
                  'containers': [{'name': 'smoke', 'image': IMAGE,
                                  'command': ['/bin/sh', '-ec', 'uname -r; echo KATA_PACKAGE_SMOKE_OK'],
                                  'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'},
                                                'limits': {'memory': '512Mi'}}}]}}]}
    path = output / 'smoke-pod.json'
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    try:
        run(*kubectl, 'apply', '-f', path, timeout=60)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            pod = json.loads(run(*kubectl, 'get', 'pod', identifier, '-o', 'json', timeout=30))
            (output / 'pod.json').write_text(json.dumps(pod, indent=2) + '\n')
            from smoke_debug import record_shims
            record_shims(output)
            if pod['spec'].get('runtimeClassName') != identifier:
                raise ValueError('smoke pod did not select the Kata RuntimeClass')
            phase = pod.get('status', {}).get('phase')
            if phase == 'Failed':
                raise ValueError('Kata smoke pod failed')
            if phase == 'Succeeded':
                break
            time.sleep(3)
        else:
            raise TimeoutError('Kata smoke pod did not finish')
        logs = run(*kubectl, 'logs', identifier, timeout=30)
        (output / 'pod.log').write_text(logs + '\n')
        statuses = pod.get('status', {}).get('containerStatuses', [])
        if len(statuses) != 1 or statuses[0].get('state', {}).get('terminated', {}).get('exitCode') != 0 or 'KATA_PACKAGE_SMOKE_OK' not in logs:
            raise ValueError('Kata workload failed')
    finally:
        for name, command in (
            ('pod-describe.txt', ['describe', 'pod', identifier]),
            ('events.txt', ['get', 'events', '--sort-by=.metadata.creationTimestamp']),
            ('nodes.txt', ['get', 'nodes', '-o', 'wide'])):
            with (output / name).open('w') as stream:
                subprocess.run([*kubectl, *command], stdout=stream, stderr=subprocess.STDOUT, check=False, timeout=30)
        subprocess.run([*kubectl, 'delete', '-f', str(path), '--ignore-not-found', '--wait=false'],
                       check=False, timeout=30)


def install_packages(packages, target, request, profile="qemu"):
    selected = installed_packages(profile)
    entry = 'kata-containers' if profile == 'qemu' else 'kata-containers-' + profile
    from validate import apt_repository, ENV
    paths = sorted(packages.glob('*.' + target['format']))
    if target['format'] == 'deb':
        subprocess.run(['apt-get', 'install', '-y', 'dpkg-dev', 'cryptsetup-bin'], env=ENV, check=True, timeout=300)
        with apt_repository(paths) as options:
            subprocess.run(['apt-get', *options, 'install', '-y', '--no-install-recommends',
                            entry + '=' + request['package_version']], env=ENV, check=True, timeout=600)
        for name in selected:
            if run('dpkg-query', '-W', '-f=${Version}', name) != request['package_version']:
                raise ValueError('installed Debian package version mismatch')
            if run('dpkg', '--verify', name):
                raise ValueError('installed package verification failed')
    else:
        subprocess.run(['dnf', 'install', '-y', 'createrepo_c', 'veritysetup'], check=True, timeout=300)
        subprocess.run(['createrepo_c', str(packages)], check=True, timeout=60)
        subprocess.run(['dnf', '--repofrompath=kata-smoke,file://' + str(packages),
                        '--setopt=kata-smoke.gpgcheck=0', 'install', '-y',
                        entry + '-' + request['package_version'] + '.el10'], check=True, timeout=600)
        for name in selected:
            if run('rpm', '-q', '--qf', '%{VERSION}-%{RELEASE}', name) != request['package_version'] + '.el10':
                raise ValueError('installed RPM version mismatch')
            if run('rpm', '-V', name):
                raise ValueError('installed package verification failed')
    subprocess.run(['python3', str(ROOT / 'scripts/verify_image.py')], check=True, timeout=120)


def verify_erofs(output):
    configs = list(output.glob('containerd-effective-config-*.toml'))
    if not configs:
        raise ValueError('no effective containerd configuration recorded for NVIDIA CPU')
    for path in configs:
        plugins = tomllib.loads(path.read_text())['plugins']
        images = plugins['io.containerd.cri.v1.images']
        snapshotter = plugins['io.containerd.snapshotter.v1.erofs']
        differ = plugins['io.containerd.differ.v1.erofs']
        if (images['runtime_platforms']['kata-nvidia-cpu']['snapshotter'] != 'erofs'
                or images.get('disable_snapshot_annotations', True)
                or snapshotter.get('dmverity_mode') != 'on'
                or snapshotter.get('default_size') != '0'
                or snapshotter.get('enable_fsverity', True)
                or not differ.get('enable_dmverity')):
            raise ValueError('running containerd does not have the required NVIDIA CPU EROFS/dm-verity settings')


def workload_modes(distro, output, profile="qemu"):
    from kubernetes import wait_ready, SERVICES
    from smoke_debug import configure, verify_containerd, collect
    results = {}
    for name, debug in (('normal', False), ('debug', True)):
        folder = output / name
        folder.mkdir()
        since = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        try:
            if profile == 'qemu':
                configure(distro, folder, debug)
            else:
                configure(distro, folder, debug, profile=profile)
            subprocess.run(['systemctl', 'restart', SERVICES[distro]], check=True, timeout=600)
            wait_ready(distro)
            verify_containerd(folder, debug, distro)
            if profile == 'nvidia-cpu':
                verify_erofs(folder)
            if profile == 'qemu':
                workload(distro, folder)
            else:
                workload(distro, folder, profile=profile)
            results[name] = {'result': 'passed'}
        except Exception as error:
            results[name] = {'result': 'failed', 'error': str(error)}
            (folder / 'failure.txt').write_text(traceback.format_exc())
        finally:
            try:
                collect(distro, folder, since)
            except Exception:
                (folder / 'diagnostics-error.txt').write_text(traceback.format_exc())
        (folder / 'result.json').write_text(json.dumps(results[name], indent=2) + '\n')
    (output / 'mode-results.json').write_text(json.dumps(results, indent=2) + '\n')
    failed = [name for name, result in results.items() if result['result'] != 'passed']
    if failed:
        raise ValueError('Kata smoke modes failed: ' + ', '.join(failed))
    return results


def smoke(request, receipt, target, packages, output, distro='kubeadm', profile='qemu'):
    release = platform.freedesktop_os_release()
    expected_os = ('ubuntu', '26.04') if target['format'] == 'deb' else ('almalinux', '10.2')
    if os.geteuid() or (release.get('ID'), release.get('VERSION_ID')) != expected_os or platform.machine() != target['arch']:
        raise ValueError('run as root on the matching disposable target host')
    if not os.access('/dev/kvm', os.R_OK | os.W_OK):
        raise ValueError('the smoke-test VM needs usable /dev/kvm')
    import fcntl
    with open('/dev/kvm', 'rb') as kvm:
        if fcntl.ioctl(kvm, 0xAE00, 0) != 12:  # KVM_GET_API_VERSION
            raise ValueError('unexpected KVM API version')
    hashes = check_packages(packages, request, target)
    output.mkdir(parents=True, exist_ok=True)
    try:
        with (output / 'installation.log').open('w') as log:
            # Capture children as well as the Python installer. Kubernetes comes first.
            with open('/dev/null') as devnull:
                command = ['python3', str(ROOT / 'scripts/smoke.py'), 'install-kubernetes',
                           '--k8s-distro', distro, '--profile', profile, '--output', str(output)]
                subprocess.run(command, stdin=devnull, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1500)
            install_packages(packages, target, request, profile)
        provenance = json.loads(Path('/usr/share/doc/kata-containers-core/source-build.json').read_text())
        if provenance['inputs']['source_revision'] != request['source_revision'] or provenance['packaging'].get('build_request') != request:
            raise ValueError('installed package provenance does not match the development build')
        subprocess.run(['/usr/libexec/kata-containers/load-modules', '--all-installed'], check=True, timeout=30)
        for mode in ('--dry-run', '--apply', '--apply', '--status'):
            with (output / ('setup-' + mode[2:] + '.txt')).open('a') as log:
                subprocess.run(['kata-containers-setup', '--k8s-distro', distro, mode], stdout=log,
                               stderr=subprocess.STDOUT, check=True, timeout=60)
        modes = workload_modes(distro, output, profile)
        (output / 'result.json').write_text(json.dumps({'request_id': request['request_id'],
            'source_revision': request['source_revision'], 'packaging_revision': request['packaging_revision'],
            'source_md5': receipt['source_md5'], 'target': target['id'], 'k8s_distro': distro,
            'modes': modes, 'packages': hashes, 'runtime_handler': PROFILES[profile]['handler'], 'image': IMAGE, 'result': 'passed'}, indent=2) + '\n')
    finally:
        with (output / 'journal.log').open('w') as stream:
            subprocess.run(['journalctl', '--since', '-45min', '--no-pager'],
                           stdout=stream, stderr=subprocess.STDOUT, check=False, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("download", "run", "install-kubernetes", "install-containerd"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--target")
    from kubernetes import DISTROS
    parser.add_argument("--k8s-distro", choices=DISTROS, default="kubeadm")
    parser.add_argument("--profile", choices=PROFILES, default="qemu")
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--disposable", action="store_true", help="confirm this runner is disposable; run mode installs packages and restarts containerd")
    args = parser.parse_args()
    if args.mode == "install-containerd":
        from kubernetes import install_containerd
        install_containerd(args.output)
        return
    if args.mode == "install-kubernetes":
        from kubernetes import install
        install(args.k8s_distro, args.output, args.profile)
        return
    if not args.request or not args.receipt or not args.target:
        parser.error("--request, --receipt and --target are required")
    request = validate_request(json.loads(args.request.read_text()))
    receipt = json.loads(args.receipt.read_text())
    if receipt["request_id"] != request["request_id"]:
        parser.error("receipt does not match build request")
    target = next(target for target in receipt["targets"] if target["id"] == args.target)
    if args.mode == "download":
        download(request, receipt, target, args.output.resolve())
    else:
        if not args.disposable or args.packages is None:
            parser.error("run mode requires --disposable and --packages")
        smoke(request, receipt, target, args.packages.resolve(), args.output.resolve(), args.k8s_distro, args.profile)


if __name__ == "__main__":
    main()
