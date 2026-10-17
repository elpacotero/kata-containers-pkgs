# SPDX-License-Identifier: MIT
"""Configure and capture logging on disposable Kubernetes smoke-test guests."""
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tomllib


def set_debug_level(text, level):
    header = re.search(r'(?m)^\[debug\][ \t]*(?:#.*)?$', text)
    if not header:
        return text.rstrip() + f'\n\n[debug]\nlevel = "{level}"\n'
    end = re.search(r'(?m)^\[', text[header.end():])
    stop = header.end() + end.start() if end else len(text)
    section = text[header.end():stop]
    section, changed = re.subn(r'(?m)^[ \t]*level\s*=.*$', f'level = "{level}"', section, count=1)
    if not changed:
        section = f'\nlevel = "{level}"\n' + section
    return text[:header.end()] + section + text[stop:]


def effective_kata_config(config):
    def merge(base, overlay):
        for key, value in overlay.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value
    result = tomllib.loads(config.read_text())
    for path in sorted((config.parent / 'config.d').iterdir()):
        merge(result, tomllib.loads(path.read_text()))
    return result


def verify_kata_config(config, output, debug):
    effective = effective_kata_config(config)
    (output / 'kata-effective-config.json').write_text(json.dumps(effective, indent=2) + '\n')
    level = 'debug' if debug else 'info'
    sections = (effective['hypervisor']['qemu'], effective['runtime'], effective['agent']['kata'])
    if any(section.get('enable_debug') != debug or section.get('log_level') != level for section in sections):
        raise ValueError('merged Kata configuration has incorrect component debug flags or log levels')
    if effective['agent']['kata'].get('debug_console_enabled', False) != debug:
        raise ValueError('merged Kata agent debug console setting is incorrect')
    params = effective['hypervisor']['qemu'].get('kernel_params', '').split()
    if any((parameter in params) != debug for parameter in ('agent.log=debug', 'initcall_debug')):
        raise ValueError('merged Kata configuration has incorrect guest debug kernel parameters')


def configure(distro, output, debug, profile="qemu"):
    level, enabled = ('debug', 'true') if debug else ('info', 'false')
    config = Path('/opt/kata/share/defaults/kata-containers/configuration-' + profile + '.toml').resolve(strict=True)
    folder = config.parent / 'config.d'
    if not folder.is_dir():
        raise ValueError('profile package did not install its config.d directory')
    dropin = folder / '20-debug.toml'
    dropin.write_text(''.join(f'[{section}]\nenable_debug = {enabled}\nlog_level = "{level}"\n\n'
                             for section in ('hypervisor.qemu', 'runtime', 'agent.kata'))
                      + f'debug_console_enabled = {enabled}\n')
    kernel_dropin = folder / '30-kernel-params.toml'
    if debug:
        base = tomllib.loads(config.read_text())['hypervisor']['qemu'].get('kernel_params', '')
        params = (base + ' agent.log=debug initcall_debug').strip()
        kernel_dropin.write_text('[hypervisor.qemu]\nkernel_params = ' + json.dumps(params) + '\n')
        shutil.copyfile(kernel_dropin, output / 'kata-kernel-params.toml')
    else:
        kernel_dropin.unlink(missing_ok=True)
    console_dropin = folder / '40-early-console.toml'
    if debug:
        qemu = tomllib.loads(config.read_text())['hypervisor']['qemu']['path']
        wrapper = output.resolve() / 'qemu-debug'
        helper = Path(__file__).resolve().with_name('smoke_qemu.py')
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(
            ['/usr/bin/python3', '-I', '-S', str(helper), '--qemu', qemu, '--output', str(output.resolve()), '--']) + ' "$@"\n')
        wrapper.chmod(0o755)
        console_dropin.write_text('[hypervisor.qemu]\npath = ' + json.dumps(str(wrapper))
                                 + '\nvalid_hypervisor_paths = ' + json.dumps([qemu, str(wrapper)]) + '\n')
        shutil.copyfile(console_dropin, output / 'kata-early-console.toml')
    else:
        console_dropin.unlink(missing_ok=True)
    verify_kata_config(config, output, debug)
    shutil.copyfile(config, output / 'kata-base.toml')
    shutil.copyfile(dropin, output / 'kata-debug.toml')
    if distro in ('k3s', 'rke2'):
        service = 'k3s' if distro == 'k3s' else 'rke2-server'
        # K3s (also embedded by RKE2) forwards this variable as containerd's
        # --log-level flag. Debug also copies the file log into the journal.
        path = Path(f'/etc/systemd/system/{service}.service.d/90-kata-smoke-logging.conf')
        text = (f'[Service]\nEnvironment="CONTAINERD_LOG_LEVEL={level}"\n'
                f'Environment="{distro.upper()}_DEBUG={enabled}"\n')
    elif distro == 'k0s':
        # Kata CI installs k0s with containerd=debug in the service command.
        # Change that flag too, since it overrides the TOML debug level.
        unit = Path('/etc/systemd/system/k0scontroller.service')
        if unit.exists():
            unit.write_text(re.sub(r'containerd=(?:debug|info)', 'containerd=' + level, unit.read_text()))
            shutil.copyfile(unit, output / 'k0scontroller.service')
        version = tomllib.loads(Path('/etc/k0s/containerd.toml').read_text()).get('version', 2)
        path = Path('/etc/k0s/containerd.d/90-kata-smoke-logging.toml')
        text = f'version = {version}\n[debug]\nlevel = "{level}"\n'
    else:
        path = Path('/etc/containerd/config.toml' if distro == 'kubeadm' else
                    '/var/snap/microk8s/current/args/containerd-template.toml')
        text = set_debug_level(path.read_text(), level)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    shutil.copyfile(path, output / ('containerd-logging.conf' if distro in ('k3s', 'rke2') else 'containerd-config.toml'))
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=30)


