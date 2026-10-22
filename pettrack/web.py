"""Request-scoped helpers shared by the blueprints."""
from __future__ import annotations

import io

from flask import current_app, g

from . import db

MAX_PHOTO_BYTES = 8 * 1024 * 1024


def cfg():
    return current_app.config["PT"]


def conn():
    if "db" not in g:
        g.db = db.connect(cfg().db_path)
    return g.db


def hw():
    return current_app.extensions["pt_hw"]


def sync():
    return current_app.extensions["pt_sync"]


def close_conn(_exc=None) -> None:
    c = g.pop("db", None)
    if c is not None:
        c.close()


def process_photo(fs) -> tuple[bytes, str]:
    """Validate, strip EXIF, shrink. Raises ValueError with a user-facing message."""
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    data = fs.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError("Photo is larger than 8 MB")
    try:
        Image.open(io.BytesIO(data)).verify()
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception:
        raise ValueError("That file is not a valid image (use JPG, PNG or WebP)") from None
    img.thumbnail((800, 800))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue(), "image/jpeg"


def public_base() -> str:
    """URL finders should open: public URL > cloud copy (from a laptop) > this host."""
    from flask import request
    c = cfg()
    return c.public_url or c.cloud_url or request.host_url.rstrip("/")
