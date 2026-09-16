"""Module extracting and downloading unwatermarked Dola videos from conversation ID or URL."""
import asyncio
import base64
import json
import re
import time
import urllib.request
from pathlib import Path
import aiohttp
from dola_client import DolaClient
import config


def get_active_cookie() -> str:
    """Reads the first valid cookie line from cookies.txt."""
    path = Path(config.COOKIES_FILE)
    if not path.exists():
        raise FileNotFoundError("cookies.txt not found. Please import cookie first.")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "sessionid" in line:
            return line
    raise ValueError("No valid session cookie found in cookies.txt")


def parse_conv_id(text_or_url: str) -> str:
    """Extracts numeric conversation_id from URL or plain string."""
    text = str(text_or_url or "").strip()
    match = re.search(r"(\d{15,22})", text)
    if match:
        return match.group(1)
    return text


def decode_main_url(raw_b64: str) -> str:
    """Decodes main_url token from base64/urlsafe-base64."""
    if not raw_b64:
        return ""
    if raw_b64.startswith("http"):
        return raw_b64
    normalized = raw_b64.replace("-", "+").replace("_", "/")
    padded = normalized + "=" * ((4 - len(normalized) % 4) % 4)
    try:
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass
    return ""


async def extract_videos_from_conv(conversation_id_or_url: str, cookie: str = None) -> list[dict]:
    """Fetches all video streams from a Dola conversation and resolves unwatermarked links."""
    cid = parse_conv_id(conversation_id_or_url)
    if not cid:
        raise ValueError("Invalid conversation ID or URL")

    cookie_str = cookie or get_active_cookie()
    client = DolaClient(cookie_str)

    body = {
        "cmd": 3100,
        "uplink_body": {
            "pull_singe_chain_uplink_body": {
                "conversation_id": cid,
                "anchor_index": 9007199254740991,
                "conversation_type": 3,
                "direction": 1,
                "limit": 20,
                "ext": {},
                "filter": {"index_list": []},
                "evaluate_ab_params": "",
                "evaluate_common_params": "",
            }
        },
        "sequence_id": str(int(time.time() * 1000)),
        "channel": 2,
        "version": "1",
    }

    async with aiohttp.ClientSession() as session:
        res = await client._request_im_chain(session, body, conversation_id=cid)

    messages = res.get("downlink_body", {}).get("pull_singe_chain_downlink_body", {}).get("messages", [])
    videos_found = []

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except Exception:
                pass
        if not isinstance(content, list):
            continue

        for block in content:
            if block.get("block_type") == 2074:
                creations = block.get("content", {}).get("creation_block", {}).get("creations", [])
                for cre in creations:
                    vid = cre.get("video", {})
                    watermarked_url = vid.get("download_url") or ""
                    vm_str = vid.get("video_model", "")
                    unwatermarked_url = ""
                    best_bitrate = 0

                    if vm_str:
                        try:
                            vm = json.loads(vm_str)
                            vlist = vm.get("video_list", {})
                            for item in vlist.values():
                                if not isinstance(item, dict):
                                    continue
                                raw_token = item.get("main_url") or ""
                                decoded_url = decode_main_url(raw_token)
                                bitrate = int(item.get("bitrate") or item.get("real_bitrate") or 0)
                                if decoded_url and (bitrate >= best_bitrate or not unwatermarked_url):
                                    best_bitrate = bitrate
                                    unwatermarked_url = decoded_url
                        except Exception:
                            pass

                    # Target unwatermarked stream with fallback
                    clean_url = unwatermarked_url or watermarked_url
                    if clean_url:
                        videos_found.append({
                            "conversation_id": cid,
                            "unwatermarked_url": clean_url,
                            "watermarked_url": watermarked_url,
                            "is_clean": bool(unwatermarked_url),
                            "bitrate": best_bitrate,
                        })

    return videos_found


async def download_clean_video(conversation_id_or_url: str, output_name: str = None) -> dict:
    """Extracts the latest unwatermarked video and downloads it to downloads/."""
    videos = await extract_videos_from_conv(conversation_id_or_url)
    if not videos:
        raise ValueError("Không tìm thấy video nào trong đoạn chat này!")

    best = videos[-1]  # Get the most recent video
    clean_url = best["unwatermarked_url"]

    Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    filename = output_name or f"clean_{best['conversation_id']}_{int(time.time())}.mp4"
    if not filename.endswith(".mp4"):
        filename += ".mp4"
    filepath = Path(config.DOWNLOAD_DIR) / filename

    # Download async chunked
    async with aiohttp.ClientSession() as session:
        async with session.get(clean_url) as resp:
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                async for chunk in resp.content.iter_chunked(65536):
                    f.write(chunk)

    local_url = f"/videos/{filename}"
    return {
        "ok": True,
        "conversation_id": best["conversation_id"],
        "filename": filename,
        "local_path": str(filepath),
        "local_url": local_url,
        "remote_url": clean_url,
        "is_clean": best["is_clean"],
        "bitrate": best.get("bitrate", 0),
        "size_bytes": filepath.stat().st_size,
        "size_mb": round(filepath.stat().st_size / (1024 * 1024), 2),
        "all_videos": videos,
    }


def list_downloaded_videos() -> list[dict]:
    """Lists existing downloaded MP4 videos in the download directory."""
    p = Path(config.DOWNLOAD_DIR)
    if not p.exists():
        return []
    items = []
    for f in p.glob("*.mp4"):
        st = f.stat()
        items.append({
            "name": f.name,
            "size_bytes": st.st_size,
            "size_mb": round(st.st_size / (1024 * 1024), 2),
            "modified_at": int(st.st_mtime),
            "url": f"/videos/{f.name}",
        })
    items.sort(key=lambda x: x["modified_at"], reverse=True)
    return items

