#!/usr/bin/env python3
"""macOS: forward one fixed cloud TLS destination through the existing HTTP proxy."""

import argparse
import asyncio
import contextlib
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import uuid

TARGET = "codex-cloud-backend.chatgpt.com"
LISTEN = ("127.0.0.1", 443)
HOSTS = Path("/private/etc/hosts")
MARKER = b"# codex-cloud-relay:v1"
MAPPING = b"127.0.0.1 " + TARGET.encode() + b" " + MARKER + b"\n"
STATE = Path(__file__).resolve().parent / ".state"
HEADER_LIMIT = 16384
DNS_HOST = "cloudflare-dns.com"
DNS_PATH = "/dns-query?name=" + TARGET + "&type=A"


def add_mapping(before):
    for line in before.splitlines():
        if TARGET.encode() in line.split(b"#", 1)[0].split()[1:]:
            raise ValueError("hosts 已有这个云域名的记录，拒绝覆盖；请先检查或运行 restore。")
    return before + (b"\n" if before and not before.endswith(b"\n") else b"") + MAPPING


def remove_mapping(current, before):
    expected = add_mapping(before)
    if current == expected:
        return before
    # Preserve unrelated edits made while the relay was running.
    owned = [line for line in current.splitlines(keepends=True) if line.rstrip(b"\r\n") == MAPPING.rstrip(b"\n")]
    if len(owned) > 1:
        raise ValueError("发现重复的中继映射，保留现场并拒绝自动修改。")
    return b"".join(line for line in current.splitlines(keepends=True) if line not in owned)


