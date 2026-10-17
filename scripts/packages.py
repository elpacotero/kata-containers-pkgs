# SPDX-License-Identifier: MIT
"""Package inventories shared by native recipes and validation."""
from pathlib import Path
import json

PROFILES = json.loads((Path(__file__).resolve().parents[1] /
                      'packaging/ubuntu-26.04/profiles/profiles.json').read_text())
GENERIC_PACKAGES = ('kata-containers', 'kata-containers-core', 'kata-containers-qemu',
                    'kata-containers-rootfs-generic', 'kata-containers-kernel-generic',
                    'kata-containers-virtiofsd')


def installed_packages(profile='qemu'):
    if profile == 'qemu':
        return GENERIC_PACKAGES
    return ('kata-containers-core', 'kata-containers-' + profile) + tuple(
        dependency.split()[0] for dependency in PROFILES[profile]['extra_dependencies']
        if dependency.startswith('kata-containers-'))


def build_packages(profiles=None):
    selected = set(GENERIC_PACKAGES)
    for profile in (profiles if profiles is not None else PROFILES):
        selected.update(installed_packages(profile))
    return tuple(sorted(selected))
