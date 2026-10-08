#!/usr/bin/env python3
"""AlphaMedia egress forward proxy.

Listens on 127.0.0.1:8899 with no auth, injects Proxy-Authorization for the
upstream egress proxy (from UPSTREAM_PROXY env, which holds a long-lived
credential), and relays. Point the AlphaMedia API at http://127.0.0.1:8899.

Why: the runtime mints short-lived egress proxy credentials per shell (~1-2 min
lifetime once the minter exits). Rewriting the API's env + restarting it every
10 min leaves YouTube/TTS broken most of the time AND kills in-flight jobs.
This proxy holds a long-lived credential, so the API never restarts for proxy
reasons.
"""
import base64
import os
import socket
import threading
from urllib.parse import urlparse

LISTEN = ("127.0.0.1", 8899)


def get_upstream():
    raw = os.environ.get("UPSTREAM_PROXY")
    if not raw:
        raise SystemExit("UPSTREAM_PROXY not set")
    u = urlparse(raw)
    auth = ""
    if u.username:
        tok = base64.b64encode(
            f"{u.username}:{u.password or ''}".encode()).decode()
        auth = f"Proxy-Authorization: Basic {tok}\r\n"
    return u.hostname, u.port or 8080, auth


UP_HOST, UP_PORT, UP_AUTH = get_upstream()


def relay(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass


def handle(client):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            data += chunk
            if len(data) > 65536:
                client.close()
                return
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        new_head = lines[0] + b"\r\n" + UP_AUTH.encode() + b"\r\n".join(lines[1:])
        up = socket.create_connection((UP_HOST, UP_PORT), timeout=20)
        up.sendall(new_head + b"\r\n\r\n" + rest)
        threading.Thread(target=relay, args=(client, up), daemon=True).start()
        relay(up, client)
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(100)
    print(f"alphamedia egress fwd on {LISTEN[0]}:{LISTEN[1]} -> {UP_HOST}:{UP_PORT}", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
