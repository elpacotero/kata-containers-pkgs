#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Own exactly one kcli VM per hosted-runner job; copy diagnostics before cleanup."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.smoke-vm'
KCLI = ['sudo', '-n', str(ROOT / '.smoke-venv/bin/kcli')]


def call(*args, **kwargs):
    return subprocess.check_output(list(map(str, args)), text=True, **kwargs).strip()


def vm_plan(target, name, key):
    arch = target['arch']
    if arch != "x86_64":
        raise ValueError("hosted smoke tests currently support x86_64 only")
    if target['format'] == 'deb':
        image_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch]
        image = f'https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-{image_arch}.img'
    else:
        image = f'https://repo.almalinux.org/almalinux/10.2/cloud/{arch}/images/AlmaLinux-10-GenericCloud-latest.{arch}.qcow2'
    # One VM fits in the smallest standard runner: 2 CPUs, 4 GiB RAM, sparse disk.
    return image, {name: {'image': image.rsplit('/', 1)[1], 'numcpus': 2, 'memory': 4096,
                         'cpumodel': 'host-passthrough', 'nested': True, 'arch': arch,
                         'disks': [24], 'nets': ['default'], 'enableroot': True,
                         'keys': [key], 'cmds': ['echo kata-smoke-cloud-init-ready']}}


