# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=4.1,<5"]
# ///
"""Start second-brain-imap the way an operator starts it, and talk MCP to it.

    uv run scripts/smoke.py                      # this checkout
    uv run scripts/smoke.py --from dist/*.whl    # a built wheel
    uv run scripts/smoke.py --expect 0.1.0       # and check the version it reports

It installs the given package with ``uvx`` into a throwaway environment, runs
``serve`` over stdio and then over HTTP, and checks what a client sees: the four
tools, all marked read-only, and a health check. No mail server is needed. The
server is pointed at a port nothing listens on, and must answer every call with
the sentence that says the mail server isn't answering, which also shows that it
fails closed. What this catches is what unit tests can't: a wheel missing a
module, an entry point that doesn't start, a dependency that doesn't resolve.
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

TOOLS = {"mail_list_folders", "mail_search", "mail_recent", "mail_read"}
NOT_ANSWERING = "The mail server isn't answering."
PACKAGE = "second-brain-imap"


def free_port() -> int:
    """A port that was free a moment ago: closed for IMAP, or ours to listen on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def environment() -> dict[str, str]:
    """This process's environment without any SBI_* setting, pointed at a closed IMAP port."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("SBI_")}
    env.update(
        SBI_IMAP_HOST="127.0.0.1",
        SBI_IMAP_PORT=str(free_port()),
        SBI_IMAP_USERNAME="smoke@example.com",
        SBI_IMAP_PASSWORD="not-a-real-password",
        SBI_IMAP_TIMEOUT="5",
    )
    return env


def uvx(spec: str, python: str | None, *arguments: str) -> list[str]:
    command = ["uvx", "--from", spec, "--reinstall-package", PACKAGE]
    if python:
        command += ["--python", python]
    return [*command, PACKAGE, *arguments]


async def check_client(client: Client[Any], label: str, expect: str | None) -> None:
    info = client.server_info
    assert info is not None, f"{label}: the server did not say what it is"
    assert info.name == PACKAGE, f"{label}: the server calls itself {info.name!r}"
    if expect is not None:
        assert info.version == expect, f"{label}: version {info.version!r}, expected {expect!r}"
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == TOOLS, f"{label}: tools are {sorted(tools)}"
    for name, tool in tools.items():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True, (
            f"{label}: {name} is not marked read-only"
        )
    for name, arguments in (
        ("mail_list_folders", {}),
        ("mail_search", {"text": "anything"}),
        ("mail_recent", {}),
    ):
        result = await client.call_tool(name, arguments, raise_on_error=False)
        text = getattr(result.content[0], "text", "") if result.content else ""
        assert result.is_error and text.startswith(NOT_ANSWERING), (
            f"{label}: {name} with no mail server answered {text[:80]!r}"
        )
    print(f"ok  {label}: {PACKAGE} {info.version}, {len(tools)} read-only tools, fails closed")


async def smoke(spec: str, python: str | None, expect: str | None) -> None:
    env = environment()
    async with Client(
        StdioTransport(uvx(spec, python, "serve")[0], uvx(spec, python, "serve")[1:], env=env)
    ) as client:
        await check_client(client, "stdio", expect)

    port = free_port()
    server = subprocess.Popen(
        uvx(spec, python, "serve", "--transport", "http", "--no-auth", "--port", str(port)),
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        health: tuple[int, object] | None = None
        for _ in range(120):
            if server.poll() is not None:
                raise AssertionError(
                    f"http: the server exited:\n{server.stderr.read() if server.stderr else ''}"
                )
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=2) as response:
                    health = (response.status, json.loads(response.read()))
            except urllib.error.HTTPError as answered:
                health = (answered.code, json.loads(answered.read()))
            except OSError:
                await asyncio.sleep(0.5)
                continue
            break
        assert health == (503, {"server": True, "mail": False}), f"http: /healthz gave {health}"
        async with Client(f"{base}/mcp") as client:
            await check_client(client, "http", expect)
        request = urllib.request.Request(f"{base}/healthz", headers={"Host": "elsewhere.example"})
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as refused:
            assert refused.code == 421, f"http: an unknown Host header got {refused.code}"
        else:
            raise AssertionError("http: an unknown Host header was accepted")
        print("ok  http: /healthz says the mail server is down (503); an unknown Host is refused")
    finally:
        server.terminate()
        try:
            server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--from",
        dest="spec",
        default=str(Path(__file__).resolve().parent.parent),
        help="what to install: a wheel, a directory or a requirement (default: this checkout)",
    )
    parser.add_argument("--python", help="the Python version uvx should use")
    parser.add_argument("--expect", help="the version the server must report")
    args = parser.parse_args()
    try:
        asyncio.run(smoke(args.spec, args.python, args.expect))
    except AssertionError as failure:
        sys.exit(f"smoke test FAILED: {failure}")
    print("smoke test passed")


if __name__ == "__main__":
    main()
