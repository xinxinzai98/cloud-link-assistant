"""Wait for the existing proxy and both relays before opening login apps.

Runs as the logged-in user. Does not restart services or alter network settings.
"""
import argparse
import http.client
import importlib.util
import json
from pathlib import Path
import plistlib
import socket
import ssl
import subprocess
import time
import urllib.request

SUPPORT = Path('/Library/Application Support/CloudLinkAssistant')
CODEX = Path('/Applications/ChatGPT.app')
CLASH = Path('/Applications/Clash Verge.app')
MODEL_URL = 'http://127.0.0.1:19847/healthz'


def report(event, **values):
    print(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                      'event': event, **values}), flush=True)


def load_relay():
    source = SUPPORT / 'relay.py'
    if not source.is_file():
        source = Path(__file__).with_name('relay.py')
    spec = importlib.util.spec_from_file_location('cloudlink_relay', source)
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    relay.STATE = Path.home() / 'Library/Application Support/云连接助手/relay-state'
    return relay


class NotReady(RuntimeError):
    pass


def readiness(relay):
    stage = 'system_proxy'
    try:
        proxy = relay.system_proxy()
        stage = 'proxy_port'
        with socket.create_connection(proxy, timeout=3):
            pass
        stage = 'cloud_relay'
        live = relay.control_request('status')
        state = relay.read_state() or {}
        if not live or not live.get('running') or state.get('phase') != 'running':
            raise ValueError('cloud relay pending')
        stage = 'cloud_mapping'
        if relay.MAPPING not in relay.HOSTS.read_bytes():
            raise ValueError('cloud mapping pending')
        stage = 'cloud_proxy_exception'
        service = (state.get('proxy_bypass') or {}).get('service', 'Wi-Fi')
        if relay.TARGET.lower() not in [s.lower() for s in relay.read_bypass(service)]:
            raise ValueError('cloud proxy exception pending')
        stage = 'model_gateway'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(MODEL_URL, timeout=3) as response:
            if response.status != 200 or not json.loads(response.read(8192)).get('running'):
                raise ValueError('model gateway pending')
        stage = 'cloud_tls'
        with socket.create_connection(('127.0.0.1', 443), timeout=3) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=relay.TARGET):
                pass
        stage = 'model_tls'
        connection = http.client.HTTPSConnection(*proxy, timeout=3,
                                                 context=ssl.create_default_context())
        connection.set_tunnel('chatgpt.com', 443)
        try:
            connection.connect()
        finally:
            connection.close()
        return proxy
    except Exception as error:
        raise NotReady(stage) from error


def wait_ready(relay, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        try:
            proxy = readiness(relay)
            report('ready')
            return proxy
        except NotReady as error:
            stage = str(error)
            if stage != previous:
                report('waiting', stage=stage)
                previous = stage
            time.sleep(2)
    report('timeout', stage=previous)
    return None


def application_running(app, bundle_id):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != bundle_id:
        raise RuntimeError('Unexpected application identity')
    executable = str(app / 'Contents/MacOS' / info['CFBundleExecutable'])
    output = subprocess.check_output(['/bin/ps', '-axo', 'comm='], text=True)
    return executable in [line.strip() for line in output.splitlines()]


def open_if_needed(app, bundle_id):
    if application_running(app, bundle_id):
        report('already_running', application=bundle_id)
        return
    subprocess.run(['/usr/bin/open', '-g', '-a', str(app)], check=True, timeout=15)
    report('open_requested', application=bundle_id)


def launch_ready_apps(helper):
    open_if_needed(helper, 'local.hive.cloudlinkassistant')
    if application_running(CODEX, 'com.openai.codex'):
        report('already_running', application='com.openai.codex')
        return
    # Reuse the established proxy launcher, including its NO_PROXY exceptions.
    launcher = Path(__file__).with_name('通过系统代理启动Codex.command')
    subprocess.run(['/bin/zsh', str(launcher)], check=True, timeout=20)
    report('open_requested', application='com.openai.codex')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper-app', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='Check only; never open apps')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    relay = load_relay()
    if args.check:
        readiness(relay)
        report('ready', check_only=True)
        return
    if CLASH.is_dir():
        open_if_needed(CLASH, 'io.github.clash-verge-rev.clash-verge-rev')
    if wait_ready(relay, args.timeout) is not None:
        launch_ready_apps(args.helper_app)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not serialize exception payloads, paths, HTTP headers, or credentials.
        report('failed', error_type=type(error).__name__)
        raise SystemExit(1)
