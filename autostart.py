#!/usr/bin/env python3
"""Install native macOS login/boot jobs for the existing fixed-domain relay."""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

SUPPORT = Path('/Library/Application Support/CloudLinkAssistant')
DAEMON_LABEL = 'local.hive.cloudlinkassistant.autorelay'
AGENT_LABEL = 'local.hive.cloudlinkassistant.login'
DAEMON_PLIST = Path('/Library/LaunchDaemons') / (DAEMON_LABEL + '.plist')
SOURCE = Path(__file__).resolve().parent


def operator():
    uid = int(os.environ.get('SUDO_UID', str(os.getuid())))
    if uid == 0:
        raise RuntimeError('请由普通用户通过 sudo 安装。')
    user = pwd.getpwuid(uid)
    return {'uid': uid, 'gid': user.pw_gid, 'home': user.pw_dir}


def old_state(user):
    return Path(user['home']) / 'Library/Application Support/云连接助手/relay-state'


def agent_path(user):
    return Path(user['home']) / 'Library/LaunchAgents' / (AGENT_LABEL + '.plist')


def jobs(user, app):
    daemon = {
        'Label': DAEMON_LABEL,
        'ProgramArguments': [str(SUPPORT / 'python/bin/python3'), '-I', '-B',
                             str(SUPPORT / 'autostart.py'), 'run'],
        'RunAtLoad': True,
        # A manual stop exits successfully and remains stopped until next boot.
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 30,
        'ExitTimeOut': 25,
        'ProcessType': 'Background',
        'WorkingDirectory': str(SUPPORT),
        'EnvironmentVariables': {'PYTHONNOUSERSITE': '1', 'PYTHONSAFEPATH': '1',
                                 'SUDO_UID': str(user['uid']), 'SUDO_GID': str(user['gid'])},
        'StandardOutPath': str(SUPPORT / 'autostart.log'),
        'StandardErrorPath': str(SUPPORT / 'autostart.log'),
    }
    agent = {'Label': AGENT_LABEL, 'RunAtLoad': True,
             'ProgramArguments': ['/usr/bin/open', '-a', str(app)],
             'LimitLoadToSessionType': 'Aqua', 'ProcessType': 'Interactive'}
    return daemon, agent


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError('安装/关闭开机启动需要一次管理员授权。')


def trusted(path):
    """Root jobs must never execute code/configuration writable by a normal user."""
    path = Path(path)
    for item in [path, *path.parents]:
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('系统启动文件权限不安全：' + str(item))


