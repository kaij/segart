from pathlib import Path

from PIL import Image, ImageFilter, ImageOps


def clamp_image_to_max_dim(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    scale = max_dim / float(max(w, h))
    return img.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.LANCZOS)


def crop_scanner_border(img: Image.Image, tolerance: int = 15) -> Image.Image:
    """Detect border color from corners (median), crop to content bounding box."""
    gray = img.convert("L")
    w, h = gray.size
    sample = max(5, min(w, h) // 30)
    corner_pixels: list[int] = []
    for box in [(0, 0, sample, sample), (w - sample, 0, w, sample), (0, h - sample, sample, h), (w - sample, h - sample, w, h)]:
        corner_pixels.extend(gray.crop(box).getdata())
    border_color = sorted(corner_pixels)[len(corner_pixels) // 2]
    mask = gray.point(lambda p: 255 if abs(p - border_color) > tolerance else 0)
    bbox = mask.getbbox()
    return img.crop(bbox) if bbox else img


def optimize_microfilm(img: Image.Image, max_dim: int = 1200) -> Image.Image:
    """Autocontrast, remove black borders, sharpen, and resize."""
    img = img.convert("RGB")
    img = ImageOps.autocontrast(img, cutoff=10, ignore=2)
    img = crop_scanner_border(img)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=3))
    return clamp_image_to_max_dim(img, max_dim)


def save_image(img: Image.Image, path: Path, fmt: str = "webp", quality: int = 90) -> None:
    if fmt == "webp":
        img.save(path, format="WEBP", quality=quality, method=4)
    else:
        img.save(path, format="JPEG", quality=quality, optimize=True)
