"""Image preparation for the Analyst — the only place images are touched before the model.

Downscales to ``IMAGE_MAX_EDGE`` on the long edge and re-encodes JPEG at ``IMAGE_JPEG_QUALITY``
(cost discipline: smaller images, cheaper vision calls). Returns raw JPEG bytes ready to hand
to Gemini.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from daylook import config


def prepare_image(path: Path) -> bytes:
    """Load, downscale (long edge <= IMAGE_MAX_EDGE), and JPEG-encode. Returns bytes."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        long_edge = max(im.size)
        if long_edge > config.IMAGE_MAX_EDGE:
            scale = config.IMAGE_MAX_EDGE / long_edge
            new_size = (round(im.width * scale), round(im.height * scale))
            im = im.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=config.IMAGE_JPEG_QUALITY)
        return buf.getvalue()
