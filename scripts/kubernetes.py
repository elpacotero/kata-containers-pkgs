#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Deploy Kubernetes using pinned Kata CI helpers on disposable test hosts."""
import os
from pathlib import Path
import platform
import subprocess

DISTROS = ('kubeadm', 'k0s', 'k3s', 'microk8s', 'rke2')
SERVICES = {'kubeadm': 'containerd', 'k0s': 'k0scontroller', 'k3s': 'k3s',
            'microk8s': 'snap.microk8s.daemon-containerd', 'rke2': 'rke2-server'}
KUBECTL = {'kubeadm': ['kubectl', '--kubeconfig=/etc/kubernetes/admin.conf'],
           'k0s': ['k0s', 'kubectl'], 'k3s': ['k3s', 'kubectl'],
           'microk8s': ['/snap/bin/microk8s', 'kubectl'],
           'rke2': ['/var/lib/rancher/rke2/bin/kubectl', '--kubeconfig=/etc/rancher/rke2/rke2.yaml']}


def shell(script, env=None):
    environment = dict(os.environ if env is None else env)
    environment.pop('BASH_ENV', None)
    environment.pop('ENV', None)
    subprocess.run(['bash', '-euxo', 'pipefail', '-c', script], env=environment, check=True, timeout=900)


def install(distro, output, profile="qemu"):
    if distro not in DISTROS or os.geteuid():
        raise ValueError('select a supported Kubernetes distro and run as root')
    if profile not in ('qemu', 'nvidia-cpu') or (profile == 'nvidia-cpu' and distro != 'kubeadm'):
        raise ValueError('NVIDIA CPU CI currently supports kubeadm only')
    output.mkdir(parents=True, exist_ok=True)
    # Hosts are disposable. Retain the real distribution SELinux policy mode.
    shell('swapoff -a; modprobe overlay; modprobe br_netfilter; '
          'sysctl -w net.ipv4.ip_forward=1 net.bridge.bridge-nf-call-iptables=1')
    ubuntu = platform.freedesktop_os_release().get('ID') == 'ubuntu'
    if ubuntu:
        shell('export DEBIAN_FRONTEND=noninteractive; apt-get update; '
              'apt-get install -y curl ca-certificates gnupg python3 kmod iptables conntrack socat jq wget')
    else:
        shell('dnf install -y curl ca-certificates python3 kmod iptables conntrack-tools socat jq wget sudo; '
              'systemctl disable --now firewalld || test "$(systemctl is-active firewalld)" = inactive')
        # Upstream helpers invoke tools installed in /usr/local/bin and
        # /snap/bin through sudo; AlmaLinux's default secure_path omits both.
        sudo_path = Path('/etc/sudoers.d/kata-smoke-path')
        sudo_path.write_text('Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"\n')
        sudo_path.chmod(0o440)
        subprocess.run(['visudo', '-cf', str(sudo_path)], check=True, timeout=30)
    if distro != 'kubeadm' or ubuntu:
        upstream = Path(__file__).resolve().parents[1] / 'upstream-kata'
        if not (upstream / 'tests/gha-run-k8s-common.sh').is_file():
            raise ValueError('Kubernetes setup requires the matching upstream Kata CI checkout')
        if distro == 'microk8s':
            if ubuntu:
                shell('apt-get install -y snapd')
            else:
                shell('dnf install -y epel-release; dnf install -y snapd; '
                      'systemctl enable --now snapd.socket; '
                      'test -e /snap || ln -s /var/lib/snapd/snap /snap')
            shell('snap wait system seed.loaded')
        env = dict(os.environ, KUBERNETES='vanilla' if distro == 'kubeadm' else distro,
                   KATA_HOST_OS='ubuntu' if ubuntu else 'almalinux',
                   KATA_HYPERVISOR='qemu-runtime-rs', CONTAINER_ENGINE='containerd',
                   CONTAINER_ENGINE_VERSION='latest' if profile == 'nvidia-cpu' else 'distro',
                   KATA_PACKAGE_PROFILE=profile, INSTALL_IN_GOPATH='false',
                   target_branch='main', USER='root')
        env.pop('BASH_ENV', None)
        env.pop('ENV', None)
        if not ubuntu:
            env['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin'
        adapter = Path(__file__).with_name('setup-kubernetes.sh')
        subprocess.run(['bash', str(adapter), str(upstream), str(output.resolve())], env=env, check=True, timeout=1200)
        if distro == 'kubeadm':
            with (output / 'containerd-package.txt').open('w') as stream:
                packages = ['runc'] if profile == 'nvidia-cpu' else ['containerd', 'runc']
                subprocess.run(['dpkg-query', '-W', *packages], stdout=stream, check=True, timeout=30)
            with (output / 'containerd-version.txt').open('w') as stream:
                subprocess.run(['containerd', '--version'], stdout=stream, check=True, timeout=30)
    else:
        # Resolve latest Kubernetes stable once, then use that minor's signed repo.
        version = subprocess.check_output(['curl', '--fail', '--show-error', '--location',
            '--retry', '3', '--max-time', '120', 'https://dl.k8s.io/release/stable.txt'], text=True, timeout=600).strip()
        import re
        if not re.fullmatch(r'v1\.[0-9]+\.[0-9]+', version):
            raise ValueError('unexpected Kubernetes stable version')
        minor = '.'.join(version.split('.')[:2])
        (output / 'kubernetes-version.txt').write_text(version + '\n')
        shell('dnf install -y epel-release; dnf install -y containerd runc')
        Path('/etc/yum.repos.d/kubernetes.repo').write_text(
            f'[kubernetes]\nname=Kubernetes\nbaseurl=https://pkgs.k8s.io/core:/stable:/{minor}/rpm/\n'
            f'enabled=1\ngpgcheck=1\ngpgkey=https://pkgs.k8s.io/core:/stable:/{minor}/rpm/repodata/repomd.xml.key\n')
        shell('dnf install -y kubelet kubeadm kubectl')
        shell('mkdir -p /etc/containerd; containerd config default > /etc/containerd/config.toml; '
              "sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml; "
              'systemctl enable --now containerd kubelet')
        subprocess.run(['kubeadm', 'init', '--kubernetes-version', version,
                        '--cri-socket=unix:///run/containerd/containerd.sock', '--pod-network-cidr=10.244.0.0/16'],
                       check=True, timeout=600)
        # A real CNI so the smoke Pod exercises normal Kubernetes networking.
        subprocess.run([*KUBECTL[distro], 'apply', '-f',
                        'https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml'],
                       check=True, timeout=120)
        subprocess.run([*KUBECTL[distro], 'taint', 'nodes', '--all', 'node-role.kubernetes.io/control-plane-'],
                       check=True, timeout=30)
    wait_ready(distro)
    with (output / 'kubernetes-version.json').open('w') as stream:
        subprocess.run([*KUBECTL[distro], 'version', '-o', 'json'], stdout=stream, check=True, timeout=30)


def install_containerd(output):
    """Install current stable containerd through Kata CI, without Kubernetes."""
    if os.geteuid():
        raise ValueError('containerd installation requires root on a disposable host')
    upstream = Path(__file__).resolve().parents[1] / 'upstream-kata'
    if not (upstream / 'tests/gha-run-k8s-common.sh').is_file():
        raise ValueError('containerd installation requires the matching upstream Kata CI checkout')
    output.mkdir(parents=True, exist_ok=True)
    ubuntu = platform.freedesktop_os_release().get('ID') == 'ubuntu'
    if ubuntu:
        shell('apt-get update; apt-get install -y curl ca-certificates jq wget sudo runc')
    else:
        shell('dnf install -y curl ca-certificates jq wget sudo runc')
    env = dict(os.environ, KUBERNETES='vanilla', KATA_PACKAGE_PROFILE='nvidia-cpu',
               KATA_CONTAINERD_ONLY='true', CONTAINER_ENGINE='containerd',
               CONTAINER_ENGINE_VERSION='latest', INSTALL_IN_GOPATH='false',
               KATA_HOST_OS='ubuntu' if ubuntu else 'almalinux',
               KATA_HYPERVISOR='qemu-runtime-rs', target_branch='main', USER='root')
    env.pop('BASH_ENV', None)
    env.pop('ENV', None)
    subprocess.run(['bash', str(Path(__file__).with_name('setup-kubernetes.sh')),
                    str(upstream), str(output.resolve())], env=env, check=True, timeout=600)


def wait_ready(distro):
    # CLI binaries/kubeconfigs may appear after systemd reports the server started.
    import time
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            subprocess.run([*KUBECTL[distro], 'wait', '--for=condition=Ready', 'nodes', '--all', '--timeout=30s'],
                           check=True, timeout=45)
            return
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(5)
    raise TimeoutError(f'{distro} did not become ready')