def atomic_write(path, data, expected=None, mode=0o644):
    """Preserve the existing file's ownership/mode; do not overwrite a concurrent edit."""
    path = Path(path)
    original_stat = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(original_stat.st_mode) if original_stat else mode)
        if original_stat and os.geteuid() == 0:
            os.fchown(fd, original_stat.st_uid, original_stat.st_gid)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None and path.read_bytes() != expected:
            raise RuntimeError("文件刚被其他程序修改，已取消写入。")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def save_state(data):
    atomic_write(STATE / "state.json", (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def read_state():
    try:
        return json.loads((STATE / "state.json").read_text())
    except FileNotFoundError:
        return None


def system_proxy():
    output = subprocess.check_output(["/usr/sbin/scutil", "--proxy"], text=True)
    values = dict(re.findall(r"^\s*(\w+)\s*:\s*(.*?)\s*$", output, re.M))
    if values.get("HTTPSEnable") != "1":
        raise RuntimeError("系统 HTTPS 代理未启用；本工具不回退到直连。")
    host, port = values.get("HTTPSProxy"), int(values.get("HTTPSPort", "0"))
    if host not in ("127.0.0.1", "localhost", "::1") or not 1 <= port <= 65535:
        raise RuntimeError("本工具只复用回环地址上的系统 HTTPS 代理。")
    return host, port


def read_bypass(service):
    result = subprocess.run(["/usr/sbin/networksetup", "-getproxybypassdomains", service],
                            check=True, capture_output=True, text=True)
    output = result.stdout.strip()
    if "AuthorizationCreate() failed" in output or output.startswith("** Error"):
        raise RuntimeError("无法读取所选网络服务的代理例外。")
    return [] if not output or output.startswith("There aren't any") else output.splitlines()


def set_bypass(service, domains):
    subprocess.run(["/usr/sbin/networksetup", "-setproxybypassdomains", service, *(domains or ["Empty"])],
                   check=True, capture_output=True, text=True)
    if read_bypass(service) != domains:
        raise RuntimeError("系统代理例外写入后回读不一致。")


def bypass_plan(service):
    if not service:
        return None
    before = read_bypass(service)
    return {"service": service, "before": before, "added": TARGET.lower() not in [x.lower() for x in before]}


def apply_bypass(plan):
    if not plan or not plan["added"]:
        return
    current = read_bypass(plan["service"])
    if TARGET.lower() in [item.lower() for item in current]:
        plan["added"] = False
        return
    set_bypass(plan["service"], current + [TARGET])


def restore_bypass(plan):
    if plan and plan["added"]:
        current = read_bypass(plan["service"])
        restored = [item for item in current if item.lower() != TARGET.lower()]
        if restored != current:
            set_bypass(plan["service"], restored)


def restore_changes(data, before=None):
    errors = []
    try:
        restore_bypass(data.get("proxy_bypass"))
    except Exception as error:
        errors.append("代理例外：" + str(error))
    try:
        current = HOSTS.read_bytes()
        if MARKER in current:
            if before is None:
                name = data.get("backup", "")
                if not re.fullmatch(r"hosts-before-[a-f0-9]{32}\.bin", name):
                    raise RuntimeError("缺少有效的 hosts 恢复记录。")
                backup = STATE / name
                if backup.is_symlink() or backup.stat().st_uid != 0 or backup.stat().st_mode & 0o022:
                    raise RuntimeError("hosts 恢复记录权限异常。")
                before = backup.read_bytes()
            atomic_write(HOSTS, remove_mapping(current, before), expected=current)
    except Exception as error:
        errors.append("hosts：" + str(error))
    if errors:
        raise RuntimeError("；".join(errors))


def parse_addresses(data):
    if data.get("Status") != 0:
        raise RuntimeError("经代理的 DNS 查询失败；不使用本机 hosts 解析上游。")
    answers = [item for item in data.get("Answer", []) if item.get("type") == 1]
    addresses = []
    for item in answers:
        address = ipaddress.ip_address(item["data"])
        if not isinstance(address, ipaddress.IPv4Address) or not address.is_global or address.is_multicast:
            raise RuntimeError("上游 DNS 返回非公网地址，拒绝转发。")
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise RuntimeError("上游 DNS 没有返回公网 IPv4 地址。")
    ttl = max(30, min(300, min(int(item.get("TTL", 60)) for item in answers)))
    return {"addresses": addresses, "ttl": ttl}


def resolve_target(proxy):
    # Clash may itself honor /etc/hosts. CONNECT to a validated address obtained via
    # proxy-backed DoH, while leaving the application's original TLS/SNI untouched.
    # Do not use urllib's implicit macOS proxy lookup from a background process.
    connection = http.client.HTTPSConnection(*proxy, timeout=8, context=ssl.create_default_context())
    connection.set_tunnel(DNS_HOST, 443)
    try:
        connection.request("GET", DNS_PATH, headers={"Accept": "application/dns-json"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("加密 DNS 服务未正常返回。")
        raw = response.read(65537)
    finally:
        connection.close()
    if len(raw) > 65536:
        raise RuntimeError("上游 DNS 响应过长。")
    return parse_addresses(json.loads(raw))


def connect_header(destination):
    return f"CONNECT {destination}:443 HTTP/1.1\r\nHost: {TARGET}:443\r\n\r\n".encode()


def check_proxy(proxy, destination):
    """Anonymous transport check with normal certificate and hostname verification."""
    with socket.create_connection(proxy, timeout=8) as upstream:
        upstream.sendall(connect_header(destination))
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            chunk = upstream.recv(1)
            if not chunk or len(header) >= HEADER_LIMIT:
                raise RuntimeError("代理响应头无效或过长。")
            header.extend(chunk)
        if not re.match(rb"HTTP/1\.[01] 200(?: |\r)", header):
            raise RuntimeError("现有代理未接受固定云地址的 CONNECT 请求。")
        with ssl.create_default_context().wrap_socket(upstream, server_hostname=TARGET):
            pass


async def open_tunnel(proxy, destination=TARGET):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(*proxy, limit=HEADER_LIMIT), 8)
    try:
        writer.write(connect_header(destination))
        await writer.drain()
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 8)
        if len(header) > HEADER_LIMIT or not re.match(rb"HTTP/1\.[01] 200(?: |\r)", header):
            raise RuntimeError("上游代理拒绝 CONNECT。")
        return reader, writer
    except BaseException:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        raise


async def pump(reader, writer, metrics=None, read_side=None, write_side=None):
    while True:
        try:
            chunk = await reader.read(65536)
        except OSError as error:
            error.relay_side, error.relay_action = read_side, "read"
            raise
        if not chunk:
            break
        try:
            writer.write(chunk)
            await writer.drain()
        except OSError as error:
            error.relay_side, error.relay_action = write_side, "write"
            raise
        if metrics is not None:
            metrics[read_side + "_bytes"] += len(chunk)
    if writer.can_write_eof():
        with contextlib.suppress(OSError):
            writer.write_eof()


async def forward(reader, writer, proxy, destinations=(TARGET,), metrics=None):
    upstream = None
    tasks = []
    try:
        last_error = None
        for destination in destinations:
            try:
                source, upstream = await open_tunnel(proxy, destination)
                break
            except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, RuntimeError) as error:
                error.relay_side, error.relay_action = "upstream", "connect"
                last_error = error
        if upstream is None:
            raise last_error or RuntimeError("没有可用的上游地址。")
        tasks = [asyncio.create_task(pump(reader, upstream, metrics, "client", "upstream")),
                 asyncio.create_task(pump(source, writer, metrics, "upstream", "client"))]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        # A client FIN may precede a long response. An upstream FIN means no more
        # response bytes can arrive; release the slot without waiting for the client.
        if not tasks[1].done():
            await tasks[1]
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for stream in (writer, upstream):
            if stream:
                stream.close()
                with contextlib.suppress(Exception):
                    await stream.wait_closed()


async def serve(listener, proxy, control_path, ready_fd, target_info=None):
    stopping = asyncio.Event()
    connections = set()
    target_info = target_info or {"addresses": [TARGET], "ttl": 300}
    stats = {"accepted": 0, "rejected": 0, "errors": 0, "last_error": None,
             "last_error_info": None, "reset_sides": {"client": 0, "upstream": 0},
             "dns_refreshes": 0, "last_dns_refresh_at": None}
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(signum, stopping.set)

    async def accept(reader, writer):
        if len(connections) >= 32:
            stats["rejected"] += 1
            writer.close()
            return
        task = asyncio.current_task()
        connections.add(task)
        stats["accepted"] += 1
        connection_id = stats["accepted"]
        started = time.monotonic()
        metrics = {"client_bytes": 0, "upstream_bytes": 0}
        try:
            await forward(reader, writer, proxy, tuple(target_info["addresses"]), metrics)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, RuntimeError) as error:
            stats["errors"] += 1
            stats["last_error"] = type(error).__name__
            side = getattr(error, "relay_side", None)
            if isinstance(error, ConnectionResetError) and side in stats["reset_sides"]:
                stats["reset_sides"][side] += 1
            info = {"at": time.time(), "connection_id": connection_id, "type": type(error).__name__,
                    "side": side, "action": getattr(error, "relay_action", None),
                    "errno": getattr(error, "errno", None),
                    "duration_seconds": round(time.monotonic() - started, 3), **metrics}
            stats["last_error_info"] = info
            print(json.dumps({"event": "connection_error", **info}), flush=True)
        finally:
            connections.discard(task)

    async def control(reader, writer):
        try:
            command = await asyncio.wait_for(reader.readline(), 2)
            if command == b"stop\n":
                stopping.set()
            result = {"running": True, "worker_pid": os.getpid(), "connections": len(connections), "target": TARGET,
                      "upstream_addresses": target_info["addresses"], **stats}
            writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()
        except (ConnectionError, TimeoutError):
            # Status clients may time out or close before reading the reply.
            pass
        finally:
            writer.close()

    async def refresh_dns():
        while True:
            await asyncio.sleep(target_info["ttl"])
            try:
                target_info.update(await asyncio.to_thread(resolve_target, proxy))
                stats["dns_refreshes"] += 1
                stats["last_dns_refresh_at"] = time.time()
            except Exception as error:
                stats["last_error"] = "DNS:" + type(error).__name__

    async with await asyncio.start_server(accept, sock=listener), await asyncio.start_unix_server(control, path=control_path, limit=64):
        os.chmod(control_path, 0o600)
        os.write(ready_fd, b"1")
        os.close(ready_fd)
        refresh = asyncio.create_task(refresh_dns())
        await stopping.wait()
        refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)
        for task in list(connections):
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)


