"""One-shot: GIF.gif -> transparent looping WebP for the desk pet."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageSequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "GIF.gif"
DST = ROOT / "assets" / "character_anim.webp"
MAX_SIDE = 560
DURATION_MS = 100


def chroma_alpha(rgb: np.ndarray) -> np.ndarray:
    """Knock out the solid blue notebook background; keep hair and outlines."""
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    dist = np.sqrt((r - 21.0) ** 2 + (g - 61.0) ** 2 + (b - 151.0) ** 2)
    is_blue = (b > r + 25) & (b > g + 15) & (r < 90)
    alpha = np.clip((dist - 28.0) / 22.0, 0.0, 1.0)
    alpha = np.where(is_blue, 0.0, alpha)
    return (alpha * 255.0).astype(np.uint8)


def wipe_ui(alpha: np.ndarray) -> None:
    """Clear Bilibili chrome and notebook rings (mutates alpha)."""
    h, w = alpha.shape
    alpha[:70, :] = 0
    alpha[:, :140] = 0
    alpha[:110, :400] = 0


def main() -> None:
    im = Image.open(SRC)
    frames_rgba: list[Image.Image] = []
    crop: tuple[int, int, int, int] | None = None

    for i, fr in enumerate(ImageSequence.Iterator(im)):
        rgb = np.array(fr.convert("RGB"))
        alpha = chroma_alpha(rgb)
        wipe_ui(alpha)
        if crop is None:
            ys, xs = np.where(alpha > 16)
            if len(xs) == 0:
                raise RuntimeError("chroma key removed the whole frame")
            pad = 4
            h, w = alpha.shape
            crop = (
                max(0, int(xs.min()) - pad),
                max(0, int(ys.min()) - pad),
                min(w, int(xs.max()) + pad + 1),
                min(h, int(ys.max()) + pad + 1),
            )
            print(f"crop {crop}  src={w}x{h}")
        x0, y0, x1, y1 = crop
        rgba = np.dstack([rgb[y0:y1, x0:x1], alpha[y0:y1, x0:x1]])
        img = Image.fromarray(rgba, "RGBA")
        tw, th = img.size
        scale = min(1.0, MAX_SIDE / max(tw, th))
        if scale < 1.0:
            img = img.resize((max(1, int(tw * scale)), max(1, int(th * scale))), Image.Resampling.LANCZOS)
        frames_rgba.append(img)
        if i % 20 == 0:
            print(f"frame {i}/{im.n_frames} size={img.size}")

    print(f"encoding {len(frames_rgba)} frames -> {DST}")
    frames_rgba[0].save(
        DST,
        format="WEBP",
        save_all=True,
        append_images=frames_rgba[1:],
        duration=DURATION_MS,
        loop=0,
        lossless=False,
        quality=82,
        method=4,
    )
    print(f"done {DST} bytes={DST.stat().st_size}")


if __name__ == "__main__":
    main()
