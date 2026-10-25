import asyncio
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
SOURCE = Path(os.environ.get("RELAY_SOURCE", str(Path(__file__).resolve().parent / "relay.py")))
spec = importlib.util.spec_from_file_location("relay", SOURCE)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class HostsTests(unittest.TestCase):
    def test_exact_round_trip_without_final_newline(self):
        original = b"127.0.0.1 localhost\n# user note"
        self.assertEqual(relay.remove_mapping(relay.add_mapping(original), original), original)

    def test_preserves_concurrent_unrelated_changes(self):
        original = b"127.0.0.1 localhost\n"
        current = relay.add_mapping(original) + b"10.0.0.8 office.example\n"
        self.assertEqual(relay.remove_mapping(current, original), original + b"10.0.0.8 office.example\n")

    def test_refuses_existing_target_mapping(self):
        with self.assertRaises(ValueError):
            relay.add_mapping(b"1.2.3.4 alias " + relay.TARGET.encode() + b"\n")

    def test_ignores_comments_and_similar_hostnames(self):
        original = b"# " + relay.TARGET.encode() + b"\n1.2.3.4 sub." + relay.TARGET.encode() + b"\n"
        self.assertEqual(relay.remove_mapping(relay.add_mapping(original), original), original)

    def test_does_not_remove_modified_mapping(self):
        original = b"127.0.0.1 localhost\n"
        modified = relay.add_mapping(original).replace(b"# codex-cloud-relay:v1", b"# user-managed")
        self.assertEqual(relay.remove_mapping(modified, original), modified)

    def test_rejects_duplicate_owned_mappings(self):
        with self.assertRaises(ValueError):
            relay.remove_mapping(relay.MAPPING * 2, b"")

    def test_atomic_write_refuses_conflicting_edit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "hosts"
            path.write_bytes(b"new edit")
            with self.assertRaises(RuntimeError):
                relay.atomic_write(path, b"replacement", expected=b"old version")
            self.assertEqual(path.read_bytes(), b"new edit")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_upstream_dns_rejects_loopback_and_private_addresses(self):
        for address in ["127.0.0.1", "10.0.0.5", "198.18.0.1", "224.0.0.1"]:
            with self.subTest(address=address), self.assertRaises(RuntimeError):
                relay.parse_addresses({"Status": 0, "Answer": [{"type": 1, "data": address, "TTL": 60}]})

    def test_upstream_dns_requires_success_and_bounds_refresh(self):
        with self.assertRaises(RuntimeError):
            relay.parse_addresses({"Status": 3})
        info = relay.parse_addresses({"Status": 0, "Answer": [
            {"type": 5, "data": "alias.example", "TTL": 1},
            {"type": 1, "data": "104.18.32.47", "TTL": 10000},
        ]})
        self.assertEqual(info, {"addresses": ["104.18.32.47"], "ttl": 300})


