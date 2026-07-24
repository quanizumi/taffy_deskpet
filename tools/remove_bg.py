"""Fallback background removal using OpenCV GrabCut + edge refine."""
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def remove_bg_grabcut(src: Path, dst: Path) -> None:
    bgr = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError(f"Cannot read {src}")
    h, w = bgr.shape[:2]

    # Rough foreground rect (character is centered)
    margin_x = int(w * 0.08)
    margin_y = int(h * 0.04)
    rect = (margin_x, margin_y, w - 2 * margin_x, h - 2 * margin_y)

    mask = np.zeros((h, w), np.uint8)
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    cv2.grabCut(bgr, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)

    # Seed sure-background near borders (classroom BG)
    border = 6
    mask[:border, :] = cv2.GC_BGD
    mask[-border:, :] = cv2.GC_BGD
    mask[:, :border] = cv2.GC_BGD
    mask[:, -border:] = cv2.GC_BGD
    cv2.grabCut(bgr, mask, None, bgd, fgd, 3, cv2.GC_INIT_WITH_MASK)

    binary = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)

    # Keep largest connected component
    num, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if num > 1:
        largest = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
        binary = np.where(labels == largest, 255, 0).astype(np.uint8)

    # Soften alpha
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    alpha = cv2.GaussianBlur(binary, (5, 5), 0)

    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = alpha

    # Crop to content
    ys, xs = np.where(alpha > 16)
    if len(xs) == 0:
        raise RuntimeError("No foreground found")
    pad = 8
    x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + pad + 1)
    y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + pad + 1)
    cropped = rgba[y0:y1, x0:x1]

    Image.fromarray(cv2.cvtColor(cropped, cv2.COLOR_BGRA2RGBA)).save(dst)
    print(f"Saved {dst} size=({cropped.shape[1]}, {cropped.shape[0]})")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    remove_bg_grabcut(root / "assets" / "character_raw.png", root / "assets" / "character.png")
