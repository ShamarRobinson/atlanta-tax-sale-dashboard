"""Shared helpers for parcel photos: download, verify, shrink and store under data/photos/<slug>/.

A photo entry is a dict: {"src": "data/photos/<slug>/<file>.jpg" (stored copy) or "url": "https://..." (hotlink),
"kind": "assessor" | "listing" | "street" | "aerial", "credit": "who published it", "date": optional "YYYY" or "YYYY-MM-DD",
"page": optional URL of the page the photo came from}.
"""
import hashlib
import io
import re
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "data" / "photos"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
MAX_W = 760


def safe(s):
    return re.sub(r"[^0-9A-Za-z]+", "_", str(s)).strip("_") or "x"


def fetch(url, headers=None, timeout=30):
    """Return (bytes, content_type) or (None, None). Never raises."""
    h = {"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"}
    h.update(headers or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.read(), (r.headers.get("Content-Type") or "").lower()
    except Exception:
        return None, None


def digest(data):
    return hashlib.sha1(data).hexdigest()


def is_image(data, min_bytes=3000, min_side=120):
    """True when the bytes decode as a real picture of a useful size (not a 1x1 pixel or an icon)."""
    if not data or len(data) < min_bytes:
        return False
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        return min(im.size) >= min_side
    except Exception:
        return False


def is_blank(data, tol=6):
    """True for an (almost) single-colour picture, e.g. an empty map tile or a grey 'no image' card."""
    try:
        from PIL import Image, ImageStat
        im = Image.open(io.BytesIO(data)).convert("L").resize((64, 64))
        return ImageStat.Stat(im).stddev[0] < tol
    except Exception:
        return True


def save(slug, parcel, n, data):
    """Shrink to MAX_W wide JPEG and store. Returns the site-relative path, or None if it is not a usable image."""
    if not is_image(data):
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.width > MAX_W:
            im = im.resize((MAX_W, round(im.height * MAX_W / im.width)))
        d = ROOT / slug
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{safe(parcel)}_{n}.jpg"
        im.save(f, "JPEG", quality=68, optimize=True)
        return f"data/photos/{slug}/{f.name}"
    except Exception:
        return None
