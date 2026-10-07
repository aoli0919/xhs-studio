#!/usr/bin/env python3
"""Launcher added by Ao Li, 2026. Apache-2.0. Python 3.11+."""

import argparse
import hashlib
import os
import platform
import secrets
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.request import urlopen

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workbench.server import Backend, LocalStore, WorkbenchServer, UPSTREAM_VERSION, atomic_write, private_dir

RELEASE = "https://github.com/xpzouying/xiaohongshu-mcp/releases/download/" + UPSTREAM_VERSION
DIGESTS = {
    "darwin-arm64": "fad3633fda4a8060e66e2941bc8fc5460326c7cb4e58a32485a9e2c9408e95a8",
    "linux-amd64": "a4e99322156e7a169466e793045dadc3306c0792db3eade603e030cc0c1c2e3a",
    "windows-amd64": "cb55674f90c1649c875be9c4cde3af193b02fc34ffc860c40c8d711e6ca53e27",
}


def install(root):
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    machine = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}.get(platform.machine())
    target = f"{system}-{machine}"
    if target not in DIGESTS:
        raise RuntimeError("此平台没有固定版本安装包。支持 macOS Apple Silicon、Linux x64、Windows x64；其他平台可用 --connect-existing。")
    name = "xiaohongshu-mcp-" + target + (".exe" if system == "windows" else "")
    folder = private_dir(Path(root) / "bin")
    path = folder / name
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == DIGESTS[target]:
        return path
    print(f"下载官方发布服务 {UPSTREAM_VERSION}（首次运行）…", flush=True)
    with urlopen(RELEASE + "/" + name, timeout=60) as response:
        data = response.read(40 * 1024 * 1024 + 1)
    if len(data) > 40 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != DIGESTS[target]:
        raise RuntimeError("下载文件的 SHA256 校验失败，未安装或执行。")
    atomic_write(path, data)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def parser():
    cli = argparse.ArgumentParser(description="小红书本地创作工作台")
    cli.add_argument("--ui-port", type=int, default=18088)
    cli.add_argument("--backend-port", type=int, default=18061)
    cli.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    cli.add_argument("--connect-existing", metavar="URL", help="连接已有的本机 HTTP 发布服务，不启动新进程")
    cli.add_argument("--backend-token-file", type=Path, help="已有服务的鉴权令牌文件，不会写入网页")
    cli.add_argument("--install-only", action="store_true", help="只下载并校验官方发布服务")
    cli.add_argument("--runtime-dir", type=Path, default=Path(__file__).parent / ".runtime")
    cli.add_argument("--binary", type=Path, help="使用本地发布服务可执行文件，适合开发；由用户自行确认来源")
    return cli


def runtime_path(path):
    path = Path(path).resolve()
    repository = Path(__file__).resolve().parent.parent
    default = Path(__file__).resolve().parent / ".runtime"
    if path.is_relative_to(repository) and path != default.resolve():
        raise ValueError("仓库内仅允许默认 workbench/.runtime；自定义运行目录请放在仓库外，避免私密数据进入 Git。")
    return path


def main():
    args = parser().parse_args()
    if not 1 <= args.ui_port <= 65535 or not 1 <= args.backend_port <= 65535:
        raise SystemExit("端口必须在 1–65535 范围内")
    try:
        root = private_dir(runtime_path(args.runtime_dir))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if args.install_only:
        print(install(root))
        return
    process = None
    log = None
    try:
        if args.connect_existing:
            token = args.backend_token_file.read_text("utf-8").strip() if args.backend_token_file else os.environ.get("XHS_BACKEND_TOKEN", "")
            backend = Backend(args.connect_existing, token)
        else:
            if args.backend_token_file:
                raise RuntimeError("--backend-token-file 仅用于 --connect-existing")
            token_path = root / "backend-token"
            if not token_path.exists():
                atomic_write(token_path, secrets.token_urlsafe(32).encode("ascii"))
            token = token_path.read_text("utf-8").strip()
            binary = args.binary.resolve() if args.binary else install(root)
            if not binary.is_file():
                raise RuntimeError("发布服务可执行文件不存在")
            backend = Backend(f"http://127.0.0.1:{args.backend_port}", token)
            # 若端口已被占用，不能把意外服务误认成自己的进程。
            import socket
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", args.backend_port)) == 0:
                    raise RuntimeError("发布服务端口已被占用，请换 --backend-port 或用 --connect-existing")
            environment = dict(os.environ, AUTH_TOKEN=token, COOKIES_PATH=str(root / "cookies.json"))
            fd = os.open(root / "backend.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            log = os.fdopen(fd, "ab")
            process = subprocess.Popen([str(binary), "-port", f"127.0.0.1:{args.backend_port}"],
                                       cwd=root, env=environment, stdout=log, stderr=log)
        store = LocalStore(root)
        server = WorkbenchServer(("127.0.0.1", args.ui_port), store, backend, Path(__file__).parent / "web")
        address = f"http://127.0.0.1:{server.server_port}"
        print(f"工作台：{address}", flush=True)
        print("草稿与账号状态仅保存在本机。首次启动浏览器可能需要下载，详情见 .runtime/backend.log。", flush=True)
        if not args.no_open:
            webbrowser.open(address)
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupt)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if log:
            log.close()


if __name__ == "__main__":
    main()
