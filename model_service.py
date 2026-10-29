"""User-level installation and recovery for the optional model HTTPS/SSE relay."""
import argparse
import copy
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import sys
import time
import tomllib

HOME = Path.home()
SOURCE = Path(__file__).resolve().parent
SUPPORT = HOME / 'Library/Application Support/云连接助手/model-https-compat'
CONFIG = HOME / '.codex/config.toml'
AGENTS = HOME / 'Library/LaunchAgents'
LABEL = 'local.hive.cloudlinkassistant.model-https'
LOGIN_LABEL = 'local.hive.cloudlinkassistant.login'
AGENT = AGENTS / (LABEL + '.plist')
LOGIN = AGENTS / (LOGIN_LABEL + '.plist')
SYSTEM = Path('/Library/Application Support/CloudLinkAssistant')
CLOUD_DAEMON = Path('/Library/LaunchDaemons/local.hive.cloudlinkassistant.autorelay.plist')
URL = 'http://127.0.0.1:19847'
FILES = ['https_compat.py', 'relay.py', 'login_ready.py', 'model_service.py',
         '通过系统代理启动Codex.command']


def relay_module():
    spec = importlib.util.spec_from_file_location('cloudlink_relay', SOURCE / 'relay.py')
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    return relay


def read_config():
    data = CONFIG.read_bytes() if CONFIG.exists() else b''
    return data, tomllib.loads(data.decode())


def config_change(before, enable):
    parsed = tomllib.loads(before.decode())
    current = parsed.get('openai_base_url')
    if enable:
        if parsed.get('model_provider', 'openai') != 'openai':
            raise RuntimeError('当前默认提供商不是官方 openai，请先确认自己的提供商设置。')
        if current not in [None, URL]:
            raise RuntimeError('已设置其他接口地址，保留原配置，不自动覆盖。')
        if current == URL:
            return before
    elif current != URL:
        return before
    text = before.decode()
    first_table = re.search(r'(?m)^\s*\[', text)
    cut = first_table.start() if first_table else len(text)
    top, rest = text[:cut], text[cut:]
    expected = copy.deepcopy(parsed)
    if enable:
        top = top.rstrip() + '\n\n# CloudLink model HTTPS/SSE compatibility.\nopenai_base_url = ' + json.dumps(URL) + '\n\n'
        expected['openai_base_url'] = URL
    else:
        top, count = re.subn(r'(?m)^[ \t]*openai_base_url[ \t]*=[^\r\n]*(?:\r?\n|$)', '', top)
        if count != 1:
            raise RuntimeError('接口地址格式有自定义改动，请手动检查；未覆盖配置。')
        expected.pop('openai_base_url')
    changed = (top + rest).encode()
    if tomllib.loads(changed.decode()) != expected:
        raise RuntimeError('配置校验未通过，未修改其他设置。')
    return changed


def health():
    connection = http.client.HTTPConnection('127.0.0.1', 19847, timeout=1)
    try:
        connection.request('GET', '/healthz')
        response = connection.getresponse()
        result = json.loads(response.read(16384)) if response.status == 200 else {}
        return result if result.get('running') is True else {}
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    finally:
        connection.close()


