#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Add early boot capture to QEMU in the disposable debug smoke guest."""
import argparse
import json
import os
from pathlib import Path
import socket


def command(qemu, args, output, pid):
    args = list(args)
    if '-append' not in args:
        return [qemu, *args]  # Preserve version/capability probes.
    index = args.index('-append') + 1
    args[index] += (' console=ttyS0,115200 earlycon=uart8250,io,0x3f8,115200'
                    ' earlyprintk=serial,ttyS0,115200 ignore_loglevel')
    return [qemu, *args,
            '-serial', f'file:{output}/guest-kernel-{pid}.log',
            '-d', 'guest_errors,cpu_reset', '-D', f'{output}/qemu-cpu-{pid}.log',
            '-qmp', f'unix:{output}/qmp-{pid}.sock,server=on,wait=off']


def snapshot(output):
    """Query a separate diagnostic QMP socket, without consuming the shim's one."""
    for path in output.glob('qmp-*.sock'):
        report = path.with_suffix('.json')
        if report.exists():
            continue
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(str(path))
                stream = client.makefile('rwb')
                replies = [json.loads(stream.readline())]
                for request in ({'execute': 'qmp_capabilities'}, {'execute': 'query-status'},
                                {'execute': 'query-cpus-fast'},
                                {'execute': 'human-monitor-command', 'arguments': {'command-line': 'info registers'}}):
                    stream.write(json.dumps(request).encode() + b'\n');stream.flush()
                    while True:
                        reply = json.loads(stream.readline());replies.append(reply)
                        if 'return' in reply or 'error' in reply:
                            break
                report.write_text(json.dumps(replies, indent=2) + '\n')
        except (OSError, ValueError):
            # Boot capture is best-effort; never change the Pod outcome.
            continue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    values = args.args[1:] if args.args[:1] == ['--'] else args.args
    cmd = command(args.qemu, values, args.output, os.getpid())
    if '-append' in values:
        (args.output / f'qemu-command-{os.getpid()}.json').write_text(json.dumps(cmd, indent=2) + '\n')
    os.execv(args.qemu, cmd)


if __name__ == '__main__':
    main()