def control_request(command):
    data = read_state()
    if not data or data.get("phase") != "running":
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(data["control_socket"])
            client.sendall(command.encode() + b"\n")
            with client.makefile("rb") as stream:
                response = stream.readline(4097)
            if len(response) > 4096 or not response.endswith(b"\n"):
                return None
            return json.loads(response)
    except (OSError, ValueError):
        return None


def require_admin():
    if os.geteuid() != 0:
        raise RuntimeError("启用/恢复需要管理员权限：请运行对应的 .command 文件。")
    STATE.mkdir(mode=0o755, exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid != 0 or STATE.stat().st_mode & 0o022:
        raise RuntimeError("运行状态目录的权限不安全，拒绝操作。")


def restore_stale():
    current = HOSTS.read_bytes()
    data = read_state()
    if not data:
        if MARKER in current:
            raise RuntimeError("存在中继映射但缺少恢复记录，未改动 hosts。")
        return
    if MARKER not in current and data.get("phase") == "stopped":
        return
    # The supervisor lock is held by the caller: a responsive worker here is an orphan.
    if control_request("stop") is not None:
        deadline = time.monotonic() + 5
        while control_request("status") is not None:
            if time.monotonic() >= deadline:
                raise RuntimeError("旧转发进程尚未停止，保留现有映射。")
            time.sleep(0.1)
    restore_changes(data)
    data["phase"] = "stopped"
    save_state(data)


def supervisor(listener, proxy, operator_uid, operator_gid, feedback_fd, lock_fd, target_info=None, proxy_bypass=None):
    """Only this small parent keeps root: forwarding happens after the child drops privileges."""
    before = HOSTS.read_bytes()
    mapped = add_mapping(before)
    run_id = uuid.uuid4().hex
    backup = "hosts-before-" + run_id + ".bin"
    atomic_write(STATE / backup, before, mode=0o600)
    temporary = Path(tempfile.mkdtemp(prefix="codex-relay-", dir="/private/tmp"))
    os.chown(temporary, operator_uid, operator_gid)
    control_path = str(temporary / "control.sock")
    data = {"phase": "starting", "version": 3, "target": TARGET, "proxy": list(proxy), "backup": backup,
            "control_socket": control_path, "supervisor_pid": os.getpid(), "started_at": time.time(),
            "upstream_addresses": target_info["addresses"] if target_info else [], "proxy_bypass": proxy_bypass}
    save_state(data)
    ready_read, ready_write = os.pipe()
    payload = {"listener_fd": listener.fileno(), "proxy": proxy, "control_path": control_path,
               "ready_fd": ready_write, "target_info": target_info, "uid": operator_uid}
    credentials = {"user": operator_uid, "group": operator_gid, "extra_groups": []} if os.geteuid() == 0 else {}
    worker = subprocess.Popen(internal_command("_worker", payload), pass_fds=(listener.fileno(), ready_write),
                              stdin=subprocess.DEVNULL, **credentials)
    child = worker.pid
    listener.close()
    os.close(ready_write)
    installed = False

    def stop_child(signum=None, *_):
        if signum is not None:
            data["stop_signal"] = signum
        if worker.poll() is None:
            worker.terminate()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop_child)
    try:
        if not select.select([ready_read], [], [], 5)[0] or os.read(ready_read, 1) != b"1":
            raise RuntimeError("转发进程未能启动；hosts 未改变。")
        atomic_write(HOSTS, mapped, expected=before)
        installed = True
        apply_bypass(proxy_bypass)
        data.update(phase="running", worker_pid=child)
        save_state(data)
        os.write(feedback_fd, b"OK\n")
        os.close(feedback_fd)
        feedback_fd = -1
        worker.wait()
    except BaseException as error:
        print("管理进程：" + str(error), flush=True)
        if feedback_fd >= 0:
            with contextlib.suppress(OSError):
                os.write(feedback_fd, ("ERROR " + str(error) + "\n").encode())
    finally:
        os.close(ready_read)
        stop_child()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
        data["worker_exit_code"] = worker.returncode
        data["finished_at"] = time.time()
        try:
            if installed:
                restore_changes(data, before)
            data["phase"] = "stopped"
        except Exception as error:
            data["phase"] = "needs_restore"
            print("恢复尚未完成：" + str(error), flush=True)
        save_state(data)
        shutil.rmtree(temporary)


