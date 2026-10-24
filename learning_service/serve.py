"""
Run the app so other devices on the same network can open it.

    python -m uv run serve.py            # everyone on the network: http://<this PC's address>:8000
    python -m uv run serve.py --local    # this PC only
    python -m uv run serve.py --port 9000

All API data routes require authentication. Model generation is opt-in.

(No --reload here either: on this Windows/OneDrive setup it leaves stale workers running old code.)
"""

from __future__ import annotations

import argparse
import socket

import uvicorn

from app.main import create_app


def order(default_ip: str | None, others: list[str]) -> list[str]:
    """Addresses worth sharing: the one used to reach the internet first, then the rest.
    Loopback and link-local (169.254.x.x, i.e. no real network) are dropped; duplicates removed."""
    wanted = ([default_ip] if default_ip else []) + others
    return list(dict.fromkeys(a for a in wanted if not a.startswith(("127.", "169.254."))))


def addresses() -> list[str]:
    default_ip = None
    try:  # a UDP "connect" sends nothing; it only asks the OS which interface would carry traffic out
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            default_ip = s.getsockname()[0]
    except OSError:
        pass  # offline: fall back to whatever the hostname resolves to
    try:
        others = [
            info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        ]
    except OSError:
        others = []
    return order(default_ip, others)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--local", action="store_true", help="only this PC can connect (127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    if args.local:
        print(f"\n  This PC only:  http://localhost:{args.port}   (API docs: /docs)\n")
    else:
        urls = [f"http://{ip}:{args.port}" for ip in addresses()] or [
            f"http://<this PC's address>:{args.port}"
        ]
        print("\n  Share this with people on the same network:")
        for url in urls:
            print(f"    {url}        API docs: {url}/docs")
        print("\n  Authentication is required. Generation is opt-in.")
        print("  Press Ctrl+C to stop.\n")
    uvicorn.run(create_app(), host="127.0.0.1" if args.local else "0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
