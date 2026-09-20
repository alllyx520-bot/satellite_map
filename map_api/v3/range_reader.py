"""Strict small-range reader for proxies that truncate large COG responses."""
import io
import os
import re
import time
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

import requests
from ..utils.http import request_proxies
from .runtime import current_budget

BLOCK_SIZE = 64 * 1024


def _cog_proxies():
    proxy = os.environ.get("V3_COG_PROXY")
    if not proxy:
        return request_proxies()
    userpwd = os.environ.get("V3_COG_PROXYUSERPWD")
    if userpwd:
        parts = urlsplit(proxy)
        proxy = urlunsplit((parts.scheme, f"{userpwd}@{parts.netloc}", parts.path, parts.query, parts.fragment))
    return {"http": proxy, "https": proxy}


class HTTPRangeFile(io.RawIOBase):
    """A seekable, bounded HTTP object for rasterio's Python VSI opener.

    Every cached block is validated against Content-Range and object identity.
    This changes transport only: GDAL still decodes the original COG tiles.
    """
    def __init__(self, url, *, timeout=900, max_bytes=256*1024*1024):
        super().__init__()
        self.url, self.position, self.size = url, 0, None
        self.budget = current_budget()
        self.etag, self.transferred = None, 0
        self.deadline, self.max_bytes = time.monotonic()+timeout, max_bytes
        self.blocks = OrderedDict()
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self._block(0)

    def _block(self, index):
        if self.budget:
            self.budget.check()
        with self.lock:
            if index in self.blocks:
                self.blocks.move_to_end(index)
                return self.blocks[index]
        if time.monotonic() >= self.deadline or self.transferred >= self.max_bytes:
            raise OSError("COG 分块传输达到时间或字节预算")
        start = index * BLOCK_SIZE
        end = start + BLOCK_SIZE - 1 if self.size is None else min(start+BLOCK_SIZE, self.size)-1
        if end < start:
            return b""
        for attempt in range(2):
            parts = urlsplit(self.url)
            query = "&".join(x for x in (parts.query, "ss_range="+uuid.uuid4().hex) if x)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
            headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity", "Cache-Control": "no-cache"}
            if self.etag:
                headers["If-Match"] = self.etag
            try:
                with requests.get(url, headers=headers, stream=True, timeout=(5, 15), proxies=_cog_proxies()) as response:
                    if response.status_code != 206:
                        raise OSError("源服务器未返回可验证的部分内容")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match:
                        raise OSError("COG 分块缺少有效 Content-Range")
                    first, last, size = map(int, match.groups())
                    if first != start or last != min(end, size-1) or (self.size is not None and size != self.size):
                        raise OSError("COG 分块位置或源文件大小发生变化")
                    etag = response.headers.get("ETag")
                    if self.etag and etag != self.etag:
                        raise OSError("COG 源文件版本在读取期间发生变化")
                    chunks, count = [], 0
                    for chunk in response.iter_content(16*1024):
                        count += len(chunk)
                        if count > last-first+1:
                            raise OSError("COG 分块响应超出请求范围")
                        chunks.append(chunk)
                    if count != last-first+1:
                        raise OSError("COG 分块响应不完整")
                    value = b"".join(chunks)
                    self.size, self.etag = size, etag
                    with self.lock:
                        self.transferred += count
                        self.blocks[index] = value
                        while len(self.blocks) > 128:
                            self.blocks.popitem(last=False)
                    return value
            except (requests.RequestException, OSError) as exc:
                if attempt == 1:
                    raise OSError("无法取得完整的 64 KiB 原始 COG 分块") from exc

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position+offset if whence == 1 else self.size+offset if whence == 2 else -1
        if position < 0:
            raise ValueError("无效的 COG 文件偏移")
        self.position = position
        return position

    def read(self, size=-1):
        size = min(self.size-self.position, size if size >= 0 else self.size-self.position)
        if size > 16*1024*1024:
            raise OSError("单次 COG 读取超过 16 MiB，拒绝整文件解码")
        if size > BLOCK_SIZE:
            first, last = self.position//BLOCK_SIZE, (self.position+size-1)//BLOCK_SIZE
            # Keep prefetch below the bounded LRU capacity; GDAL commonly asks
            # for one compressed tile spanning a few dozen transport blocks.
            indexes = list(range(first, min(last+1, first+64)))
            list(self.pool.map(self._block, indexes))
        output = bytearray()
        while size > 0:
            index, offset = divmod(self.position, BLOCK_SIZE)
            block = self._block(index)
            count = min(size, len(block)-offset)
            if count <= 0:
                break
            output.extend(block[offset:offset+count])
            self.position += count
            size -= count
        return bytes(output)

    def readinto(self, buffer):
        value = self.read(len(buffer))
        buffer[:len(value)] = value
        return len(value)

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.blocks.clear()
        super().close()
