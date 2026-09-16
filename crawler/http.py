"""按域名限速的 HTTP 抓取：同一域名串行 + 最小间隔，失败或空响应时整域名退避，原始页面落盘。"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BACKOFF = [15, 45, 120, 300]  # 秒；实测苏州站连续请求后会返回 0 字节


class FetchError(Exception):
    pass


def decode_html(content: bytes, header_charset: str | None) -> str:
    candidates = [header_charset] if header_charset else []
    m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', content[:4000], re.I)
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        if enc.lower() in ("gb2312", "gbk"):
            enc = "gb18030"
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, raw_dir: Path, min_interval: float = 3.0, per_host: dict[str, float] | None = None):
        self.raw_dir = raw_dir
        self.min_interval = min_interval
        self.per_host = per_host or {}
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._next_ok: dict[str, float] = defaultdict(float)
        self._insecure_hosts: set[str] = set()
        kw = dict(headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                  follow_redirects=True, timeout=httpx.Timeout(30, read=180))
        self._client = httpx.Client(**kw)
        self._client_insecure = httpx.Client(verify=False, **kw)

    def _host_lock(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks[host]

    def _client_for(self, host: str) -> httpx.Client:
        return self._client_insecure if host in self._insecure_hosts else self._client

    def _attempts(self, url: str, call):
        """在域名锁内执行 call(client)；call 返回 (ok, value, err)。失败时整个域名推迟 BACKOFF 秒。"""
        host = urlsplit(url).hostname or ""
        interval = self.per_host.get(host, self.min_interval)
        err = None
        for attempt in range(len(BACKOFF) + 1):
            with self._host_lock(host):
                wait = self._next_ok[host] - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    ok, value, err = call(self._client_for(host))
                except httpx.ConnectError as e:
                    if "CERTIFICATE" in str(e).upper() and host not in self._insecure_hosts:
                        self._insecure_hosts.add(host)
                        ok, value, err = False, None, e
                        self._next_ok[host] = time.monotonic()
                        continue
                    ok, value, err = False, None, e
                except (httpx.HTTPError, OSError) as e:
                    ok, value, err = False, None, e
                if ok:
                    self._next_ok[host] = time.monotonic() + interval
                    return value
                if isinstance(err, FetchError) and re.match(r"HTTP 4(?!29)\d\d", str(err)):
                    self._next_ok[host] = time.monotonic() + interval  # 403/404 等客户端错误重试也没用，不退避
                    raise err
                if attempt < len(BACKOFF):
                    self._next_ok[host] = time.monotonic() + BACKOFF[attempt]
        raise FetchError(f"{url}: {err}")

    def request(self, url: str, method: str = "GET", data: dict | None = None, min_bytes: int = 200) -> httpx.Response:
        def call(client: httpx.Client):
            resp = client.request(method, url, data=data)
            if resp.status_code == 404:
                return False, None, FetchError(f"HTTP 404 {url}")
            if resp.status_code == 200 and len(resp.content) >= min_bytes:
                return True, resp, None
            return False, None, FetchError(f"HTTP {resp.status_code} ({len(resp.content)} bytes)")
        return self._attempts(url, call)

    def raw_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode()).hexdigest()
        host = urlsplit(url).hostname or "unknown"
        return self.raw_dir / host / digest[:2] / f"{digest}.html"

    def get_text(self, url: str, save_raw: bool = True) -> tuple[str, Path | None]:
        resp = self.request(url)
        text = decode_html(resp.content, resp.charset_encoding)
        if not save_raw:
            return text, None
        path = self.raw_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text, path

    def download(self, url: str, dest: Path, headers: dict | None = None) -> tuple[str, int]:
        """流式下载到 dest，返回 (content_type, 字节数)。部分站点有防盗链，需要带 Referer。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")

        def call(client: httpx.Client):
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 404:
                    return False, None, FetchError(f"HTTP 404 {url}")
                if resp.status_code != 200:
                    return False, None, FetchError(f"HTTP {resp.status_code}")
                size = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 16):
                        f.write(chunk)
                        size += len(chunk)
                if size == 0:
                    return False, None, FetchError("0 bytes")
                tmp.replace(dest)
                return True, (resp.headers.get("content-type", ""), size), None
        try:
            return self._attempts(url, call)
        finally:
            tmp.unlink(missing_ok=True)
