# SPDX-License-Identifier: MIT
"""Package inventories shared by native recipes and validation."""
from pathlib import Path
import json

PROFILES = json.loads((Path(__file__).resolve().parents[1] /
                      'packaging/ubuntu-26.04/profiles/profiles.json').read_text())
X86_PACKAGES = frozenset(('kata-containers-nvidia-gpu-tee', 'kata-containers-rootfs-coco', 'kata-containers-nydus-snapshotter'))

def profile_handlers(profile):
    return PROFILES[profile].get('handlers', [PROFILES[profile]['handler']])

def profile_configurations(profile):
    return PROFILES[profile].get('configurations', [profile])

def profile_hypervisor(profile):
    return PROFILES[profile].get('hypervisor', 'qemu')

def profiles_for_arch(architecture):
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(architecture, architecture)
    return tuple(name for name, profile in PROFILES.items() if architecture in profile['architectures'])

GENERIC_PACKAGES = ('kata-containers', 'kata-containers-core', 'kata-containers-qemu',
                    'kata-containers-rootfs-generic', 'kata-containers-kernel-generic',
                    'kata-containers-virtiofsd')


def installed_packages(profile='qemu'):
    if profile == 'qemu':
        return GENERIC_PACKAGES
    selected = {'kata-containers-core', 'kata-containers-' + profile}
    for dependency in PROFILES[profile]['extra_dependencies']:
        name = dependency.split()[0]
        if name.startswith('kata-containers-'):
            selected.add(name)
            nested = name.removeprefix('kata-containers-')
            if nested in PROFILES:
                selected.update(installed_packages(nested))
    return tuple(sorted(selected))


def build_packages(profiles=None, architecture=None):
    selected = set(GENERIC_PACKAGES)
    for profile in (profiles if profiles is not None else PROFILES):
        if architecture is None or profile in profiles_for_arch(architecture):
            selected.update(installed_packages(profile))
    return tuple(sorted(selected))
