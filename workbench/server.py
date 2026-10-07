"""Local HTTP bridge added by Ao Li, 2026. Apache-2.0.

The upstream Go service owns browser automation. This bridge owns local drafts,
uploaded images and the exact content reviewed before a manual submission.
"""

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

UPSTREAM_VERSION = "v2.5.5"
MAX_IMAGE = 10 * 1024 * 1024
MAX_BODY = 15 * 1024 * 1024
ASSET_PATTERN = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp|gif)$")
VISIBILITY = {"public": "公开可见", "private": "仅自己可见", "friends": "仅互关好友可见"}


class APIError(Exception):
    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def atomic_write(path, data):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def title_length(text):
    # 与上游 UTF-16 计数规则一致，emoji 的代理项分别计数。
    units = text.encode("utf-16-le")
    weight = sum(1 if int.from_bytes(units[i:i + 2], "little") <= 127 else 2
                 for i in range(0, len(units), 2))
    return (weight + 1) // 2


def normalize_draft(value):
    if not isinstance(value, dict):
        raise APIError("草稿必须是 JSON 对象")
    draft_id = value.get("id") or str(uuid.uuid4())
    try:
        draft_id = str(uuid.UUID(draft_id))
    except (ValueError, TypeError, AttributeError):
        raise APIError("草稿 ID 无效") from None
    draft = {"id": draft_id}
    for key, maximum in (("title", 200), ("content", 20000), ("schedule_at", 80)):
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > maximum:
            raise APIError(f"{key} 格式错误或过长")
        # 拒绝 JSON 孤立代理项，确保保存与上游发送可以正常编码。
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise APIError(f"{key} 包含无效字符") from None
        draft[key] = text
    tags = value.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 10:
        raise APIError("工作台最多支持 10 个标签")
    if any(not isinstance(tag, str) or not tag.strip() or len(tag) > 30 for tag in tags):
        raise APIError("标签不能为空，每个最多 30 个字符")
    draft["tags"] = list(dict.fromkeys(tag.strip().lstrip("#") for tag in tags))
    if any(not tag for tag in draft["tags"]):
        raise APIError("标签不能为空")
    draft["visibility"] = value.get("visibility", "public")
    if not isinstance(draft["visibility"], str) or draft["visibility"] not in VISIBILITY:
        raise APIError("可见范围无效")
    original = value.get("is_original", False)
    if not isinstance(original, bool):
        raise APIError("原创声明格式无效")
    draft["is_original"] = original
    images = value.get("images", [])
    if not isinstance(images, list) or len(images) > 18:
        raise APIError("工作台每篇最多支持 18 张图片")
    draft["images"] = []
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("id"), str):
            raise APIError("图片格式无效")
        asset_id = image["id"]
        if not ASSET_PATTERN.fullmatch(asset_id):
            raise APIError("只能使用工作台上传的图片")
        name = image.get("name", "图片")
        if not isinstance(name, str) or len(name) > 200:
            raise APIError("图片名称无效")
        draft["images"].append({"id": asset_id, "name": name, "url": "/assets/" + asset_id})
    try:
        json.dumps(draft, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise APIError("草稿包含无效字符") from None
    return draft


class LocalStore:
    def __init__(self, root):
        self.root = private_dir(root)
        self.drafts = private_dir(self.root / "drafts")
        self.uploads = private_dir(self.root / "uploads")
        self.receipts = private_dir(self.root / "receipts")
        self.lock = threading.RLock()

    def save(self, value):
        draft = normalize_draft(value)
        draft["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self.lock:
            path = self.drafts / (draft["id"] + ".json")
            if not path.exists() and len(list(self.drafts.glob("*.json"))) >= 200:
                raise APIError("草稿已达 200 份，请先清理或备份")
            atomic_write(path, json.dumps(draft, ensure_ascii=False).encode("utf-8"))
        return draft

    def list(self):
        with self.lock:
            drafts = []
            for path in self.drafts.glob("*.json"):
                try:
                    draft = json.loads(path.read_text("utf-8"))
                    normalize_draft(draft)
                except (OSError, ValueError, APIError):
                    raise APIError("草稿文件损坏，请先备份运行目录；未覆盖原文件", 500) from None
                drafts.append(draft)
            return sorted(drafts, key=lambda item: item.get("updated_at", ""), reverse=True)

    def delete(self, draft_id):
        try:
            draft_id = str(uuid.UUID(draft_id))
        except (ValueError, TypeError):
            raise APIError("草稿 ID 无效") from None
        with self.lock:
            path = self.drafts / (draft_id + ".json")
            path.unlink(missing_ok=True)
        return draft_id

    def asset_path(self, asset_id):
        if not ASSET_PATTERN.fullmatch(asset_id):
            raise APIError("图片 ID 无效", 404)
        path = self.uploads / asset_id
        if not path.is_file() or path.is_symlink():
            raise APIError("图片不存在，请重新上传", 404)
        return path

    def upload(self, value):
        name, encoded = value.get("filename"), value.get("base64")
        if not isinstance(name, str) or not name or len(name) > 200 or not isinstance(encoded, str):
            raise APIError("图片文件格式无效")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise APIError("图片数据无效") from None
        if not data or len(data) > MAX_IMAGE:
            raise APIError("每张图片不能超过 10 MiB")
        extension = None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            extension = "jpg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            extension = "gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            extension = "webp"
        if not extension:
            raise APIError("支持 PNG、JPEG、WebP 和 GIF；暂不支持 SVG、HEIC")
        asset_id = uuid.uuid4().hex + "." + extension
        atomic_write(self.uploads / asset_id, data)
        return {"id": asset_id, "name": Path(name.replace("\\", "/")).name,
                "url": "/assets/" + asset_id}

    def prepare(self, value, now=None, check_schedule=True):
        draft = normalize_draft(value)
        if not draft["title"].strip() or not draft["content"].strip():
            raise APIError("请填写标题和正文")
        if title_length(draft["title"]) > 20:
            raise APIError("标题超出上游 20 字计数限制，请缩短后再预览")
        if len(draft["content"]) > 1000:
            raise APIError("工作台图文正文最多 1000 个字符，请缩短后再预览")
        if not draft["images"]:
            raise APIError("图文笔记至少需要一张图片")
        paths = [str(self.asset_path(image["id"]).resolve()) for image in draft["images"]]
        schedule = draft["schedule_at"]
        if schedule:
            try:
                scheduled = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
                if scheduled.tzinfo is None:
                    raise ValueError("missing timezone")
            except ValueError:
                raise APIError("定时时间必须包含时区，例如 +08:00") from None
            current = now or datetime.now(timezone.utc)
            if check_schedule and not current + timedelta(hours=1) <= scheduled <= current + timedelta(days=14):
                raise APIError("定时时间需在 1 小时后至 14 天内")
        request = {key: draft[key] for key in ("title", "content", "tags", "schedule_at", "is_original")}
        request["visibility"] = VISIBILITY[draft["visibility"]]
        request["images"] = paths
        return request

    def submission_status(self, key):
        with self.lock:
            path = self.receipts / (key + ".json")
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text("utf-8"))["status"]
            except (OSError, ValueError, KeyError, TypeError):
                return "unknown"

    def record_submission(self, key, status):
        with self.lock:
            record = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
            atomic_write(self.receipts / (key + ".json"), json.dumps(record).encode())


class Backend:
    def __init__(self, url, token=""):
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("发布服务地址必须是本机 http://127.0.0.1 或 localhost")
        if parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("发布服务地址不能带路径、凭据或查询参数")
        self.url = url.rstrip("/")
        self.token = token

    def call(self, path, payload=None, timeout=90):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(self.url + path, data=body, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.load(error)
                message = result.get("message") or result.get("error") or "发布服务拒绝了请求"
            except (ValueError, AttributeError):
                message = "发布服务拒绝了请求"
            raise APIError(str(message), 502) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise APIError("无法连接本地发布服务，请检查服务是否运行", 502) from None
        if not isinstance(result, dict) or result.get("success") is not True:
            raise APIError(str(result.get("message") or result.get("error") or "发布服务返回异常")
                           if isinstance(result, dict) else "发布服务返回异常", 502)
        return result


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, backend, web_root):
        super().__init__(address, Handler)
        self.store = store
        self.backend = backend
        self.web_root = Path(web_root)
        self.session = secrets.token_urlsafe(32)
        self.confirmations = {}
        self.confirmation_lock = threading.Lock()
        self.publish_lock = threading.Lock()
        self.origins = {f"http://127.0.0.1:{self.server_port}", f"http://localhost:{self.server_port}"}

    @staticmethod
    def submission_key(request):
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=False)
        return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def require_unsubmitted(self, key):
        status = self.store.submission_status(key)
        if status == "submitted":
            raise APIError("这份内容已提交，请到小红书核对；工作台不会重复提交相同内容", 409, "ALREADY_SUBMITTED")
        if status in ("pending", "unknown"):
            raise APIError("上次提交结果待核对。请先到小红书确认；只有确认未发布才能解除锁定。", 409, "PUBLISH_RESULT_UNKNOWN")

    def review(self, value):
        request = self.store.prepare(value)
        canonical, key = self.submission_key(request)
        self.require_unsubmitted(key)
        confirmation = secrets.token_urlsafe(24)
        with self.confirmation_lock:
            self.confirmations = {key: item for key, item in self.confirmations.items() if item[1] > time.monotonic()}
            if len(self.confirmations) >= 100:
                raise APIError("预览过于频繁，请稍后重试", 429)
            self.confirmations[confirmation] = (canonical, time.monotonic() + 300)
        return {"request": request, "confirmation_id": confirmation,
                "warnings": ["提交后仍需到小红书核对审核与可见状态。"]}

    def publish(self, value):
        if value.get("confirmed") is not True:
            raise APIError("请先预览内容并手动确认提交")
        request = self.store.prepare(value)
        confirmation = value.get("confirmation_id")
        if not isinstance(confirmation, str):
            raise APIError("请先打开发布前预览")
        canonical, key = self.submission_key(request)
        if not self.publish_lock.acquire(blocking=False):
            raise APIError("已有提交正在处理，请勿重复操作", 409)
        try:
            self.require_unsubmitted(key)
            with self.confirmation_lock:
                reviewed = self.confirmations.pop(confirmation, None)
            if not reviewed or reviewed[1] <= time.monotonic() or reviewed[0] != canonical:
                raise APIError("预览已过期、已使用或内容发生变化，请重新预览", 409)
            logged = self.backend.call("/api/v1/login/status")
            if not logged.get("data", {}).get("is_logged_in"):
                raise APIError("请先扫码登录并核对账号", 409)
            # 先落盘，进程中断或响应丢失后也不能用新预览绕过重复提交保护。
            self.store.record_submission(key, "pending")
            try:
                result = self.backend.call("/api/v1/publish", request, timeout=240)
            except APIError:
                # 请求发送后即使报错也不自动重试，防止重复发帖。
                self.store.record_submission(key, "unknown")
                raise APIError("提交未获确认，结果未知。请到小红书核对，勿重复提交。", 502, "PUBLISH_RESULT_UNKNOWN") from None
            self.store.record_submission(key, "submitted")
            return result
        finally:
            self.publish_lock.release()

    def reset_submission(self, value):
        if value.get("confirmed_not_published") is not True:
            raise APIError("需先到小红书核对并明确确认未发布")
        request = self.store.prepare(value, check_schedule=False)
        _, key = self.submission_key(request)
        if not self.publish_lock.acquire(blocking=False):
            raise APIError("提交仍在处理中，请等待完成后再核对", 409)
        try:
            if self.store.submission_status(key) not in ("pending", "unknown"):
                raise APIError("只有结果未知的提交可以解除锁定", 409)
            self.store.record_submission(key, "cleared")
            return {"reset": True}
        finally:
            self.publish_lock.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # 不把草稿内容、令牌或二维码写入访问日志。
        return

    def respond(self, status, data, content_type="application/json; charset=utf-8", cookie=False):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, dict) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"xhs_workbench={self.server.session}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self, mutation=False):
        host = self.headers.get("Host", "")
        if "http://" + host not in self.server.origins:
            raise APIError("请求主机无效", 403)
        if mutation:
            origin = self.headers.get("Origin")
            if origin and origin not in self.server.origins:
                raise APIError("拒绝来自其他网站的操作", 403)
        if self.path.split("?", 1)[0] in ("/", "/index.html", "/app.js", "/style.css", "/favicon.ico"):
            return
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            raise APIError("请从工作台首页重新进入", 401) from None
        supplied = cookies.get("xhs_workbench")
        if not supplied or not secrets.compare_digest(supplied.value, self.server.session):
            raise APIError("请从工作台首页重新进入", 401)

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise APIError("请求需使用 JSON", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError("请求长度无效") from None
        if not 0 < length <= MAX_BODY:
            raise APIError("请求为空或过大", 413)
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise APIError("JSON 格式无效") from None
        if not isinstance(value, dict):
            raise APIError("请求必须是 JSON 对象")
        return value

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        try:
            self.authorized(method != "GET")
            path = urlsplit(self.path).path
            if method == "GET" and path in ("/", "/index.html", "/app.js", "/style.css"):
                file = self.server.web_root / ("index.html" if path in ("/", "/index.html") else path[1:])
                content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[file.suffix]
                return self.respond(200, file.read_bytes(), content_type + "; charset=utf-8", cookie=file.suffix == ".html")
            if method == "GET" and path.startswith("/assets/"):
                file = self.server.store.asset_path(path.removeprefix("/assets/"))
                return self.respond(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            if method == "GET" and path == "/api/health":
                try:
                    self.server.backend.call("/health", timeout=2)
                    healthy = True
                except APIError:
                    healthy = False
                return self.respond(200, {"success": True, "data": {"backend_healthy": healthy, "upstream_version": UPSTREAM_VERSION}})
            if method == "GET" and path in ("/api/login/status", "/api/login/qrcode"):
                return self.respond(200, self.server.backend.call(path.replace("/api/", "/api/v1/", 1)))
            if method == "GET" and path == "/api/drafts":
                return self.respond(200, {"success": True, "data": {"drafts": self.server.store.list()}})
            value = self.body() if method == "POST" else None
            if method == "POST" and path == "/api/drafts":
                data = {"draft": self.server.store.save(value)}
            elif method == "POST" and path == "/api/assets":
                data = {"asset": self.server.store.upload(value)}
            elif method == "POST" and path == "/api/prepare":
                data = self.server.review(value)
            elif method == "POST" and path == "/api/publish":
                return self.respond(200, self.server.publish(value))
            elif method == "POST" and path == "/api/publish/reset":
                data = self.server.reset_submission(value)
            elif method == "DELETE" and path.startswith("/api/drafts/"):
                data = {"deleted": self.server.store.delete(path.removeprefix("/api/drafts/"))}
            else:
                raise APIError("页面或接口不存在", 404)
            self.respond(200, {"success": True, "data": data})
        except APIError as error:
            self.respond(error.status, {"success": False, "error": str(error), "code": error.code})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond(500, {"success": False, "error": "本地操作失败，请检查运行目录权限或磁盘空间"})
