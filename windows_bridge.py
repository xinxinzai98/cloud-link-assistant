"""Run the released Cloud Link network workers on Windows loopback."""

import argparse
import asyncio
import contextlib
import http.client
import logging
import os
from pathlib import Path
import ssl

import https_compat
import relay


def serve_model(port, proxy):
    def connect_official():
        connection = http.client.HTTPSConnection(
            *proxy, timeout=12, context=ssl.create_default_context()
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
    parser.add_argument("mode", choices=("model", "cloud"))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--proxy-port", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.proxy_port <= 65535:
        parser.error("ports must be between 1 and 65535")
    if args.port == args.proxy_port:
        parser.error("the relay and its upstream proxy must use different ports")
    logging.basicConfig(
        filename=Path(__file__).with_name(f"{args.mode}.log"),
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    proxy = ("127.0.0.1", args.proxy_port)
    try:
        if args.mode == "model":
            serve_model(args.port, proxy)
        else:
            asyncio.run(serve_cloud(args.port, proxy))
    except Exception as error:
        logging.error("Relay stopped: %s", type(error).__name__)
        raise


if __name__ == "__main__":
    main()