def command(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if check and result.returncode:
        raise RuntimeError('系统启动项操作未完成（代码 ' + str(result.returncode) + '）。')
    return result


def loaded(label):
    return command('/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}', check=False).returncode == 0


def owned_agent():
    if not AGENT.exists():
        return False
    value = plistlib.loads(AGENT.read_bytes())
    return value.get('Label') == LABEL and str(SUPPORT / 'https_compat.py') in value.get('ProgramArguments', [])


def atomic(path, payload, before=None, mode=0o600):
    if before is not None and path.read_bytes() != before:
        raise RuntimeError('文件被其他程序修改，未覆盖。')
    relay_module().atomic_write(path, payload, expected=before, mode=mode)


def backup(paths):
    directory = HOME / '.codex/backups/cloud-link-assistant' / str(time.time_ns())
    directory.mkdir(parents=True, mode=0o700)
    saved = {}
    for index, path in enumerate(paths):
        if path.is_file():
            data = path.read_bytes()
            target = directory / (str(index) + '-' + path.name)
            target.write_bytes(data)
            target.chmod(0o600)
            saved[str(path)] = str(target)
        else:
            saved[str(path)] = None
    (directory / 'index.json').write_text(json.dumps(saved, ensure_ascii=False, indent=2) + '\n')
    (directory / 'index.json').chmod(0o600)
    return directory, saved


def login_job(app, enabled):
    result = {'Label': LOGIN_LABEL, 'RunAtLoad': True, 'LimitLoadToSessionType': 'Aqua',
              'ProcessType': 'Interactive', 'ProgramArguments': ['/usr/bin/open', '-a', str(app)]}
    if enabled:
        runtime = (SYSTEM / 'python/bin/python3') if (SYSTEM / 'python/bin/python3').is_file() else Path(sys.executable)
        result.update(ProgramArguments=[str(runtime), '-I', '-B', str(SUPPORT / 'login_ready.py'),
                                        '--helper-app', str(app)],
                      StandardOutPath=str(SUPPORT / 'startup.log'),
                      StandardErrorPath=str(SUPPORT / 'startup.stderr.log'))
    return result


def configure_login(app, enabled):
    if enabled and not CLOUD_DAEMON.exists():
        return False
    was_gate = validate_login()
    if not enabled and not was_gate:
        return False
    command('/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{LOGIN_LABEL}', check=False)
    atomic(LOGIN, plistlib.dumps(login_job(app, enabled)))
    command('/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(LOGIN))
    return enabled


def validate_login():
    if LOGIN.exists():
        old = plistlib.loads(LOGIN.read_bytes())
        args = old.get('ProgramArguments', [])
        gates = [SUPPORT / 'login_ready.py',
                 HOME / 'Library/Application Support/云连接助手/login-ready/login_ready.py']
        is_gate = any(str(p) in args for p in gates)
        is_helper = len(args) == 3 and args[:2] == ['/usr/bin/open', '-a']
        if is_helper:
            info = Path(args[2]) / 'Contents/Info.plist'
            is_helper = info.is_file() and plistlib.loads(info.read_bytes()).get('CFBundleIdentifier') == 'local.hive.cloudlinkassistant'
        if old.get('Label') != LOGIN_LABEL or not (is_helper or is_gate):
            raise RuntimeError('助手登录项包含其他自定义内容，未覆盖。')
        return is_gate
    return False


def enable(app):
    if os.geteuid() == 0:
        raise RuntimeError('本机优化应由当前普通用户启用，不需要管理员权限。')
    app = Path(app).expanduser().resolve()
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'local.hive.cloudlinkassistant':
        raise RuntimeError('所选 App 不是云连接助手。')
    if any(path.is_symlink() for path in [SUPPORT, CONFIG, AGENT, LOGIN]):
        raise RuntimeError('配置或安装路径是自定义链接，请先检查；未替换链接。')
    if CLOUD_DAEMON.exists():
        validate_login()
    before, parsed = read_config()
    changed = config_change(before, True)
    was_loaded, live = loaded(LABEL), health()
    if AGENT.exists() and not owned_agent():
        raise RuntimeError('同名本机启动项不属于当前中继，未覆盖。')
    if (live or was_loaded) and (not owned_agent() or not (SUPPORT / 'install-state.json').is_file()):
        raise RuntimeError('本机端口或启动项已占用，未接管未知程序。')
    if was_loaded and not live:
        raise RuntimeError('现有本机中继没有响应，请结束聊天后先停止再启用。')
    if not live:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 19847))
    targets = [CONFIG, AGENT, LOGIN, SUPPORT / 'install-state.json', *[SUPPORT / n for n in FILES]]
    directory, saved = backup(targets)
    SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    SUPPORT.chmod(0o700)
    AGENTS.mkdir(parents=True, exist_ok=True)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    login_attempted = False
    written = {}
    login_was_loaded = loaded(LOGIN_LABEL)
    try:
        for name in FILES:
            payload = (SOURCE / name).read_bytes()
            atomic(SUPPORT / name, payload, mode=0o700)
            written[str(SUPPORT / name)] = payload
        for name in ['service.stdout.log', 'service.stderr.log', 'startup.log', 'startup.stderr.log']:
            (SUPPORT / name).touch(mode=0o600, exist_ok=True)
        runtime = (SYSTEM / 'python/bin/python3') if (SYSTEM / 'python/bin/python3').is_file() else Path(sys.executable)
        agent = {'Label': LABEL, 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                 'ProcessType': 'Background', 'WorkingDirectory': str(SUPPORT),
                 'ProgramArguments': [str(runtime), '-I', '-B', str(SUPPORT / 'https_compat.py'), '--port', '19847'],
                 'StandardOutPath': str(SUPPORT / 'service.stdout.log'),
                 'StandardErrorPath': str(SUPPORT / 'service.stderr.log')}
        agent_bytes = plistlib.dumps(agent)
        atomic(AGENT, agent_bytes)
        written[str(AGENT)] = agent_bytes
        if not was_loaded:
            command('/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(AGENT))
            command('/bin/launchctl', 'kickstart', f'gui/{os.getuid()}/{LABEL}')
        for _ in range(50):
            if health():
                break
            time.sleep(.2)
        else:
            raise RuntimeError('本机中继未能启动，已请求回退。')
        if changed != before:
            if CONFIG.exists():
                atomic(CONFIG, changed, before=before)
            elif CONFIG.exists() or before:
                raise RuntimeError('配置已被其他程序修改。')
            else:
                atomic(CONFIG, changed)
            written[str(CONFIG)] = changed
        state = {'format': 2, 'enabled': True, 'app': str(app), 'runtime': str(runtime),
                 'backup_directory': str(directory), 'source_destination': str(SUPPORT / 'https_compat.py'),
                 'updated_at': time.time(), 'source_sha256': hashlib.sha256((SOURCE / 'https_compat.py').read_bytes()).hexdigest()}
        state_bytes = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()
        atomic(SUPPORT / 'install-state.json', state_bytes)
        written[str(SUPPORT / 'install-state.json')] = state_bytes
        login_attempted = CLOUD_DAEMON.exists()
        configure_login(app, True)
    except BaseException:
        if not was_loaded:
            command('/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}', check=False)
        if login_attempted:
            command('/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{LOGIN_LABEL}', check=False)
        for path, original in saved.items():
            if path not in written and not (login_attempted and path == str(LOGIN)):
                continue
            path = Path(path)
            if str(path) in written and path.exists() and path.read_bytes() != written[str(path)]:
                continue  # Preserve an unrelated concurrent edit.
            if original:
                atomic(path, Path(original).read_bytes())
            elif path.exists():
                path.rename(directory / ('new-' + path.name))
        if login_attempted and login_was_loaded and LOGIN.exists():
            command('/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(LOGIN), check=False)
        raise
    print('本机 HTTPS/SSE 已启用，保留官方提供商、账号和模型。')
    if live:
        print('现有中继继续运行；新代码将在下次重启中继时加载。')
    print('配置备份已保存在用户私有目录；已有聊天可继续，重开 Codex 后统一生效。')


def restore(stop=False, app=None):
    before, parsed = read_config()
    changed = config_change(before, False)
    state_path = SUPPORT / 'install-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if stop and AGENT.exists() and not owned_agent():
        raise RuntimeError('启动项已被其他程序修改，未停止。')
    if changed != before:
        backup([CONFIG])
        atomic(CONFIG, changed, before=before)
    if state:
        state['enabled'] = False
        atomic(state_path, (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode())
    helper = app or state.get('app')
    if helper:
        configure_login(Path(helper), False)
    if stop:
        command('/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}', check=False)
        for _ in range(30):
            if not health():
                break
            time.sleep(.1)
        else:
            raise RuntimeError('接口配置已恢复，但本机中继停止尚未确认；请刷新状态。')
        if AGENT.exists():
            AGENT.rename(AGENT.with_name(AGENT.name + '.disabled-' + str(time.time_ns())))
        print('已恢复本工具添加的接口配置并停止本机中继。重新打开 Codex 后使用原连接方式。')
    else:
        print('已恢复本工具添加的接口配置；本机中继继续服务现有聊天。结束任务并重开 Codex 后使用原连接方式。')


def status():
    _, config = read_config()
    live = health()
    enabled = config.get('openai_base_url') == URL
    state_path = SUPPORT / 'install-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    expected = hashlib.sha256((SUPPORT / 'https_compat.py').read_bytes()).hexdigest() if (SUPPORT / 'https_compat.py').is_file() else None
    login = plistlib.loads(LOGIN.read_bytes()) if LOGIN.exists() else {}
    gates = [SUPPORT / 'login_ready.py', HOME / 'Library/Application Support/云连接助手/login-ready/login_ready.py']
    return {'configured': enabled, 'running': bool(live), 'pending_code_restart': state.get('format') == 2 and bool(live) and live.get('source_sha256') != expected,
            'ordered_login': enabled and any(str(p) in login.get('ProgramArguments', []) for p in gates),
            'owned_startup': owned_agent(), 'cloud_autostart': CLOUD_DAEMON.exists()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['enable', 'restore', 'stop', 'status', 'ui-status'])
    parser.add_argument('--app', type=Path)
    args = parser.parse_args()
    if args.command == 'enable':
        if args.app is None:
            parser.error('启用时需要 --app 指向已安装的助手 App')
        enable(args.app)
    elif args.command in ['restore', 'stop']:
        restore(args.command == 'stop', args.app)
    else:
        value = status()
        if args.command == 'status':
            print(json.dumps(value))
        else:
            title = '已启用' if value['configured'] and value['running'] else ('等待启动' if value['configured'] else ('后台运行' if value['running'] else '未启用'))
            print(('running' if value['running'] else 'stopped') + '\n' + title)
            print('仅将本机聊天转为 HTTPS/SSE，账号和模型保持原样。')
            print('登录顺序：' + ('就绪后启动 Codex' if value['ordered_login'] else '开启本机优化与云中继自启后接入'))
            if value['pending_code_restart']:
                print('新中继代码将在下次重启后加载。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1)
