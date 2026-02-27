# main.py – PinDrop backend with Paystack webhook + HMAC token verification
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from typing import Optional
from urllib.parse import urlparse, quote

import aiohttp
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PinDrop Video Downloader", version="4.0.0")

# ─── ENV VARS (set these in Railway/Render dashboard) ────────────────────────
# PAYSTACK_SECRET_KEY  → sk_live_xxx  (from Paystack dashboard)
# TOKEN_SECRET         → any long random string you generate (e.g. openssl rand -hex 32)
# FRONTEND_URL         → https://www.pindr.site

PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
TOKEN_SECRET        = os.environ.get("TOKEN_SECRET", "change-this-to-a-long-random-secret")
FRONTEND_URL        = os.environ.get("FRONTEND_URL", "https://www.pindr.site")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "https://www.pindr.site", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.pinterest.com/",
    "Origin": "https://www.pinterest.com",
}

# ─── Token helpers ────────────────────────────────────────────────────────────

def _sign(payload: str) -> str:
    """HMAC-SHA256 signature of payload using TOKEN_SECRET."""
    return hmac.new(
        TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()


def generate_premium_token(email: str, plan: str, reference: str) -> str:
    """
    Creates a signed token in the format:
      base64(json_payload).signature
    The frontend stores this; the backend verifies it on each session restore.
    Lifetime tokens never expire. Monthly tokens expire in 33 days.
    """
    now = int(time.time())
    expiry = 0 if plan == "lifetime" else now + 33 * 24 * 3600  # 0 = never

    payload = {
        "email":     email,
        "plan":      plan,
        "reference": reference,
        "iat":       now,
        "exp":       expiry,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64  = payload_json.encode().hex()          # hex-encode for URL safety
    sig          = _sign(payload_b64)
    return f"{payload_b64}.{sig}"


def verify_premium_token(token: str) -> Optional[dict]:
    """
    Verifies the token signature and expiry.
    Returns the payload dict on success, None on failure.
    """
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        expected_sig = _sign(payload_b64)
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(bytes.fromhex(payload_b64).decode())
        exp = payload.get("exp", 0)
        if exp != 0 and time.time() > exp:
            return None
        return payload
    except Exception:
        return None


# ─── Paystack helpers ─────────────────────────────────────────────────────────

async def verify_paystack_transaction(reference: str) -> Optional[dict]:
    """Call Paystack's verify endpoint and return transaction data if successful."""
    if not PAYSTACK_SECRET_KEY:
        logger.warning("PAYSTACK_SECRET_KEY not set – skipping server-side verification")
        return None
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if resp.status == 200 and data.get("status") and data["data"].get("status") == "success":
                return data["data"]
    return None


# ─── API Models ───────────────────────────────────────────────────────────────

class PinRequest(BaseModel):
    url: str


class VerifyPaymentRequest(BaseModel):
    reference: str
    email: str
    plan: str   # "monthly" | "lifetime"


class VerifyTokenRequest(BaseModel):
    token: str


# ─── Payment endpoints ────────────────────────────────────────────────────────

@app.post("/api/verify-payment")
async def verify_payment(req: VerifyPaymentRequest):
    """
    Called by the frontend after Paystack popup closes successfully.
    1. Verifies the transaction with Paystack's API.
    2. Returns a signed premium token the frontend stores in localStorage.
    """
    if not req.reference or not req.email or req.plan not in ("monthly", "lifetime"):
        raise HTTPException(400, "Invalid request")

    txn = await verify_paystack_transaction(req.reference)

    if txn is None:
        # If secret key not set (dev mode), issue token anyway with a warning
        if not PAYSTACK_SECRET_KEY:
            logger.warning("Dev mode: issuing token without Paystack verification")
        else:
            raise HTTPException(402, "Payment not verified. Please contact support.")

    # Extra guard: make sure amounts match expected values
    if txn:
        expected_amounts = {"monthly": 100, "lifetime": 2900}   # in cents
        paid_amount = txn.get("amount", 0)
        expected    = expected_amounts.get(req.plan, 0)
        if paid_amount < expected:
            raise HTTPException(402, f"Incorrect payment amount: got {paid_amount}, expected {expected}")

    token = generate_premium_token(req.email, req.plan, req.reference)
    logger.info(f"Premium token issued: plan={req.plan} email={req.email} ref={req.reference}")

    return {
        "success": True,
        "token":   token,
        "plan":    req.plan,
        "email":   req.email,
    }


@app.post("/api/verify-token")
async def verify_token(req: VerifyTokenRequest):
    """
    Called on every app load to verify a stored premium token.
    The frontend cannot forge this — it requires our TOKEN_SECRET.
    """
    payload = verify_premium_token(req.token)
    if not payload:
        raise HTTPException(401, "Invalid or expired token")

    return {
        "valid":  True,
        "plan":   payload.get("plan"),
        "email":  payload.get("email"),
        "expiry": payload.get("exp"),
    }


@app.post("/api/paystack/webhook")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None)):
    """
    Paystack sends POST events here for subscription renewals / charges.
    Set this URL in Paystack dashboard → Settings → API Keys & Webhooks.
    URL: https://your-backend.railway.app/api/paystack/webhook
    """
    body = await request.body()

    # Verify webhook signature
    if PAYSTACK_SECRET_KEY and x_paystack_signature:
        expected = hmac.new(
            PAYSTACK_SECRET_KEY.encode(),
            body,
            hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(expected, x_paystack_signature):
            raise HTTPException(400, "Invalid webhook signature")

    try:
        event = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    event_type = event.get("event", "")
    data       = event.get("data", {})

    logger.info(f"Paystack webhook: {event_type}")

    if event_type in ("charge.success", "subscription.create"):
        reference = data.get("reference", "")
        email     = data.get("customer", {}).get("email", "")
        plan_code = data.get("plan", {}).get("plan_code", "") if "plan" in data else ""
        plan      = "monthly" if plan_code else "lifetime"
        logger.info(f"Payment confirmed via webhook: {email} plan={plan} ref={reference}")
        # In a real app with a DB you'd upsert a user record here.
        # Without a DB, the frontend will re-verify via /api/verify-payment.

    elif event_type == "subscription.disable":
        email = data.get("customer", {}).get("email", "")
        logger.info(f"Subscription cancelled: {email}")
        # Token will naturally expire after 33 days. No action needed.

    return {"status": "ok"}


# ─── Existing Pinterest endpoints (unchanged) ─────────────────────────────────

def safe_filename(filename: str, max_bytes: int = 200) -> str:
    safe = re.sub(r'[^\w\-_\. ]', '_', filename).strip()
    if not safe:
        safe = "pinterest_video"
    while len(safe.encode('utf-8')) > max_bytes:
        safe = safe[:-1]
    return safe


def content_disposition_filename(filename: str, as_attachment: bool = True) -> str:
    safe_ascii = safe_filename(filename)
    try:
        safe_ascii.encode('ascii')
        disp = 'attachment' if as_attachment else 'inline'
        return f'{disp}; filename="{safe_ascii}"'
    except UnicodeEncodeError:
        encoded = quote(safe_ascii.encode('utf-8'), safe='')
        disp = 'attachment' if as_attachment else 'inline'
        return f"{disp}; filename*=utf-8''{encoded}"


def extract_pin_id(url: str) -> Optional[str]:
    patterns = [r'pinterest\.com/pin/(\d+)', r'pin\.it/([A-Za-z0-9]+)']
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
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
        url = await resolve_shortlink(url, session)
        pin_id = extract_pin_id(url)

        if not pin_id:
            raise HTTPException(400, "Could not extract Pin ID from URL")

        api_url = (
            f"https://www.pinterest.com/resource/PinResource/get/"
            f"?source_url=/pin/{pin_id}/"
            f"&data=%7B%22options%22%3A%7B%22id%22%3A%22{pin_id}%22%2C%22field_set_key%22%3A%22unauth_react%22%7D%7D"
        )
        try:
            async with session.get(api_url, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"}) as resp:
                if resp.status != 200:
                    raise Exception("API returned non-200")
                data = await resp.json()
                pin_data = data.get("resource_response", {}).get("data", {})
                if not pin_data:
                    raise Exception("No pin data")
                return parse_pin_data(pin_data, pin_id)
        except Exception as e:
            logger.warning(f"API fetch failed: {e}, falling back to HTML scrape")
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    raise HTTPException(422, "Could not fetch pin page")
                html = await resp.text()
                return parse_html_for_video(html, pin_id)


def parse_pin_data(data: dict, pin_id: str) -> dict:
    title = data.get("title", "") or data.get("description", "Pinterest Video") or "Pinterest Video"
    videos = data.get("videos", {})
    video_list = videos.get("video_list", {})
    if not video_list:
        raise HTTPException(422, "This pin does not contain a video")

    best = None
    best_width = 0
    for vdata in video_list.values():
        if isinstance(vdata, dict):
            w = vdata.get("width", 0)
            if w > best_width:
                best_width = w
                best = vdata
    if not best:
        raise HTTPException(422, "No video URL found")

    video_url = best.get("url", "")
    width = best.get("width")
    height = best.get("height")

    thumbnail = ""
    images = data.get("images", {})
    if images:
        orig = images.get("orig", {})
        if orig:
            thumbnail = orig.get("url", "")

    return {
        "id": pin_id, "type": "video", "url": video_url,
        "thumbnail": thumbnail, "title": title[:100],
        "width": width, "height": height,
        "quality": f"{width}x{height}" if width and height else "Original",
    }


def parse_html_for_video(html: str, pin_id: str) -> dict:
    video_patterns = [
        r'"video_url"\s*:\s*"([^"]+\.mp4[^"]*)"',
        r'"url"\s*:\s*"([^"]+\.mp4[^"]*)"',
        r'(https://v\d+\.pinimg\.com/[^"\'\\]+\.mp4[^"\'\\]*)',
    ]
    video_url = None
    for pat in video_patterns:
        m = re.search(pat, html)
        if m:
            video_url = m.group(1).replace('\\/', '/').replace('\\u002F', '/')
            break
    if not video_url:
        raise HTTPException(422, "No video found on this pin")

    thumb_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
    thumbnail = thumb_match.group(1) if thumb_match else ""

    title_match = re.search(r'<title[^>]*>([^<]+)</title>', html)
    title = "Pinterest Video"
    if title_match:
        title = re.sub(r'\s*[|\-–]\s*Pinterest\s*$', '', title_match.group(1)).strip()

    return {
        "id": pin_id, "type": "video", "url": video_url,
        "thumbnail": thumbnail, "title": title[:100],
        "width": None, "height": None, "quality": "Original",
    }


async def get_file_size(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        async with session.head(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            cl = resp.headers.get("Content-Length")
            if cl:
                size_mb = int(cl) / (1024 * 1024)
                return f"{size_mb:.1f} MB" if size_mb >= 1 else f"{int(size_mb * 1024)} KB"
    except:
        pass
    return None


def _is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return any(a in parsed.netloc for a in ["pinimg.com"])


@app.post("/api/analyze")
async def analyze_pin(request: PinRequest):
    url = request.url.strip()
    if not url:
        raise HTTPException(400, "URL required")
    if "pinterest" not in url.lower() and "pin.it" not in url.lower():
        raise HTTPException(400, "Please provide a valid Pinterest URL")

    try:
        data = await fetch_pin_data(url)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(500, "Failed to process Pinterest URL")

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        size = await get_file_size(data["url"], session)

    return {"success": True, "media": {**data, "size": size}}


@app.get("/api/preview-video")
async def preview_video(url: str, request: Request):
    if not url:
        raise HTTPException(400, "URL required")
    if not _is_allowed_url(url):
        raise HTTPException(403, "Invalid media URL domain")

    range_header = request.headers.get("Range")
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=120)

    upstream_headers = {**HEADERS}
    if range_header:
        upstream_headers["Range"] = range_header

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.get(url, headers=upstream_headers) as resp:
            if resp.status not in (200, 206):
                raise HTTPException(resp.status, "Failed to fetch video from Pinterest")

            content_type   = resp.headers.get("Content-Type", "video/mp4")
            content_length = resp.headers.get("Content-Length")
            content_range  = resp.headers.get("Content-Range")
            accept_ranges  = resp.headers.get("Accept-Ranges", "bytes")

            response_headers = {
                "Accept-Ranges": accept_ranges,
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            }
            if content_length: response_headers["Content-Length"] = content_length
            if content_range:  response_headers["Content-Range"]  = content_range

            body = await resp.read()

    status_code = 206 if (range_header and content_range) else 200
    return Response(content=body, status_code=status_code,
                    media_type=content_type, headers=response_headers)


@app.get("/api/download")
async def download_media(url: str, filename: str = "pinterest_video"):
    if not url:
        raise HTTPException(400, "URL required")
    if not _is_allowed_url(url):
        raise HTTPException(403, "Invalid media URL domain")

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=180)
    safe_base = safe_filename(filename)
    content_disp = content_disposition_filename(f"{safe_base}.mp4", as_attachment=True)

    async def stream_download():
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    raise HTTPException(resp.status, "Failed to fetch video")
                async for chunk in resp.content.iter_chunked(65536):
                    yield chunk

    return StreamingResponse(stream_download(), media_type="video/mp4",
                             headers={"Content-Disposition": content_disp, "Cache-Control": "no-cache"})


@app.get("/api/proxy-image")
async def proxy_image(url: str):
    parsed = urlparse(url)
    if "pinimg.com" not in parsed.netloc:
        raise HTTPException(403, "Invalid domain")

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        try:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    raise HTTPException(resp.status, "Failed to fetch image")
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                body = await resp.read()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))

    return Response(content=body, media_type=content_type,
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/health")
async def health():
    return {"status": "ok", "version": "4.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)