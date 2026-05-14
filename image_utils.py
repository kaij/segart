from pathlib import Path

from PIL import Image, ImageFilter, ImageOps


def clamp_image_to_max_dim(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    scale = max_dim / float(max(w, h))
    return img.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.LANCZOS)


def crop_scanner_border(img: Image.Image, tolerance: int = 30, fuzz: float = 0.3) -> Image.Image:
    """Crop to content by scanning inward until paper color is reached.

    Finds the dominant bright pixel (paper/background) from the histogram,
    then scans inward from each edge until a column/row has >= fuzz fraction
    of pixels within tolerance of that paper color. Stops at midpoint to
    prevent over-cropping fully dark images.
    """
    gray = img.convert("L")
    px = list(gray.getdata())
    w, h = gray.size

    hist = gray.histogram()
    paper_color = max(range(120, 256), key=lambda v: hist[v])

    def _col(x):
        return [px[y * w + x] for y in range(h)]

    def _row(y):
        return px[y * w: (y + 1) * w]

    def _has_paper_col(x):
        return sum(1 for p in _col(x) if abs(p - paper_color) <= tolerance) / h >= fuzz

    def _has_paper_row(y):
        return sum(1 for p in _row(y) if abs(p - paper_color) <= tolerance) / w >= fuzz

    left = 0
    while left < w // 2 and not _has_paper_col(left):
        left += 1

    right = w - 1
    while right > w // 2 and not _has_paper_col(right):
        right -= 1

    top = 0
    while top < h // 2 and not _has_paper_row(top):
        top += 1

    bottom = h - 1
    while bottom > h // 2 and not _has_paper_row(bottom):
        bottom -= 1

    if left == 0 and top == 0 and right == w - 1 and bottom == h - 1:
        return img
    return img.crop((left, top, right + 1, bottom + 1))


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