def ssh_options(ip):
    return ['-i', str(STATE / 'id_ed25519'), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={STATE / "known_hosts"}', 'root@' + ip]


def cleanup():
    state = STATE / 'vm.json'
    if state.exists():
        data = json.loads(state.read_text())
        # Name is generated here, never accept an arbitrary libvirt domain name.
        import re
        if not re.fullmatch(r'kata-smoke-[0-9]+-[0-9]+', data['name']):
            raise ValueError('invalid owned VM name')
        domains = call('sudo', '-n', 'virsh', 'list', '--all', '--name', timeout=30).splitlines()
        if data['name'] in domains:
            subprocess.run([*KCLI, 'delete', 'plan', '-y', data['name']], check=True, timeout=120)
        state.unlink()


def prepare_guest(ssh, target, output):
    if target['format'] != 'rpm':
        return
    # GenericCloud omits networking modules. Install a matching kernel/module
    # set and boot it, rather than assuming the image's old kernel is in repos.
    with (output / 'guest-kernel-setup.log').open('w') as log:
        subprocess.run([*ssh, 'dnf install -y kernel kernel-modules kernel-modules-extra grubby'],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
        subprocess.run([*ssh, "grubby --set-default /boot/vmlinuz-$(rpm -q kernel-core --qf '%{VERSION}-%{RELEASE}.%{ARCH}\\n' | sort -V | tail -n 1)"],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=30)
    previous = call(*ssh, 'cat /proc/sys/kernel/random/boot_id', timeout=20)
    subprocess.run([*ssh, 'systemctl reboot'], check=False, timeout=30)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            current = call(*ssh, 'cat /proc/sys/kernel/random/boot_id', timeout=15)
            if current != previous:
                call(*ssh, 'set -e; modprobe br_netfilter; modinfo br_netfilter; uname -r', timeout=30)
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(5)
    raise TimeoutError('guest did not boot its kernel/module set')


def execute(target, distro):
    if target['arch'] != 'x86_64' or platform.machine() != target['arch']:
        raise ValueError('runner and guest architectures must match')
    STATE.mkdir(exist_ok=True)
    if (STATE / 'vm.json').exists():
        raise ValueError('this job already owns a VM')
    identifier = os.environ.get('GITHUB_RUN_ID', '0')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if not identifier.isdigit() or not attempt.isdigit():
        raise ValueError('invalid run identifier')
    name = f'kata-smoke-{identifier}-{attempt}'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(STATE / 'id_ed25519')], check=True)
    image, plan = vm_plan(target, name, (STATE / 'id_ed25519.pub').read_text().strip())
    plan_file = STATE / 'plan.json'  # JSON is also valid YAML for kcli.
    plan_file.write_text(json.dumps(plan, indent=2) + '\n')
    output = ROOT / 'smoke-results'
    output.mkdir(exist_ok=True)
    (output / 'vm-plan.json').write_text(json.dumps({'image': image, 'plan': plan}, indent=2) + '\n')
    (STATE / 'vm.json').write_text(json.dumps({'name': name}) + '\n')
    ip = None
    try:
        call(*KCLI, 'download', 'image', '-u', image, '-P', 'arch=' + target['arch'], timeout=600)
        call(*KCLI, 'create', 'plan', '-f', plan_file, name, timeout=300)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                info = json.loads(call(*KCLI, 'info', 'vm', '-o', 'json', name, timeout=20))
                ip = str(ipaddress.ip_address(info['ip']))
                call('ssh', *ssh_options(ip), 'true', timeout=15)
                break
            except (KeyError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                time.sleep(5)
        else:
            raise TimeoutError('test VM did not become SSH-ready')
        ssh = ['ssh', *ssh_options(ip)]
        call(*ssh, 'cloud-init status --wait', timeout=300)
        prepare_guest(ssh, target, output)
        call(*ssh, 'mkdir -p /root/kata-smoke', timeout=30)
        # Tar only the necessary test code and inputs, never runner credentials.
        archive = STATE / 'inputs.tar'
        inputs = ['scripts', 'out/build-request.json', 'out/obs-build.json', 'packages']
        if (ROOT / 'upstream-kata').is_dir():
            inputs.append('upstream-kata')
        subprocess.run(['tar', '--exclude=upstream-kata/.git', '-cf', str(archive), *inputs], cwd=ROOT, check=True)
        with archive.open('rb') as stream:
            subprocess.run([*ssh, 'tar -xf - -C /root/kata-smoke'], stdin=stream, check=True, timeout=180)
        command = shlex.join(['python3', 'scripts/smoke.py', 'run', '--disposable',
                             '--k8s-distro', distro, '--target', target['id'],
                             '--request', 'out/build-request.json', '--receipt', 'out/obs-build.json',
                             '--packages', 'packages', '--output', 'smoke-results'])
        # Images carry Python; install it explicitly if a future minimal image does not.
        call(*ssh, 'command -v python3 >/dev/null || '
             '(command -v apt-get >/dev/null && apt-get update && apt-get install -y python3) || dnf install -y python3', timeout=300)
        with (output / 'guest-console.log').open('w') as stream:
            subprocess.run([*ssh, 'cd /root/kata-smoke && ' + command], stdout=stream,
                           stderr=subprocess.STDOUT, check=True, timeout=2400)
    finally:
        try:
            if ip:
                with (output / 'guest-diagnostics.tar').open('wb') as stream:
                    subprocess.run(['ssh', *ssh_options(ip),
                                    'tar -cf - -C /root/kata-smoke smoke-results'], stdout=stream, check=False, timeout=120)
                # Keep the tar as an artifact, without extracting untrusted guest paths.
            with (output / 'runner-kernel.log').open('w') as stream:
                subprocess.run(['sudo', '-n', 'dmesg', '--ctime'], stdout=stream, stderr=subprocess.STDOUT, check=False, timeout=30)
            with (output / 'libvirt-domain.xml').open('w') as stream:
                subprocess.run(['sudo', '-n', 'virsh', 'dumpxml', name], stdout=stream, check=False, timeout=30)
        finally:
            cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('run', 'cleanup'))
    parser.add_argument('--target')
    from kubernetes import DISTROS
    parser.add_argument('--k8s-distro', choices=DISTROS)
    args = parser.parse_args()
    if args.mode == 'cleanup':
        cleanup()
        return
    if not args.target or not args.k8s_distro:
        parser.error('run requires --target and --k8s-distro')
    receipt = json.loads((ROOT / 'out/obs-build.json').read_text())
    target = next(t for t in receipt['targets'] if t['id'] == args.target)
    execute(target, args.k8s_distro)


if __name__ == '__main__':
    main()
