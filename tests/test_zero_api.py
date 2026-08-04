#!/usr/bin/env python3
"""Zero-API guarantee: memkit must never touch the network or spawn processes.

Two independent enforcement layers (tests, not formal proof):

1. Static — the source may import only an explicit stdlib whitelist, and
   may not call process/dynamic-import escape hatches
   (os.system / os.popen / os.spawn* / os.exec* / os.fork, eval, exec,
   __import__, compile; importlib and ctypes are excluded by the import
   whitelist itself).
2. Dynamic — every socket constructor is replaced with a bomb, then the
   full CLI surface (refresh, check, link dry-run, link real) runs against
   an isolated copy of the shipped sample. If anything dials out, the bomb
   explodes and the test fails.
"""
from __future__ import annotations

import ast
import io
import shutil
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import memkit  # noqa: E402

FORBIDDEN_MODULES = {
    "socket", "ssl", "http", "urllib", "urllib.request", "urllib3",
    "requests", "httpx", "aiohttp", "ftplib", "smtplib", "telnetlib",
    "xmlrpc", "subprocess", "asyncio",
}


class StaticScanTest(unittest.TestCase):
    def test_source_imports_no_network_or_subprocess_modules(self) -> None:
        tree = ast.parse((ROOT / "memkit.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        overlap = imported & {name.split(".")[0] for name in FORBIDDEN_MODULES}
        self.assertEqual(overlap, set(), f"forbidden imports found: {overlap}")

    def test_no_process_or_dynamic_import_escape_hatches(self) -> None:
        forbidden_os_calls = {
            "system", "popen", "fork", "forkpty", "spawnl", "spawnle",
            "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
            "execl", "execle", "execlp", "execlpe", "execv", "execve",
            "execvp", "execvpe", "startfile",
        }
        forbidden_builtins = {"eval", "exec", "__import__", "compile"}
        tree = ast.parse((ROOT / "memkit.py").read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr in forbidden_os_calls
            ):
                offenders.append(f"os.{func.attr}")
            elif isinstance(func, ast.Name) and func.id in forbidden_builtins:
                offenders.append(func.id)
        self.assertEqual(offenders, [], f"escape hatches found: {offenders}")

    def test_only_stdlib_imports(self) -> None:
        allowed = {
            "__future__", "argparse", "collections", "datetime", "hashlib",
            "json", "os", "pathlib", "re", "shutil", "sys", "typing",
        }
        tree = ast.parse((ROOT / "memkit.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertLessEqual(imported, allowed, f"unexpected imports: {imported - allowed}")


class SocketBomb:
    def __init__(self, *args, **kwargs):
        raise AssertionError("network access attempted during a zero-API run")


class DynamicBlockTest(unittest.TestCase):
    def test_full_cli_surface_runs_with_sockets_disabled(self) -> None:
        original_socket = socket.socket
        original_create = socket.create_connection
        socket.socket = SocketBomb  # type: ignore[misc,assignment]
        socket.create_connection = SocketBomb  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp) / "demo"
                shutil.copytree(ROOT / "samples" / "demo", ws)
                for argv in (
                    ["refresh", "--workspace", str(ws)],
                    ["check", "--workspace", str(ws)],
                    ["link", "--dry-run", "--workspace", str(ws)],
                    ["link", "--workspace", str(ws)],
                    ["check", "--workspace", str(ws)],
                ):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = memkit.main(argv)
                    self.assertEqual(
                        code, 0, f"{argv} failed:\n{out.getvalue()}{err.getvalue()}"
                    )
        finally:
            socket.socket = original_socket  # type: ignore[misc]
            socket.create_connection = original_create
