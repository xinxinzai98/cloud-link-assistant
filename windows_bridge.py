"""Run the released Cloud Link network workers on Windows loopback."""

import argparse
import asyncio
import contextlib
import http.client
import logging
import os
from pathlib import Path
import ssl
import sys

import https_compat
import relay
import proxy_config


def serve_model(port, proxy=None):
    def connect_official():
        connection = http.client.HTTPSConnection(
            *(proxy or proxy_config.resolve()), timeout=12, context=ssl.create_default_context()
        )
        connection.set_tunnel(https_compat.UPSTREAM_HOST, 443)
        return connection

    with https_compat.Gateway(port, connection_factory=connect_official) as server:
        logging.info("Model relay listening on loopback")
        server.serve_forever(poll_interval=0.2)


async def serve_cloud(port, proxy):
    hosts = Path(os.environ["SystemRoot"]) / "System32/drivers/etc/hosts"
    name = relay.TARGET.encode("ascii")
    if any(name in line.partition(b"#")[0].lower().split()[1:]
           for line in hosts.read_bytes().splitlines()):
        raise RuntimeError("Windows hostname relay requires an unmapped cloud hostname")
    await asyncio.to_thread(relay.check_proxy, proxy, relay.TARGET)
    slots = asyncio.Semaphore(32)

    async def accept(reader, writer):
        if slots.locked():
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return
        async with slots:
            try:
                await relay.forward(reader, writer, proxy)
            except Exception as error:
                logging.warning("Cloud relay connection failed: %s", type(error).__name__)

    async with await asyncio.start_server(accept, "127.0.0.1", port) as server:
        logging.info("Cloud relay listening on loopback")
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("model", "cloud", "configure", "check"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--proxy-port", type=int)
    args = parser.parse_args()
    if args.mode == 'configure':
        try:
            print('当前 HTTP 代理  ' + proxy_config.proxy_url(proxy_config.resolve()))
        except (OSError, ValueError, RuntimeError) as error:
            print(proxy_config.error_message(error))
        value = input('输入 auto 自动读取，或填写本机 HTTP 地址（例如 127.0.0.1:10809）\n> ').strip()
        proxy_config.save_settings(value)
        print('设置已保存。重新启动云端中继后使用新地址。')
        return
    if args.mode == 'check':
        for name, ok, detail in proxy_config.diagnostics():
            print(('OK ' if ok else 'FAIL ') + name + '  ' + detail)
        return
    if args.port is None or not 1 <= args.port <= 65535 or (args.proxy_port is not None and not 1 <= args.proxy_port <= 65535):
        parser.error("ports must be between 1 and 65535")
    proxy = ('127.0.0.1', args.proxy_port) if args.proxy_port is not None else proxy_config.resolve()
    if args.port == proxy[1]:
        parser.error("the relay and its upstream proxy must use different ports")
    logging.basicConfig(
        filename=Path(__file__).with_name(f"{args.mode}.log"),
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.mode == "model":
            serve_model(args.port, proxy if args.proxy_port is not None else None)
        else:
            asyncio.run(serve_cloud(args.port, proxy))
    except Exception as error:
        logging.error("Relay stopped: %s", type(error).__name__)
        raise


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(proxy_config.error_message(error) + '\n请运行 Configure-Proxy.cmd 更新代理设置。', file=sys.stderr)
        raise SystemExit(2)
