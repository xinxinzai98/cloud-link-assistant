#!/usr/bin/env python3
"""Official Antigravity entry points and anonymous proxy connectivity checks."""
import argparse
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import ssl
import subprocess

APP = Path('/Applications/Antigravity.app')
ENDPOINTS = [('Antigravity', 'antigravity.google'), ('Google 登录', 'accounts.google.com'), ('Google OAuth', 'oauth2.googleapis.com')]


def proxy_settings():
    text = subprocess.check_output(['/usr/sbin/scutil', '--proxy'], text=True)
    values = dict(re.findall(r'^\s*(\w+)\s*:\s*(.*?)\s*$', text, re.M))
    host, port = values.get('HTTPSProxy'), int(values.get('HTTPSPort', '0'))
    if values.get('HTTPSEnable') != '1' or host not in ['127.0.0.1', 'localhost', '::1'] or not 1 <= port <= 65535:
        raise RuntimeError('需要开启本机回环地址的系统 HTTPS 代理。')
    return host, port


def installed_version():
    path = APP / 'Contents/Info.plist'
    return plistlib.loads(path.read_bytes()).get('CFBundleShortVersionString', '未知') if path.exists() else None


def check_one(name, hostname, proxy):
    connection = http.client.HTTPSConnection(*proxy, timeout=7, context=ssl.create_default_context())
    connection.set_tunnel(hostname, 443)
    try:
        connection.request('HEAD', '/', headers={'Connection': 'close'})
        response = connection.getresponse()
        return f'{name}：TLS 校验通过，HTTP {response.status}'
    except Exception as error:
        return f'{name}：未连通（{type(error).__name__}）'
    finally:
        connection.close()


def launch():
    if not APP.exists():
        raise RuntimeError('本机未找到 Antigravity.app。')
    already_running = subprocess.run(['/usr/bin/pgrep', '-x', 'Antigravity'], capture_output=True).returncode == 0
    if already_running:
        subprocess.run(['/usr/bin/open', '-a', str(APP)], check=True)
        print('已打开现有 Antigravity 窗口。\n请在官方应用中选择 Sign in 完成 Google 授权。\n如需重新应用代理，请先保存工作并退出 Antigravity，再从此入口启动。')
        return
    host, port = proxy_settings()
    host = f'[{host}]' if ':' in host else host
    url = f'http://{host}:{port}'
    args = ['/usr/bin/open']
    for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
        args.extend(['--env', name + '=' + url])
    bypass = os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or 'localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12'
    for name in ['NO_PROXY', 'no_proxy']:
        args.extend(['--env', name + '=' + bypass])
    args += ['-a', str(APP), '--args', '--proxy-server=' + url]
    subprocess.run(args, check=True)
    print('已使用现有系统代理启动 Antigravity。\n请在官方应用中选择 Sign in，再在浏览器完成 Google 授权。\nGoogle 授权和登录令牌由官方应用处理。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['info', 'check', 'open'])
    command = parser.parse_args().command
    try:
        if command == 'info':
            version = installed_version()
            print(('已安装 Antigravity ' + version) if version else '本机未安装 Antigravity')
        elif command == 'check':
            proxy = proxy_settings()
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                for line in pool.map(lambda item: check_one(*item, proxy), ENDPOINTS):
                    print(line)
            print('\n上述检查仅确认代理和 TLS 连通性，不代表账号已登录。')
        else:
            launch()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('未完成：' + str(error))
        raise SystemExit(1)
