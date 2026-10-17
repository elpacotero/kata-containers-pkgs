#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install shared staged payloads and write native RPM file lists."""
import argparse
from pathlib import Path
import shutil
from rpm_backend import file_entries


def install(stages, root, files):
    files.mkdir(parents=True, exist_ok=True)
    for stage in sorted(stages.iterdir()):
        if not (stage / 'DEBIAN/control').exists():
            raise ValueError('unexpected package staging entry: ' + str(stage))
        (files / (stage.name + '.list')).write_text('\n'.join(file_entries(stage)) + '\n')
        for source in stage.iterdir():
            if source.name == 'DEBIAN':
                continue
            destination = root / source.name
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
            else:
                root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stages', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--files', required=True, type=Path)
    args = parser.parse_args()
    install(args.stages, args.root, args.files)
