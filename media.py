"""Public reference media download and security validation (SSRF protected)."""
import asyncio
import base64
import ipaddress
import socket
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from PIL import Image

import config

_ALLOWED_IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


def validate_public_url(url: str) -> str:
    """Accepts only public HTTP(S) URLs, blocks localhost, private network, and credentials."""
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("Invalid reference image URL")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Reference image only supports public http/https URLs")
    if parsed.username or parsed.password:
        raise ValueError("Reference image URL must not contain authentication credentials")
    host = parsed.hostname
    try:
        # SSRF check: check direct IP and DNS resolution against private/reserved ranges
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = awaitable_getaddrinfo(host)
            addresses = {ipaddress.ip_address(x) for x in infos}
        except Exception as exc:
            raise ValueError(f"Failed to resolve reference image domain: {host}") from exc
    if not addresses or any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        for ip in addresses
    ):
        raise ValueError("Reference image points to private or reserved IP address (SSRF blocked)")
    return url


def awaitable_getaddrinfo(host: str) -> list[str]:
    """Synchronous DNS resolution wrapper."""
    return [item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


async def _validate_url_async(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("Invalid reference image URL")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Reference image only supports public http/https URLs")
    if parsed.username or parsed.password:
        raise ValueError("Reference image URL must not contain authentication credentials")
    host = parsed.hostname
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            resolved = await asyncio.to_thread(awaitable_getaddrinfo, host)
            addresses = {ipaddress.ip_address(x) for x in resolved}
        except Exception as exc:
            raise ValueError(f"Failed to resolve reference image domain: {host}") from exc
    if not addresses or any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        for ip in addresses
    ):
        raise ValueError("Reference image points to private or reserved IP address (SSRF blocked)")
    return url


def _get_local_reference_path(raw: str) -> Path | None:
    """Checks if reference string refers to a local uploaded image file."""
    s = str(raw).strip()
    if s.startswith("/uploads/"):
        candidate = Path("uploads") / s[len("/uploads/"):]
        if candidate.is_file():
            return candidate
    elif s.startswith("uploads/") or s.startswith("uploads\\"):
        candidate = Path(s)
        if candidate.is_file():
            return candidate
    elif "://127.0.0.1:" in s or "://localhost:" in s or "://0.0.0.0:" in s:
        parsed = urlparse(s)
        if parsed.path.startswith("/uploads/"):
            candidate = Path("uploads") / parsed.path[len("/uploads/"):]
            if candidate.is_file():
                return candidate
    elif Path(s).is_file():
        return Path(s)
    return None


async def validate_reference_urls(urls: list[str]) -> list[str]:
    """Validates public URLs, local files, and base64 data URIs while preserving exact order."""
    if len(urls) > config.REFERENCE_IMAGE_MAX_COUNT:
        raise ValueError(f"Tối đa {config.REFERENCE_IMAGE_MAX_COUNT} ảnh tham chiếu được phép")
    normalized = []
    for raw in urls:
        if not isinstance(raw, str) or not raw.strip():
            continue
        item = raw.strip()
        # 1. Base64 data URI
        if item.startswith("data:image/"):
            normalized.append(item)
            continue
        # 2. Local uploads file or direct file path
        local_p = _get_local_reference_path(item)
        if local_p:
            normalized.append(str(local_p.resolve()))
            continue
        # 3. Public HTTP/HTTPS URL
        url = await _validate_url_async(item)
        normalized.append(url)
    return normalized


async def _read_response_image(resp: aiohttp.ClientResponse) -> tuple[bytes, str]:
    content_length = resp.headers.get("Content-Length")
    if content_length and int(content_length) > config.REFERENCE_IMAGE_MAX_BYTES:
        raise ValueError("Reference image exceeds single file size limit")
    chunks = []
    total = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > config.REFERENCE_IMAGE_MAX_BYTES:
            raise ValueError("Reference image exceeds single file size limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
            fmt = image.format
    except Exception as exc:
        raise ValueError("Reference file is not a valid image") from exc
    if fmt not in _ALLOWED_IMAGE_FORMATS:
        raise ValueError("Reference image only supports JPEG, PNG, WEBP")
    return data, _ALLOWED_IMAGE_FORMATS[fmt]


async def download_one_image(session: aiohttp.ClientSession, url: str, dest: Path) -> Path:
    current = await _validate_url_async(url)
    # Try configured proxy first, fall back to direct connection if blocked
    proxies = []
    if config.PROXY:
        proxies.append(config.PROXY)
    proxies.append(None)
    last_error = None
    for proxy in proxies:
        current = await _validate_url_async(url)
        for _ in range(5):
            try:
                async with session.get(
                    current,
                    allow_redirects=False,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=config.REFERENCE_DOWNLOAD_TIMEOUT),
                    headers={"User-Agent": "dola-pool-reference-fetch/1.0"},
                ) as resp:
                    if 300 <= resp.status < 400 and resp.headers.get("Location"):
                        current = await _validate_url_async(urljoin(current, resp.headers["Location"]))
                        continue
                    if resp.status != 200:
                        raise ValueError(f"Failed to download reference image: HTTP {resp.status}")
                    data, suffix = await _read_response_image(resp)
                    path = dest.with_suffix(suffix)
                    path.write_bytes(data)
                    return path
            except Exception as exc:
                last_error = exc
                break
    raise ValueError(str(last_error) if last_error else "Failed to download reference image")


async def _process_reference_item(session: aiohttp.ClientSession, item: str, dest_base: Path) -> Path:
    """Processes a single reference item (local file, base64 data URI, or public URL) to local destination."""
    item_str = str(item).strip()

    # 1. Base64 Data URI
    if item_str.startswith("data:image/"):
        header, b64_part = item_str.split(",", 1) if "," in item_str else ("", item_str)
        try:
            data = base64.b64decode(b64_part)
        except Exception as exc:
            raise ValueError("Không thể giải mã dữ liệu ảnh Base64") from exc
        if len(data) > config.REFERENCE_IMAGE_MAX_BYTES:
            raise ValueError(f"Ảnh tham chiếu vượt quá giới hạn {config.REFERENCE_IMAGE_MAX_BYTES // (1024 * 1024)}MB")
        try:
            with Image.open(BytesIO(data)) as img:
                img.verify()
                fmt = img.format
        except Exception as exc:
            raise ValueError("Ảnh tham chiếu Base64 không hợp lệ") from exc
        if fmt not in _ALLOWED_IMAGE_FORMATS:
            raise ValueError("Ảnh tham chiếu chỉ hỗ trợ định dạng JPEG, PNG, WEBP")
        dest = dest_base.with_suffix(_ALLOWED_IMAGE_FORMATS[fmt])
        dest.write_bytes(data)
        return dest

    # 2. Local uploads file or local file path
    local_p = _get_local_reference_path(item_str)
    if local_p and local_p.is_file():
        data = local_p.read_bytes()
        if len(data) > config.REFERENCE_IMAGE_MAX_BYTES:
            raise ValueError(f"Ảnh tham chiếu vượt quá giới hạn {config.REFERENCE_IMAGE_MAX_BYTES // (1024 * 1024)}MB")
        try:
            with Image.open(BytesIO(data)) as img:
                img.verify()
                fmt = img.format
        except Exception as exc:
            raise ValueError("File ảnh cục bộ không hợp lệ") from exc
        if fmt not in _ALLOWED_IMAGE_FORMATS:
            raise ValueError("Ảnh tham chiếu chỉ hỗ trợ định dạng JPEG, PNG, WEBP")
        dest = dest_base.with_suffix(_ALLOWED_IMAGE_FORMATS[fmt])
        dest.write_bytes(data)
        return dest

    # 3. Public URL
    return await download_one_image(session, item_str, dest_base)


async def download_reference_images(urls: list[str], task_id: str) -> tuple[Path | None, list[str]]:
    """Prepares reference images in temp folder, preserving exact user-chosen order. Caller must cleanup."""
    urls = await validate_reference_urls(urls)
    if not urls:
        return None, []
    root = Path(tempfile.mkdtemp(prefix=f"dola_ref_{task_id}_"))
    try:
        paths = []
        async with aiohttp.ClientSession() as session:
            for index, item in enumerate(urls):
                dest_base = root / f"image_{index:03d}"
                saved = await _process_reference_item(session, item, dest_base)
                paths.append(str(saved.resolve()))
        return root, paths
    except Exception:
        for child in root.glob("*"):
            child.unlink(missing_ok=True)
        root.rmdir()
        raise