def internal_command(mode, payload):
    return [sys.executable, '-B', str(Path(__file__).resolve()), mode, json.dumps({**payload, "state_dir": str(STATE)}, separators=(",", ":"))]


def start(proxy_bypass_service=None):
    require_admin()
    uid, gid = int(os.environ.get("SUDO_UID", "0")), int(os.environ.get("SUDO_GID", "0"))
    if uid == 0:
        raise RuntimeError("请从普通用户通过 sudo 启动，以便转发进程降权运行。")
    lock = open(STATE / "run.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("中继已启动或正在恢复，请先运行 status。")
    restore_stale()
    proxy = system_proxy()
    target_info = resolve_target(proxy)
    check_proxy(proxy, target_info["addresses"][0])
    proxy_bypass = bypass_plan(proxy_bypass_service)
    before = HOSTS.read_bytes()
    add_mapping(before)  # Refuse a pre-existing mapping before opening any listener.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(LISTEN)
    listener.listen(32)
    feedback_read, feedback_write = os.pipe()
    payload = {"listener_fd": listener.fileno(), "proxy": proxy, "operator_uid": uid, "operator_gid": gid,
               "feedback_fd": feedback_write, "lock_fd": lock.fileno(), "target_info": target_info,
               "proxy_bypass": proxy_bypass}
    log_fd = os.open(STATE / "relay.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(log_fd, "ab", buffering=0) as log:
        daemon = subprocess.Popen(internal_command("_supervisor", payload),
                                  pass_fds=(listener.fileno(), feedback_write, lock.fileno()),
                                  start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    listener.close()
    os.close(feedback_write)
    try:
        if not select.select([feedback_read], [], [], 15)[0]:
            daemon.terminate()
            raise RuntimeError("启动超时，已请求停止并恢复；请检查 status。")
        result = os.read(feedback_read, 4096).decode().strip()
        if result != "OK":
            raise RuntimeError(result or "启动失败；请查看 .state/relay.log。")
    finally:
        os.close(feedback_read)
    print(f"已启用：仅 {TARGET}:443 → 系统代理 {proxy[0]}:{proxy[1]}。")
    print("转发进程以普通用户运行；未开启 TUN。请等待桌面端自动重连。")
    if proxy_bypass:
        print(f"{proxy_bypass_service} 仅为这个云域名添加代理例外，使 HTTPS 和 WebSocket 都进入中继。")


def stop():
    response = control_request("stop")
    data = read_state() or {}
    # launchctl may already have asked the supervisor to stop. Its control
    # socket closes before settings restoration finishes; wait for that result.
    if response is None and data.get("phase") not in ("running", "starting"):
        if MARKER in HOSTS.read_bytes() or data.get("phase") == "needs_restore":
            raise RuntimeError("进程未响应但恢复尚未确认，请运行“恢复解析.command”。")
        print("中继未运行，hosts 无中继映射。")
        return
    for _ in range(150):
        data = read_state() or {}
        if data.get("phase") == "stopped" and MARKER not in HOSTS.read_bytes():
            print("已停止中继，并撤销自有 hosts 映射和代理例外。")
            return
        time.sleep(0.1)
    raise RuntimeError("停止/恢复尚未确认，请检查 status；必要时运行“恢复解析.command”。")


def main():
    global STATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "plan", "start", "restart", "stop", "status", "ui-status", "restore"])
    parser.add_argument("--proxy-bypass-service", help="仅在明确授权后，为指定网络服务增加这个云域名的代理例外")
    parser.add_argument("--state-dir", type=Path, help="App 专用状态目录")
    args = parser.parse_args()
    if args.state_dir is not None:
        STATE = args.state_dir.expanduser().resolve()
    command = args.command
    if command == "check":
        proxy = system_proxy()
        info = resolve_target(proxy)
        check_proxy(proxy, info["addresses"][0])
        print(f"系统代理 {proxy[0]}:{proxy[1]} 可用，目标 TLS 证书验证通过；未改变 hosts。")
    elif command == "plan":
        print(json.dumps({"target": TARGET, "proxy_bypass": bypass_plan(args.proxy_bypass_service)}, ensure_ascii=False, indent=2))
    elif command in ("start", "restart"):
        if command == "restart":
            require_admin()
            stop()
        start(args.proxy_bypass_service)
    elif command == "status":
        print(json.dumps({"live": control_request("status"), "state": read_state(),
                          "mapping_present": MARKER in HOSTS.read_bytes()}, ensure_ascii=False, indent=2))
    elif command == "ui-status":
        data = read_state() or {}
        live = control_request("status")
        if live:
            minutes = max(0, int((time.time() - data.get("started_at", time.time())) // 60))
            print("running\n运行中")
            print(f"已运行 {minutes} 分钟 · 当前转发连接 {live['connections']} 个")
            print(f"DNS 已刷新 {live.get('dns_refreshes', 0)} 次 · 连接错误 {live.get('errors', 0)} 次")
            recent = live.get("last_error") or "无"
            info = live.get("last_error_info")
            if info:
                side = {"client": "客户端", "upstream": "上游"}.get(info.get("side"), "未知端")
                action = {"read": "读取", "write": "写入", "connect": "连接"}.get(info.get("action"), "")
                recent += "（" + side + action + "，" + time.strftime("%m-%d %H:%M:%S", time.localtime(info["at"])) + "）"
            print("最近错误：" + recent)
        elif data.get("phase") == "needs_restore" or (data and MARKER in HOSTS.read_bytes()):
            print("needs_restore\n需要恢复设置\n中继进程无法联系，请刷新或使用“恢复设置”。")
        else:
            print("stopped\n已停止\n启动后将通过现有系统代理连接 Codex 云服务。\n停止时会撤销本工具添加的解析和代理例外。")
    elif command == "stop":
        stop()
    else:
        require_admin()
        with open(STATE / "run.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("管理进程仍在运行，请先 stop。")
            restore_stale()
        print("恢复检查完成，hosts 无中继映射。")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] in ("_worker", "_supervisor"):
            payload = json.loads(sys.argv[2])
            STATE = Path(payload.pop("state_dir", str(STATE)))
            listener = socket.socket(fileno=payload.pop("listener_fd"))
            if sys.argv[1] == "_worker":
                if os.geteuid() == 0 or os.geteuid() != payload.pop("uid"):
                    raise RuntimeError("转发进程必须以原普通用户运行。")
                asyncio.run(serve(listener, **payload))
            else:
                if os.geteuid() != 0:
                    raise RuntimeError("管理进程需要已授权的管理员上下文。")
                supervisor(listener, **payload)
        else:
            main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print("未完成：" + str(error), file=sys.stderr)
        sys.exit(1)
