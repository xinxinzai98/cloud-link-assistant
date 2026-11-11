"""Per-user loopback HTTP proxy settings and anonymous connection checks."""
import argparse
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit


def settings_path():
    home = Path.home()
    if sys.platform == 'darwin':
        if os.geteuid() == 0:
            import pwd
            uid = int(os.environ.get('SUDO_UID', '0'))
            if uid <= 0:
                raise RuntimeError('需要原登录用户才能读取代理设置。')
            home = Path(pwd.getpwuid(uid).pw_dir)
        return home / 'Library/Application Support/云连接助手/proxy-settings.json'
    return Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / 'CloudLinkAssistant/proxy-settings.json'


def endpoint(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请填写本机 HTTP／混合代理地址，例如 127.0.0.1:7890。')
    parsed = urlsplit(value if '://' in value else 'http://' + value)
    host, port = parsed.hostname, parsed.port
    if parsed.scheme != 'http' or parsed.username is not None or parsed.password is not None:
        raise ValueError('请选择本机 HTTP／混合代理入口。')
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment or port is None or not 1 <= port <= 65535:
        raise ValueError('代理地址需要有效端口，且不能包含路径或查询参数。')
    if host != 'localhost':
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError('代理入口必须是本机回环地址。')
    return host, port


def proxy_url(proxy):
    host, port = proxy
    return f'http://[{host}]:{port}' if ':' in host else f'http://{host}:{port}'


def read_settings():
    path = settings_path()
    if not path.exists():
        return {'mode': 'auto'}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('mode') not in ('auto', 'manual'):
        raise ValueError('代理设置文件无效，请重新选择自动或手动模式。')
    if data['mode'] == 'manual':
        endpoint(data.get('url'))
    return data


def save_settings(value):
    data = {'mode': 'auto'} if value == 'auto' else {'mode': 'manual', 'url': proxy_url(endpoint(value))}
    path = settings_path()
    before = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.proxy-settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write('\n')
        if (path.read_bytes() if path.exists() else None) != before:
            raise RuntimeError('代理设置刚被其他程序修改，请重新打开设置。')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return data


def system_proxy():
    if sys.platform == 'darwin':
        try:
            output = subprocess.check_output(['/usr/sbin/scutil', '--proxy'], text=True, timeout=5)
        except subprocess.SubprocessError as error:
            raise RuntimeError('系统代理读取未完成，请在代理设置中手动填写入口。') from error
        values = dict(re.findall(r'^\s*(\w+)\s*:\s*(.*?)\s*$', output, re.M))
        for prefix in ('HTTPS', 'HTTP'):
            if values.get(prefix + 'Enable') == '1':
                host, port = values.get(prefix + 'Proxy', ''), values.get(prefix + 'Port', '')
                authority = f'[{host}]' if ':' in host else host
                return endpoint(f'http://{authority}:{port}')
    elif sys.platform == 'win32':
        values = urllib.request.getproxies_registry()
        for protocol in ('https', 'http'):
            if values.get(protocol):
                return endpoint(values[protocol])
    raise RuntimeError('未发现启用的本机 HTTP 代理，请在“代理设置”中手动填写 HTTP／混合端口。')


def resolve():
    data = read_settings()
    return endpoint(data['url']) if data['mode'] == 'manual' else system_proxy()


def active_network_service():
    try:
        route = subprocess.check_output(['/sbin/route', '-n', 'get', 'default'], text=True,
                                        stderr=subprocess.DEVNULL, timeout=5)
    except subprocess.CalledProcessError:
        route = subprocess.check_output(['/sbin/route', '-n', 'get', '-inet6', 'default'], text=True, timeout=5)
    found = re.search(r'^\s*interface:\s*(\S+)\s*$', route, re.M)
    if not found:
        raise RuntimeError('无法确认当前默认网络接口，请检查网络连接后重试。')
    order = subprocess.check_output(['/usr/sbin/networksetup', '-listnetworkserviceorder'], text=True, timeout=5)
    service = None
    matches = []
    for line in order.splitlines():
        name = re.match(r'^\((\d+|\*)\) (.+)$', line)
        if name:
            service = name.group(2) if name.group(1) != '*' else None
        device = re.search(r'Device:\s*([^,)]+)', line)
        if device and service and device.group(1).strip() == found.group(1) and not service.startswith('*'):
            matches.append(service)
    if len(matches) != 1:
        raise RuntimeError('默认网络接口无法对应到唯一网络服务，请检查系统网络服务顺序。')
    return matches[0]


def check_target(proxy, host, path):
    connection = http.client.HTTPSConnection(*proxy, timeout=6, context=ssl.create_default_context())
    connection.set_tunnel(host, 443)
    try:
        connection.request('GET', path, headers={'Host': host, 'Connection': 'close'})
        response = connection.getresponse()
        return status_result(response.status)
    finally:
        connection.close()


def status_result(status):
    if status == 401:
        return True, '已到达官方登录校验（HTTP 401）'
    if status in (200, 204):
        return True, f'服务可达（HTTP {status}）'
    return False, f'官方入口返回 HTTP {status}，请检查代理节点及分流规则。'


def check_cloud(proxy):
    host = 'codex-cloud-backend.chatgpt.com'
    port = 443 if sys.platform == 'darwin' else 18443
    try:
        raw = socket.create_connection(('127.0.0.1', port), timeout=3)
    except ConnectionRefusedError:
        ok, detail = check_target(proxy, host, '/')
        return ok, '经所选代理，' + detail + '；云中继尚未启动。'
    with raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(('GET / HTTP/1.1\r\nHost: ' + host + '\r\nConnection: close\r\n\r\n').encode('ascii'))
            response = http.client.HTTPResponse(tls)
            try:
                response.begin()
                ok, detail = status_result(response.status)
                return ok, '经当前云中继，' + detail
            finally:
                response.close()


def error_message(error):
    if isinstance(error, ssl.SSLCertVerificationError):
        return 'TLS 证书校验失败，请检查代理的证书或 HTTPS 解密设置。'
    if isinstance(error, ssl.SSLError):
        return 'TLS 连接中断，请检查代理节点。'
    if isinstance(error, ConnectionRefusedError):
        return '代理端口未监听，请启动代理或修正端口。'
    if isinstance(error, TimeoutError):
        return '连接超时，请检查代理节点。'
    tunnel_status = re.match(r'Tunnel connection failed: (\d{3})', str(error))
    if tunnel_status:
        return '代理拒绝 HTTP CONNECT（HTTP ' + tunnel_status.group(1) + '），请检查 HTTP／混合入口。'
    if isinstance(error, (ValueError, RuntimeError)):
        return str(error)
    return '连接未完成（' + type(error).__name__ + '），请检查代理入口及 HTTP CONNECT 支持。'


def diagnostics():
    try:
        proxy = resolve()
        with socket.create_connection(proxy, timeout=3):
            pass
    except (OSError, RuntimeError, ValueError) as error:
        return [('代理入口', False, error_message(error)),
                ('模型服务', False, '等待代理入口就绪'), ('云端服务', False, '等待代理入口就绪')]
    results = [('代理入口', True, proxy_url(proxy))]
    for name, host, path in [('模型服务', 'chatgpt.com', '/backend-api/codex/models'),
                             ('云端服务', 'codex-cloud-backend.chatgpt.com', '/')]:
        try:
            ok, detail = check_cloud(proxy) if name == '云端服务' else check_target(proxy, host, path)
        except (OSError, http.client.HTTPException, RuntimeError, ValueError) as error:
            ok, detail = False, error_message(error)
        results.append((name, ok, detail))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('show', 'set', 'check', 'url'))
    parser.add_argument('value', nargs='?')
    args = parser.parse_args()
    if args.command == 'set':
        if args.value is None:
            parser.error('set 需要 auto 或本机 HTTP 代理地址')
        save_settings(args.value)
        print('代理设置已保存，云中继将在下次启动时使用新设置。')
    elif args.command == 'show':
        data = read_settings()
        print(data['mode'])
        try:
            print(proxy_url(resolve()))
        except (OSError, RuntimeError, ValueError) as error:
            print('未识别代理')
            print(error_message(error))
    elif args.command == 'url':
        print(proxy_url(resolve()))
    else:
        for name, ok, detail in diagnostics():
            print(('✓ ' if ok else '× ') + name + '  ' + detail)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(error_message(error), file=sys.stderr)
        raise SystemExit(2)
