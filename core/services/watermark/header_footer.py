"""Header and footer image composition for the watermark engine."""

import os
from typing import List, Optional

from PIL import Image

from core.services.watermark.common import _RESAMPLE_LANCZOS, _safe_close


def compose_with_header_footer(
    base_img: Image.Image,
    header_images: List[str],
    footer_images: List[str],
) -> Optional[Image.Image]:
    """Return a new RGB image with header/footer composed, or None when no assets are valid."""
    width = base_img.width
    total_header_height = 0
    total_footer_height = 0
    header_imgs: List[Image.Image] = []
    footer_imgs: List[Image.Image] = []

    try:
        for header_path in header_images:
            if not os.path.exists(header_path):
                continue
            with Image.open(header_path) as header_img:
                if header_img.width != width:
                    new_height = int(header_img.height * (width / header_img.width))
                    prepared = header_img.resize(
                        (width, new_height), _RESAMPLE_LANCZOS
                    )
                else:
                    prepared = header_img.copy()
                prepared = prepared.convert("RGB")
                header_imgs.append(prepared)
                total_header_height += prepared.height

        for footer_path in footer_images:
            if not os.path.exists(footer_path):
                continue
            with Image.open(footer_path) as footer_img:
                if footer_img.width != width:
                    new_height = int(footer_img.height * (width / footer_img.width))
                    prepared = footer_img.resize(
                        (width, new_height), _RESAMPLE_LANCZOS
                    )
                else:
                    prepared = footer_img.copy()
                prepared = prepared.convert("RGB")
                footer_imgs.append(prepared)
                total_footer_height += prepared.height

        if not header_imgs and not footer_imgs:
            return None

        base_rgb = base_img.convert("RGB")
        new_height = base_rgb.height + total_header_height + total_footer_height
        new_img = Image.new("RGB", (width, new_height))
        y_offset = 0

        for header_img in header_imgs:
            new_img.paste(header_img, (0, y_offset))
            y_offset += header_img.height

        new_img.paste(base_rgb, (0, y_offset))
        y_offset += base_rgb.height

        for footer_img in footer_imgs:
            new_img.paste(footer_img, (0, y_offset))
            y_offset += footer_img.height

        _safe_close(base_rgb)
        return new_img
    finally:
        for img in header_imgs + footer_imgs:
            _safe_close(img)