class ControlTests(unittest.TestCase):
    def test_stop_waits_for_supervisor_already_shutting_down(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts = Path(directory) / 'hosts'
            hosts.write_bytes(b'127.0.0.1 localhost\n')
            phases = [{'phase': 'running'}, {'phase': 'running'}, {'phase': 'stopped'}]
            with mock.patch.object(relay, 'HOSTS', hosts), \
                    mock.patch.object(relay, 'control_request', return_value=None), \
                    mock.patch.object(relay, 'read_state', side_effect=phases), \
                    mock.patch.object(relay.time, 'sleep'):
                relay.stop()
            self.assertEqual(hosts.read_bytes(), b'127.0.0.1 localhost\n')

    def request_reply(self, chunks):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            path = str(Path(directory) / 'control.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(path)
                listener.listen()

                def respond():
                    with listener.accept()[0] as peer:
                        peer.recv(64)
                        for chunk in chunks:
                            with contextlib.suppress(BrokenPipeError):
                                peer.sendall(chunk)
                            time.sleep(0.02)

                worker = threading.Thread(target=respond, daemon=True)
                worker.start()
                try:
                    with mock.patch.object(relay, 'read_state', return_value={'phase': 'running', 'control_socket': path}):
                        return relay.control_request('status')
                finally:
                    worker.join(2)
                    self.assertFalse(worker.is_alive())

    def test_status_accepts_a_fragmented_reply(self):
        self.assertEqual(self.request_reply([b'{"running":', b'true}\n']), {'running': True})

    def test_status_rejects_oversized_or_unterminated_reply(self):
        for chunks in ([b'x' * 4097], [b'{"running":true}']):
            self.assertIsNone(self.request_reply(chunks))


class LifecycleTests(unittest.TestCase):
    def test_start_stop_and_concurrent_hosts_edit_in_isolated_fixture(self):
        # Exercises real forks, control socket and cleanup, using only a fake hosts file.
        # Privileged operations are simulated; this is not a real administrator/port-443 test.
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            fixture_hosts = root / "hosts"
            original = b"127.0.0.1 localhost\n# keep this\n"
            fixture_hosts.write_bytes(original)
            fixture_state = root / "state"
            fixture_state.mkdir()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            ready_read, ready_write = os.pipe()
            with mock.patch.object(relay, "HOSTS", fixture_hosts), mock.patch.object(relay, "STATE", fixture_state):
                with open(fixture_state / "run.lock", "a") as lock:
                    manager = os.fork()
                    if manager == 0:
                        os.close(ready_read)
                        try:
                            with mock.patch.object(relay.os, "setgroups"), mock.patch.object(relay.os, "chown"):
                                relay.supervisor(listener, ("127.0.0.1", 1), os.getuid(), os.getgid(), ready_write, lock.fileno())
                            os._exit(0)
                        except BaseException:
                            os._exit(2)
                    listener.close()
                    os.close(ready_write)
                    reaped = False
                    try:
                        self.assertTrue(select.select([ready_read], [], [], 8)[0])
                        self.assertEqual(os.read(ready_read, 4096), b"OK\n")
                        self.assertIsNotNone(relay.control_request("status"))
                        self.assertEqual(fixture_hosts.read_bytes(), relay.add_mapping(original))
                        fixture_hosts.write_bytes(fixture_hosts.read_bytes() + b"10.0.0.8 office.example\n")
                        self.assertIsNotNone(relay.control_request("stop"))
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline:
                            result, code = os.waitpid(manager, os.WNOHANG)
                            if result:
                                reaped = True
                                self.assertEqual(code, 0)
                                break
                            time.sleep(0.05)
                        self.assertTrue(reaped, "supervisor did not stop")
                        self.assertEqual(fixture_hosts.read_bytes(), original + b"10.0.0.8 office.example\n")
                        data = relay.read_state()
                        self.assertEqual(data["phase"], "stopped")
                        self.assertEqual((fixture_state / data["backup"]).read_bytes(), original)
                        self.assertFalse(Path(data["control_socket"]).exists())
                    finally:
                        os.close(ready_read)
                        if not reaped:
                            relay.control_request("stop")
                            with contextlib.suppress(ProcessLookupError):
                                os.kill(manager, signal.SIGTERM)
                            os.waitpid(manager, 0)


class BypassTests(unittest.TestCase):
    def test_adds_only_target_and_preserves_existing_exceptions(self):
        before = ["127.0.0.1", "10.0.0.0/8", "*.local"]
        with mock.patch.object(relay, "read_bypass", return_value=before), mock.patch.object(relay, "set_bypass") as write:
            plan = relay.bypass_plan("Wi-Fi")
            relay.apply_bypass(plan)
            write.assert_called_once_with("Wi-Fi", before + [relay.TARGET])

    def test_restore_preserves_unrelated_changes(self):
        current = ["127.0.0.1", relay.TARGET, "office.example"]
        with mock.patch.object(relay, "read_bypass", return_value=current), mock.patch.object(relay, "set_bypass") as write:
            relay.restore_bypass({"service": "Wi-Fi", "before": ["127.0.0.1"], "added": True})
            write.assert_called_once_with("Wi-Fi", ["127.0.0.1", "office.example"])

    def test_does_not_remove_a_preexisting_user_exception(self):
        with mock.patch.object(relay, "read_bypass", return_value=[relay.TARGET]), mock.patch.object(relay, "set_bypass") as write:
            plan = relay.bypass_plan("Wi-Fi")
            relay.apply_bypass(plan)
            relay.restore_bypass(plan)
            self.assertFalse(plan["added"])
            write.assert_not_called()


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        directory = Path(cls.fixture.name)
        cert, key = directory / "cert.pem", directory / "key.pem"
        subprocess.run([
            "/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=" + relay.TARGET,
            "-addext", "subjectAltName=DNS:" + relay.TARGET,
        ], check=True, capture_output=True)
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cert, key)
        cls.client_context = ssl.create_default_context(cafile=str(cert))

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    async def asyncSetUp(self):
        self.servers = []
        self.handlers = set()
        self.requests = []

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)

    async def server(self, handler, **options):
        async def track(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                self.handlers.discard(task)
        server = await asyncio.start_server(track, "127.0.0.1", 0, **options)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def test_tls_and_binary_payload_survive_without_decryption(self):
        async def echo(reader, writer):
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        target_port = await self.server(echo, ssl=self.server_context)

        async def proxy(reader, writer):
            self.requests.append(await reader.readuntil(b"\r\n\r\n"))
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", target_port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            try:
                await asyncio.gather(relay.pump(reader, upstream_writer), relay.pump(upstream_reader, writer))
            finally:
                upstream_writer.close()
        proxy_port = await self.server(proxy)
        # Literal upstream avoids a Clash/hosts feedback loop; TLS still checks TARGET.
        relay_port = await self.server(lambda r, w: relay.forward(r, w, ("127.0.0.1", proxy_port), ("104.18.32.47",)))
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port, ssl=self.client_context, server_hostname=relay.TARGET)
        payload = bytes(range(256)) * 4096
        writer.write(payload)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 5), payload)
        self.assertEqual(self.requests, [f"CONNECT 104.18.32.47:443 HTTP/1.1\r\nHost: {relay.TARGET}:443\r\n\r\n".encode()])
        writer.close()
        await writer.wait_closed()

    async def test_refuses_proxy_rejection(self):
        async def reject(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            await writer.drain()
        proxy_port = await self.server(reject)
        with self.assertRaises(RuntimeError):
            await relay.open_tunnel(("127.0.0.1", proxy_port))

    async def test_upstream_eof_releases_slot_while_client_is_still_open(self):
        finished = asyncio.Event()

        async def proxy(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\n\r\nfinished")
            await writer.drain()
            writer.write_eof()
            await reader.read()

        proxy_port = await self.server(proxy)

        async def forward(reader, writer):
            try:
                await relay.forward(reader, writer, ("127.0.0.1", proxy_port))
            finally:
                finished.set()

        port = await self.server(forward)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"finished")
            await asyncio.wait_for(finished.wait(), 0.5)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_client_half_close_keeps_delayed_response_intact(self):
        payload = bytes(range(256)) * 4096

        async def proxy(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await writer.drain()
            self.assertEqual(await reader.read(), b"request")
            await asyncio.sleep(0.03)
            writer.write(payload)
            await writer.drain()

        proxy_port = await self.server(proxy)
        port = await self.server(lambda r, w: relay.forward(r, w, ("127.0.0.1", proxy_port)))
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"request")
            await writer.drain()
            writer.write_eof()
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), payload)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_records_whether_reset_came_from_client_or_upstream(self):
        for side in ('client', 'upstream'):
            with self.subTest(side=side):
                trigger = asyncio.Event()
                finished = asyncio.Event()
                failures = []
                metrics = {'client_bytes': 0, 'upstream_bytes': 0}

                def reset(writer):
                    writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                                              struct.pack('ii', 1, 0))
                    writer.transport.abort()

                async def proxy(reader, writer):
                    await reader.readuntil(b'\r\n\r\n')
                    writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
                    await writer.drain()
                    writer.write(await reader.readexactly(4))
                    await writer.drain()
                    await trigger.wait()
                    if side == 'upstream':
                        reset(writer)
                    else:
                        with contextlib.suppress(ConnectionError):
                            await reader.read()

                proxy_port = await self.server(proxy)

                async def forward(reader, writer):
                    try:
                        await relay.forward(reader, writer, ('127.0.0.1', proxy_port), metrics=metrics)
                    except ConnectionResetError as error:
                        failures.append((error.relay_side, error.relay_action))
                    finally:
                        finished.set()

                port = await self.server(forward)
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                try:
                    writer.write(b'ping')
                    await writer.drain()
                    self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 1), b'ping')
                    trigger.set()
                    if side == 'client':
                        reset(writer)
                    await asyncio.wait_for(finished.wait(), 1)
                    self.assertEqual(len(failures), 1)
                    self.assertEqual(failures[0][0], side)
                    self.assertEqual(metrics, {'client_bytes': 4, 'upstream_bytes': 4})
                finally:
                    writer.close()
                    with contextlib.suppress(ConnectionError):
                        await writer.wait_closed()

    async def test_bounds_proxy_response_header(self):
        async def oversized(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nX: " + b"a" * (relay.HEADER_LIMIT + 100) + b"\r\n\r\n")
            await writer.drain()
        proxy_port = await self.server(oversized)
        with self.assertRaises((asyncio.LimitOverrunError, RuntimeError)):
            await relay.open_tunnel(("127.0.0.1", proxy_port))

    async def test_client_still_checks_tls_hostname(self):
        async def hold(reader, writer):
            await reader.read()
        port = await self.server(hold, ssl=self.server_context)
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        reset_seen = asyncio.Event()
        def expected_reset(loop, context):
            error = context.get("exception")
            if error is ConnectionResetError or isinstance(error, ConnectionResetError):
                reset_seen.set()
            elif previous:
                previous(loop, context)
            else:
                loop.default_exception_handler(context)
        loop.set_exception_handler(expected_reset)
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                await asyncio.open_connection("127.0.0.1", port, ssl=self.client_context, server_hostname="wrong.invalid")
            await asyncio.wait_for(reset_seen.wait(), 1)
        finally:
            loop.set_exception_handler(previous)


if __name__ == "__main__":
    unittest.main(verbosity=2)