def load_relay(directory):
    spec = importlib.util.spec_from_file_location('cloudlink_relay', Path(directory) / 'relay.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def console_uid():
    return os.stat('/dev/console').st_uid


def repair_bypass(relay):
    data = relay.read_state() or {}
    plan = data.get('proxy_bypass') or {}
    if data.get('phase') != 'running' or not plan.get('added'):
        return False
    current = relay.read_bypass(plan['service'])
    if relay.TARGET.lower() in [name.lower() for name in current]:
        return False
    relay.set_bypass(plan['service'], current + [relay.TARGET])
    # A manual stop may race with the write. Never leave our exception behind.
    if relay.control_request('status') is None or (relay.read_state() or {}).get('phase') != 'running':
        relay.restore_bypass(plan)
        return False
    print('已恢复运行中中继的单域名代理例外。', flush=True)
    return True


def supervise(relay, uid, stopping):
    """Wait for the user's proxy; leave the GUI's manual stop in control."""
    previous = None
    while not stopping.is_set():
        if relay.control_request('status') is not None:
            break
        try:
            if console_uid() != uid:
                raise RuntimeError('等待指定用户登录')
            relay.start('Wi-Fi')
            break
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            message = '等待登录或现有系统代理：' + type(error).__name__
            if message != previous:
                print(message, flush=True)
                previous = message
            stopping.wait(15)
    if stopping.is_set():
        if relay.control_request('status') is not None:
            relay.stop()
        return 0
    print('中继已启动；手动停止后本次开机不再自动拉起。', flush=True)
    last_bypass_check = 0
    bypass_error = None
    while not stopping.wait(2):
        if relay.control_request('status') is not None:
            if time.monotonic() - last_bypass_check >= 10:
                try:
                    repair_bypass(relay)
                    bypass_error = None
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    if bypass_error != type(error).__name__:
                        print('代理例外检查暂未完成：' + type(error).__name__, flush=True)
                    bypass_error = type(error).__name__
                last_bypass_check = time.monotonic()
            continue
        data = relay.read_state() or {}
        # The worker closes its control socket before the supervisor restores hosts.
        if data.get('phase') in ('running', 'starting'):
            continue
        return 0 if data.get('phase') == 'stopped' and data.get('worker_exit_code') == 0 else 1
    relay.stop()
    return 0


def run():
    require_root()
    for name in ['autostart.py', 'relay.py', 'operator.json', 'python/bin/python3']:
        # The runtime entry point may be an internal relative symlink.
        trusted((SUPPORT / name).resolve())
    user = json.loads((SUPPORT / 'operator.json').read_text())
    if user['uid'] <= 0 or pwd.getpwuid(user['uid']).pw_gid != user['gid']:
        raise RuntimeError('启动用户记录不匹配。')
    relay = load_relay(SUPPORT)
    relay.STATE = SUPPORT / 'relay-state'
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda *_: stopping.set())
    return supervise(relay, user['uid'], stopping)


def seal_tree(root):
    for item in [root, *root.rglob('*')]:
        os.lchown(item, 0, 0)
        if item.is_symlink():
            if not item.resolve().is_relative_to(root):
                raise RuntimeError('运行环境包含目录外的符号链接。')
        else:
            os.chmod(item, stat.S_IMODE(item.stat().st_mode) & ~0o022)


def write_plist(path, data, uid=0, gid=0):
    if path.exists() or path.is_symlink():
        raise RuntimeError('启动项已存在，拒绝覆盖：' + str(path))
    with path.open('xb') as stream:
        plistlib.dump(data, stream)
    os.chmod(path, 0o644)
    os.chown(path, uid, gid)
    subprocess.run(['/usr/bin/plutil', '-lint', str(path)], check=True)


def migrate_state(original, destination):
    original.rename(destination)
    try:
        original.symlink_to(destination, target_is_directory=True)
    except BaseException:
        destination.rename(original)
        raise


