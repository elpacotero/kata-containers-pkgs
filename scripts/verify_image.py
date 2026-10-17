#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify the actual dm-verity trees in the packaged measured guest images."""

import argparse
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


def verify(image, measurement):
    params = dict(part.split("=", 1) for part in measurement.strip().split(","))
    with image.open("rb") as stream:
        mbr = stream.read(512)
        if mbr[510:512] != b"\x55\xaa":
            raise ValueError(f"missing MBR: {image}")
        data_start, data_sectors = struct.unpack_from("<II", mbr, 446 + 8)
        hash_start, hash_sectors = struct.unpack_from("<II", mbr, 462 + 8)
        if not data_sectors or not hash_sectors or data_start + data_sectors > hash_start:
            raise ValueError(f"invalid measured image partitions: {image}")
        length = int(params["data_blocks"]) * int(params["data_block_size"])
        if length > data_sectors * 512 or (hash_start + hash_sectors) * 512 > image.stat().st_size:
            raise ValueError("verity parameters exceed image bounds")
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "rootfs-data"
            stream.seek(data_start * 512)
            remaining = length
            with data.open("wb") as dest:
                while remaining:
                    block = stream.read(min(4 * 1024 * 1024, remaining))
                    if not block:
                        raise ValueError("truncated rootfs")
                    dest.write(block)
                    remaining -= len(block)
            subprocess.run([
                "veritysetup", "verify", "--no-superblock", "--hash=sha256",
                "--data-blocks=" + params["data_blocks"],
                "--data-block-size=" + params["data_block_size"],
                "--hash-block-size=" + params["hash_block_size"],
                "--salt=" + params["salt"], "--hash-offset=" + str(hash_start * 512),
                str(data), str(image), params["root_hash"],
            ], check=True)
    print(f"Verified dm-verity data and hash tree: {image}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"), help="installed root or extracted package union")
    args = parser.parse_args()
    if not shutil.which("veritysetup"):
        parser.error("install cryptsetup-bin (Ubuntu) or veritysetup (AlmaLinux)")
    base = args.root / "opt/kata/share/kata-containers"
    images = (('kata-containers.img', 'root_hash_base.txt'),
              ('kata-containers-nvidia.img', 'root_hash_nvidia.txt'))
    found = False
    for image, measurement in images:
        if (base / image).exists():
            verify(base / image, (base / measurement).read_text())
            found = True
    if not found:
        parser.error('no installed measured guest image found')



if __name__ == "__main__":
    main()
