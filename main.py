import asyncio
import re
import uuid
from typing import Optional
from urllib.parse import urlparse, urljoin

import aiohttp
import aiofiles
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
import json
import os
import time
import tempfile
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="PinDrop API",
    description="High-performance Pinterest media downloader API",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = Path(tempfile.gettempdir()) / "pindrop"
TEMP_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}


class PinRequest(BaseModel):
    url: str


class MediaInfo(BaseModel):
    id: str
    type: str  # "image", "video", "gif"
    url: str
    thumbnail: str
    title: str
    quality: Optional[str] = None
    size: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


def extract_pin_id(url: str) -> Optional[str]:
    patterns = [
        r'pinterest\.com/pin/(\d+)',
        r'pin\.it/([A-Za-z0-9]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


async def resolve_shortlink(url: str, session: aiohttp.ClientSession) -> str:
    if "pin.it" in url:
        try:
            async with session.get(url, allow_redirects=True, headers=HEADERS) as resp:
                return str(resp.url)
        except:
            return url
    return url


async def fetch_pin_data(url: str) -> dict:
    connector = aiohttp.TCPConnector(ssl=False, limit=10)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Resolve short links
        url = await resolve_shortlink(url, session)

        pin_id = extract_pin_id(url)

        results = []

        # Try Pinterest API first (fastest)
        if pin_id:
            api_url = f"https://www.pinterest.com/resource/PinResource/get/?source_url=/pin/{pin_id}/&data=%7B%22options%22%3A%7B%22id%22%3A%22{pin_id}%22%2C%22field_set_key%22%3A%22unauth_react%22%7D%7D"

            try:
                async with session.get(api_url, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest",
                                                         "Accept": "application/json"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pin_data = data.get("resource_response", {}).get("data", {})

                        if pin_data:
                            return parse_pin_resource(pin_data, pin_id)
            except Exception as e:
                logger.warning(f"API fetch failed: {e}")

        # Fallback: scrape HTML
        try:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    return parse_html_for_media(html, pin_id or "unknown", url)
        except Exception as e:
            logger.error(f"HTML scrape failed: {e}")

        raise HTTPException(status_code=422,
                            detail="Could not extract media from this Pinterest URL. Ensure the URL is public and valid.")


def parse_pin_resource(data: dict, pin_id: str) -> dict:
    media_type = "image"
    media_url = ""
    thumbnail = ""
    title = data.get("title", "") or data.get("description", "Pinterest Pin") or "Pinterest Media"
    width = None
    height = None

    # Check for video
    videos = data.get("videos", {})
    if videos:
        video_list = videos.get("video_list", {})
        if video_list:
            best_video = None
            best_quality = 0
            for quality, vdata in video_list.items():
                if isinstance(vdata, dict):
                    w = vdata.get("width", 0) or 0
                    if w > best_quality:
                        best_quality = w
                        best_video = vdata

            if best_video:
                media_type = "video"
                media_url = best_video.get("url", "")
                width = best_video.get("width")
                height = best_video.get("height")

    # Images
    images = data.get("images", {})
    if images:
        orig = images.get("orig", {})
        if orig:
            if not media_url:
                media_url = orig.get("url", "")
            thumbnail = orig.get("url", "")
            if not width:
                width = orig.get("width")
            if not height:
                height = orig.get("height")

    # Check if GIF
    if media_url and ".gif" in media_url.lower():
        media_type = "gif"

    return {
        "id": pin_id,
        "type": media_type,
        "url": media_url,
        "thumbnail": thumbnail,
        "title": title[:100] if title else "Pinterest Media",
        "width": width,
        "height": height,
    }


def parse_html_for_media(html: str, pin_id: str, original_url: str) -> dict:
    media_url = ""
    thumbnail = ""
    media_type = "image"
    title = "Pinterest Media"

    # Extract title
    title_match = re.search(r'<title[^>]*>([^<]+)</title>', html)
    if title_match:
        title = re.sub(r'\s*[|\-–]\s*Pinterest\s*$', '', title_match.group(1)).strip()

    # OG meta tags
    og_video = re.search(r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)["\']', html)
    og_image = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)

    if og_video:
        media_url = og_video.group(1)
        media_type = "video"

    if og_image:
        thumbnail = og_image.group(1)
        if not media_url:
            media_url = og_image.group(1)

    # Check JSON-LD
    json_matches = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    for jm in json_matches:
        try:
            jdata = json.loads(jm)
            if isinstance(jdata, list):
                jdata = jdata[0]
            if jdata.get("@type") == "VideoObject":
                cu = jdata.get("contentUrl", "")
                if cu:
                    media_url = cu
                    media_type = "video"
            thumbnail_url = jdata.get("thumbnailUrl", "")
            if thumbnail_url and not thumbnail:
                thumbnail = thumbnail_url
        except:
            pass

    # Pinterest video in inline scripts
    if not media_url or media_type != "video":
        video_patterns = [
            r'"video_url"\s*:\s*"([^"]+\.mp4[^"]*)"',
            r'"url"\s*:\s*"([^"]+\.mp4[^"]*)"',
            r'(https://v\d+\.pinimg\.com/[^"\'\\]+\.mp4[^"\'\\]*)',
        ]
        for pat in video_patterns:
            m = re.search(pat, html)
            if m:
                media_url = m.group(1).replace('\\/', '/').replace('\\u002F', '/')
                media_type = "video"
                break

    # High-res image patterns
    if not media_url or media_type == "image":
        img_patterns = [
            r'(https://i\.pinimg\.com/originals/[^"\'\\]+\.(jpg|jpeg|png|gif|webp))',
            r'(https://i\.pinimg\.com/\d+x/[^"\'\\]+\.(jpg|jpeg|png|gif|webp))',
        ]
        for pat in img_patterns:
            m = re.search(pat, html)
            if m:
                img_url = m.group(1)
                if not media_url:
                    media_url = img_url
                if not thumbnail:
                    thumbnail = img_url
                if ".gif" in img_url.lower():
                    media_type = "gif"
                break

    if ".gif" in media_url.lower():
        media_type = "gif"

    if not media_url:
        raise HTTPException(status_code=422,
                            detail="No downloadable media found. The pin may be private or unsupported.")

    return {
        "id": pin_id,
        "type": media_type,
        "url": media_url,
        "thumbnail": thumbnail or media_url,
        "title": title[:100],
        "width": None,
        "height": None,
    }


async def get_file_size(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        async with session.head(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            cl = resp.headers.get("Content-Length")
            if cl:
                size_mb = int(cl) / (1024 * 1024)
                if size_mb < 1:
                    return f"{int(size_mb * 1024)} KB"
                return f"{size_mb:.1f} MB"
    except:
        pass
    return None


@app.get("/")
async def root():
    return {"status": "PinDrop API running", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": time.time()}


@app.post("/api/analyze")
async def analyze_pin(request: PinRequest):
    url = request.url.strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    if "pinterest" not in url.lower() and "pin.it" not in url.lower():
        raise HTTPException(status_code=400, detail="Please provide a valid Pinterest URL")

    try:
        data = await fetch_pin_data(url)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process Pinterest URL")

    # Get file size asynchronously
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        size = await get_file_size(data["url"], session)

    return {
        "success": True,
        "media": {
            **data,
            "size": size,
            "quality": f"{data['width']}x{data['height']}" if data.get("width") and data.get("height") else "Original",
        }
    }


@app.get("/api/download")
async def download_media(url: str, filename: str = "pinterest_media", type: str = "image"):
    if not url:
        raise HTTPException(status_code=400, detail="URL required")

    # Validate URL domain
    parsed = urlparse(url)
    allowed = ["pinimg.com", "pinterest.com", "v1.pinimg.com", "v2.pinimg.com"]
    if not any(a in parsed.netloc for a in allowed):
        raise HTTPException(status_code=403, detail="Invalid media URL domain")

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=120)

    ext_map = {"video": "mp4", "gif": "gif", "image": "jpg"}
    ext = ext_map.get(type, "jpg")

    # Clean filename
    safe_filename = re.sub(r'[^\w\-_\.]', '_', filename)[:50]
    download_name = f"{safe_filename}.{ext}"

    content_type_map = {
        "video": "video/mp4",
        "gif": "image/gif",
        "image": "image/jpeg"
    }

    async def stream_content():
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=resp.status, detail="Failed to fetch media")
                async for chunk in resp.content.iter_chunked(65536):  # 64KB chunks for speed
                    yield chunk

    return StreamingResponse(
        stream_content(),
        media_type=content_type_map.get(type, "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        }
    )


@app.get("/api/proxy-image")
async def proxy_image(url: str):
    """Proxy Pinterest images to avoid CORS issues"""
    parsed = urlparse(url)
    if "pinimg.com" not in parsed.netloc and "pinterest.com" not in parsed.netloc:
        raise HTTPException(status_code=403, detail="Invalid domain")

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=15)

    async def stream():
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, headers=HEADERS) as resp:
                async for chunk in resp.content.iter_chunked(32768):
                    yield chunk

    return StreamingResponse(stream(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, workers=4)