def reactivate(user, app, resources):
    for name in ['operator.json', 'installation.json', 'autostart.py', 'relay.py', 'relay-state']:
        trusted(SUPPORT / name)
    if json.loads((SUPPORT / 'operator.json').read_text()) != user:
        raise RuntimeError('现有启动项属于其他用户，未修改。')
    original = old_state(user)
    if not original.is_symlink() or original.resolve() != SUPPORT / 'relay-state':
        raise RuntimeError('原状态路径已改变，保留现场。')
    agent = agent_path(user)
    if any(p.exists() or p.is_symlink() for p in [DAEMON_PLIST, agent]):
        raise RuntimeError('开机启动已开启或存在同名设置，未覆盖。')
    relay = load_relay(SUPPORT)
    relay.STATE = SUPPORT / 'relay-state'
    was_running = relay.control_request('status') is not None
    incoming = {'autostart.py': (SOURCE / 'autostart.py').read_bytes(),
                'relay.py': (resources / 'relay.py').read_bytes()}
    previous_code = {name: (SUPPORT / name).read_bytes() for name in incoming}
    if previous_code['relay.py'] != incoming['relay.py']:
        phase = (relay.read_state() or {}).get('phase')
        if was_running or phase != 'stopped':
            raise RuntimeError('中继程序需要升级，请先停止现有中继并确认设置恢复，再重新开启自动启动。')
    if previous_code != incoming:
        backup = SUPPORT / ('code-before-upgrade-' + str(time.time_ns()))
        backup.mkdir(mode=0o700)
        for name, code in previous_code.items():
            relay.atomic_write(backup / name, code)
    daemon, login = jobs(user, app)
    try:
        for name, code in incoming.items():
            relay.atomic_write(SUPPORT / name, code)
        write_plist(DAEMON_PLIST, daemon)
        write_plist(agent, login, user['uid'], user['gid'])
        subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(DAEMON_PLIST)], check=True)
        subprocess.run(['/bin/launchctl', 'bootstrap', 'gui/' + str(user['uid']), str(agent)], check=True)
        record = json.loads((SUPPORT / 'installation.json').read_text())
        record.update(app=str(app), reactivated_at=time.time())
        relay.atomic_write(SUPPORT / 'installation.json', (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
    except BaseException:
        for target in ['gui/' + str(user['uid']) + '/' + AGENT_LABEL, 'system/' + DAEMON_LABEL]:
            subprocess.run(['/bin/launchctl', 'bootout', target], capture_output=True)
        for path in [DAEMON_PLIST, agent]:
            if path.exists():
                path.rename(SUPPORT / (path.name + '.failed-' + str(time.time_ns())))
        for name, code in previous_code.items():
            relay.atomic_write(SUPPORT / name, code)
        if was_running and relay.control_request('status') is None:
            relay.start('Wi-Fi')
        raise
    print('已重新开启开机自动启动，原恢复记录保持完整。')


def install(app):
    require_root()
    user = operator()
    app = Path(app).expanduser().resolve()
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'local.hive.cloudlinkassistant':
        raise RuntimeError('所选 App 不是云连接助手。')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    resources = app / 'Contents/Resources'
    runtime = resources / 'python'
    if not (runtime / 'bin/python3').is_file():
        raise RuntimeError('需要带有内置 Python 的安装版 App。')
    if any(not p.resolve().is_relative_to(runtime) for p in runtime.rglob('*') if p.is_symlink()):
        raise RuntimeError('Python 包含目录外的符号链接。')
    original = old_state(user)
    agent = agent_path(user)
    if SUPPORT.exists() or SUPPORT.is_symlink():
        reactivate(user, app, resources)
        return
    if DAEMON_PLIST.exists() or agent.exists() or agent.is_symlink():
        raise RuntimeError('已存在同名启动设置；请先检查，未覆盖任何文件。')
    if original.is_symlink() or (original.exists() and (original.stat().st_uid != 0 or original.stat().st_mode & 0o022)):
        raise RuntimeError('原状态目录权限不匹配，保留现场。')
    if not original.is_dir():
        raise RuntimeError('请先在 App 中成功启动过一次中继，以便保留恢复记录。')
    trusted(SUPPORT.parent)
    if original.stat().st_dev != SUPPORT.parent.stat().st_dev:
        raise RuntimeError('状态目录跨文件系统，未自动迁移。')
    daemon, login = jobs(user, app)
    SUPPORT.mkdir(mode=0o755)
    migrated = False
    old_was_running = False
    relay = None
    try:
        # This protected copy is independent of the user's editable App/source files.
        shutil.copytree(runtime, SUPPORT / 'python', symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copyfile(SOURCE / 'autostart.py', SUPPORT / 'autostart.py')
        shutil.copyfile(resources / 'relay.py', SUPPORT / 'relay.py')
        (SUPPORT / 'operator.json').write_text(json.dumps(user, ensure_ascii=False) + '\n')
        seal_tree(SUPPORT)
        trusted(SUPPORT / 'autostart.py')
        trusted(SUPPORT / 'relay.py')
        subprocess.run([str(SUPPORT / 'python/bin/python3'), '-I', '-B', '-c',
                        'import ssl,asyncio; print("系统副本运行环境检查通过")'], check=True)
        relay = load_relay(SUPPORT)
        relay.STATE = original
        old_was_running = relay.control_request('status') is not None
        relay.stop()
        migrate_state(original, SUPPORT / 'relay-state')
        migrated = True
        relay.STATE = SUPPORT / 'relay-state'
        if not agent.parent.exists():
            agent.parent.mkdir(mode=0o755)
            os.chown(agent.parent, user['uid'], user['gid'])
        write_plist(DAEMON_PLIST, daemon)
        write_plist(agent, login, user['uid'], user['gid'])
        subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(DAEMON_PLIST)], check=True)
        subprocess.run(['/bin/launchctl', 'bootstrap', 'gui/' + str(user['uid']), str(agent)], check=True)
        record = {'app': str(app), 'original_state': str(original),
                  'protected_state': str(SUPPORT / 'relay-state'), 'installed_at': time.time()}
        (SUPPORT / 'installation.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        print('已安装：登录后打开助手，中继等待登录与现有系统代理就绪后启动。', flush=True)
        print('原恢复记录完整迁移，App 的启动、停止和恢复按钮继续使用同一状态。', flush=True)
    except BaseException:
        for target in ['gui/' + str(user['uid']) + '/' + AGENT_LABEL, 'system/' + DAEMON_LABEL]:
            subprocess.run(['/bin/launchctl', 'bootout', target], capture_output=True)
        if relay and migrated:
            relay.STATE = SUPPORT / 'relay-state'
            with contextlib.suppress(Exception):
                relay.stop()
            if original.is_symlink() and original.resolve() == relay.STATE:
                original.unlink()
            if not original.exists():
                relay.STATE.rename(original)
            relay.STATE = original
        for path in [DAEMON_PLIST, agent]:
            if path.exists() and not path.is_symlink():
                path.rename(SUPPORT / (path.name + '.failed'))
        if relay and old_was_running:
            with contextlib.suppress(Exception):
                relay.start('Wi-Fi')
        print('安装未完成；已尝试回退状态路径并恢复原中继。系统副本保留供检查。', file=sys.stderr)
        raise


def disable():
    require_root()
    trusted(SUPPORT / 'operator.json')
    user = json.loads((SUPPORT / 'operator.json').read_text())
    for target in ['gui/' + str(user['uid']) + '/' + AGENT_LABEL, 'system/' + DAEMON_LABEL]:
        subprocess.run(['/bin/launchctl', 'bootout', target], capture_output=True)
    relay = load_relay(SUPPORT)
    relay.STATE = SUPPORT / 'relay-state'
    relay.stop()
    for path in [DAEMON_PLIST, agent_path(user)]:
        if path.exists():
            data = plistlib.loads(path.read_bytes())
            if data.get('Label') not in (DAEMON_LABEL, AGENT_LABEL):
                raise RuntimeError('启动项内容已变化，未移动。')
            destination = SUPPORT / (path.name + '.disabled-' + str(time.time_ns()))
            path.rename(destination)
    print('已关闭开机启动并停止中继；程序和全部恢复记录保留，App 仍可手动启动。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'install', 'run', 'disable'])
    parser.add_argument('--app', type=Path)
    args = parser.parse_args()
    if args.command in ('plan', 'install') and args.app is None:
        parser.error('需要 --app 指向已安装的云连接助手 App')
    if args.command == 'plan':
        user = operator()
        daemon, agent = jobs(user, args.app.resolve())
        print(json.dumps({'system_copy': str(SUPPORT), 'daemon': str(DAEMON_PLIST),
                          'login_item': str(agent_path(user)), 'state_from': str(old_state(user)),
                          'state_to': str(SUPPORT / 'relay-state'), 'jobs': [daemon, agent]}, ensure_ascii=False, indent=2))
    elif args.command == 'install':
        install(args.app)
    elif args.command == 'disable':
        disable()
    else:
        return run()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print('未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1)