def containerd_command(args):
    if not args:
        return False
    name = Path(args[0].strip()).name
    return name == 'containerd' or (name in ('k3s', 'rke2') and args[1:2] == ['containerd'])


def containerd_startup_args(journal):
    commands = []
    for line in journal.splitlines():
        match = re.search(r'msg=("(?:[^"\\]|\\.)*")', line)
        if match:
            message = json.loads(match[1])
            if message.startswith('Running containerd '):
                commands.append(['containerd', *shlex.split(message.removeprefix('Running containerd '))])
    if not commands:
        raise ValueError('containerd hid its argv and the service has no startup command record')
    return commands[-1]


def verify_containerd(output, debug, distro=None):
    level = 'debug' if debug else 'info'
    processes = []
    candidates = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / 'comm').read_text().strip() != 'containerd':
                continue
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').strip('\0').split('\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        candidates.append({'pid': proc.name, 'args': args})
        (output / 'containerd-candidates.json').write_text(json.dumps(candidates, indent=2) + '\n')
        # K3s's multicall parent can have comm=containerd too. Inspect argv
        # before treating it as the actual child containerd process.
        if not containerd_command(args):
            continue
        raw_args = args
        if len(args) == 1 and distro in ('k3s', 'rke2'):
            from kubernetes import SERVICES
            # K3s replaces /proc/cmdline with "containerd ". Its supervisor
            # logs the real child command at every start; retain that evidence.
            journal = subprocess.check_output(['journalctl', '-u', SERVICES[distro], '-b',
                                               '--no-pager', '-o', 'cat', '-g', 'Running containerd '],
                                              text=True, timeout=30)
            (output / 'containerd-startup.log').write_text(journal)
            args = containerd_startup_args(journal)
        configured, path = None, '/etc/containerd/config.toml'
        for i, arg in enumerate(args):
            if arg.startswith('--log-level='):
                configured = arg.split('=', 1)[1]
            elif arg in ('--log-level', '-l'):
                configured = args[i + 1]
            elif arg in ('--config', '-c'):
                path = args[i + 1]
            elif arg.startswith('--config='):
                path = arg.split('=', 1)[1]
        if configured is None:
            executable = str((proc / 'exe').resolve(strict=True))
            prefix = args[:2] if args[1:2] == ['containerd'] else args[:1]
            effective = subprocess.check_output([*prefix, '--config', path, 'config', 'dump'],
                                                executable=executable, text=True, timeout=30)
            (output / f'containerd-effective-config-{proc.name}.toml').write_text(effective)
            configured = tomllib.loads(effective).get('debug', {}).get('level', 'info')
        processes.append({'pid': proc.name, 'args': args, 'proc_args': raw_args,
                          'config': path, 'log_level': configured})
    (output / 'containerd-processes.json').write_text(json.dumps(processes, indent=2) + '\n')
    if not processes or any(proc['log_level'] != level for proc in processes):
        raise ValueError(f'running containerd did not enable {level} logging')


def record_shims(output):
    from smoke_qemu import snapshot
    snapshot(output)
    procfs = Path('/proc')
    if not procfs.is_dir():
        return
    path = output / 'kata-shim-processes.json'
    processes = json.loads(path.read_text()) if path.exists() else {}
    for proc in procfs.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').strip('\0').split('\0')
        except OSError:
            continue
        if args and args[0].endswith('/containerd-shim-kata-v2'):
            processes[proc.name] = {'args': args, 'debug_flag': '-debug' in args or '--debug' in args}
    path.write_text(json.dumps(processes, indent=2) + '\n')


def collect(distro, output, since):
    from kubernetes import SERVICES
    commands = {'journal.log': ['journalctl', '--since', since, '--no-pager'],
                'host-kernel.log': ['journalctl', '-k', '-b', '--no-pager'],
                'containerd-journal.log': ['journalctl', '-u', SERVICES[distro], '--since', since, '--no-pager'],
                'kata-journal.log': ['journalctl', '-t', 'kata', '--since', since, '--no-pager']}
    for filename, command in commands.items():
        with (output / filename).open('w') as stream:
            try:
                subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as error:
                stream.write(str(error) + '\n')
    if distro in ('k3s', 'rke2'):
        path = Path(f'/var/lib/rancher/{distro}/agent/containerd/containerd.log')
        if path.exists():
            shutil.copyfile(path, output / 'containerd.log')
