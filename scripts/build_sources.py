#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the QEMU-only DEB/RPM POC components in an external Kata checkout."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import shlex
import subprocess
import sys
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
COMPONENTS = ("qemu", "kernel", "agent", "rootfs-image", "ovmf", "virtiofsd", "shim-v2-rust")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="pinned external Kata source checkout")
    parser.add_argument("--component", choices=COMPONENTS, action="append",
                        help="build only these components (caller supplies prerequisites)")
    parser.add_argument("--resume", action="store_true", help="skip completed components after hash verification")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    components = args.component or COMPONENTS
    if args.plan:
        print("\n".join(components))
        return 0
    if args.source is None:
        parser.error("--source is required unless --plan is used")
    ROOT = args.source.resolve()
    if platform.system() != "Linux" or platform.machine() not in ("aarch64", "x86_64"):
        parser.error("run natively on an Ubuntu 26.04 or AlmaLinux 10.2 x86_64/aarch64 Linux builder")
    release = Path("/etc/os-release").read_text()
    distro = dict(line.split("=", 1) for line in release.splitlines() if "=" in line)
    distro = {key: shlex.split(value)[0] for key, value in distro.items() if value}
    if not ((distro.get("ID") == "ubuntu" and distro.get("VERSION_ID") == "26.04") or
            (distro.get("ID") == "almalinux" and distro.get("VERSION_ID") == "10.2")):
        parser.error("this POC requires Ubuntu 26.04 or AlmaLinux 10.2")
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
    local = ROOT / "tools/packaging/kata-deploy/local-build"
    output = local / "build"
    logs = output / "ubuntu-source-build"
    logs.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock = (logs / "lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    identity = {"source_revision": revision, "versions_sha256": digest(ROOT / "versions.yaml"),
                "architecture": platform.machine(), "measured_rootfs": True, "runtime": "rust",
                "source_patch_sha256": hashlib.sha256(subprocess.check_output(
                    ["git", "diff", "--binary", "HEAD"], cwd=ROOT)).hexdigest()}
    status_path = logs / "status.json"
    status = {"inputs": identity, "started": now(), "components": {}}
    if args.resume and status_path.exists():
        status = json.loads(status_path.read_text())
        if status["inputs"] != identity:
            parser.error("build inputs changed; use a clean workspace for the new source revision")
    env = dict(os.environ, USE_CACHE="no", USE_ORAS_CACHE="no", PUSH_TO_REGISTRY="no",
               RUNTIME_CHOICE="rust", MEASURED_ROOTFS="yes", CONFIDENTIAL_GUEST="no",
               HOME=str(Path.home()), USER=pwd.getpwuid(os.getuid()).pw_name)
    # Prevent credentials from entering nested builder containers.
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ARTEFACT_REGISTRY_PASSWORD"):
        env.pop(name, None)

    def save():
        temp = status_path.with_suffix(".tmp")
        temp.write_text(json.dumps(status, indent=2) + "\n")
        temp.replace(status_path)

    for component in components:
        record = status["components"].get(component, {})
        artifacts = record.get("artifacts", {})
        if args.resume and record.get("state") == "complete" and artifacts and all(
            (output / name).is_file() and digest(output / name) == value
            for name, value in artifacts.items()
        ):
            print(f"Verified completed {component}", flush=True)
            continue
        record = {"state": "building", "started": now(), "log": str(logs / (component + ".log"))}
        status["components"][component] = record
        save()
        print(f"Building {component}; log: {record['log']}", flush=True)
        with Path(record["log"]).open("a") as log:
            result = subprocess.run(["make", "-C", str(local), component + "-tarball", "DEPS="],
                                    env=env, stdout=log, stderr=subprocess.STDOUT)
        record["finished"] = now()
        if result.returncode:
            record.update(state="failed", exit_code=result.returncode)
            save()
            print(f"Failed: {component}; see {record['log']}", file=sys.stderr, flush=True)
            return result.returncode
        archive = output / f"kata-static-{component}.tar.zst"
        if not archive.is_file():
            record.update(state="failed", error="build returned success without its archive")
            save()
            return 1
        files = [archive]
        record.update(state="complete", artifacts={p.name: digest(p) for p in files})
        save()
        print(f"Completed {component}", flush=True)
    status["finished"] = now()
    save()
    print(f"Completed requested source builds: {status_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
