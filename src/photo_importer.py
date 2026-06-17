"""Process courthouse phone photos into NoticeData records.

Applies OpenCV preprocessing (EXIF rotation, blur check, perspective
correction, CLAHE, adaptive threshold) then Tesseract OCR (PSM 6) and
Claude Haiku LLM parsing to extract structured fields.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

import config
from image_utils import ocr_page
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png"}

# Max dimension for resize before OCR — phone photos are 2-8MB
MAX_IMAGE_DIM = 2000


class BlurryImageError(Exception):
    """Raised when a photo's Laplacian variance is below BLUR_THRESHOLD."""
    pass


def _order_corners(pts):
    """Order 4 points as: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]   # top-left has smallest sum
    rect[2] = pts[np.argmax(s)]   # bottom-right has largest sum
    d = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(d)]   # top-right has smallest difference
    rect[3] = pts[np.argmax(d)]   # bottom-left has largest difference
    return rect


def preprocess_image(image: Image.Image, correct_perspective: bool = True) -> Image.Image:
    """Apply full OpenCV preprocessing chain to a phone photo.

    Steps: EXIF rotation → resize → blur check → grayscale → bilateral filter
    (removes moire from terminal screens) → perspective correction → Otsu threshold.

    Raises BlurryImageError if Laplacian variance is below BLUR_THRESHOLD.
    """
    # 1. EXIF rotation — phone cameras embed orientation in metadata
    img = ImageOps.exif_transpose(image)
    logger.debug("  EXIF transpose applied, size: %s", img.size)

    # 2. Resize to max dimension (preserving aspect ratio)
    w, h = img.size
    if max(w, h) > MAX_IMAGE_DIM:
        scale = MAX_IMAGE_DIM / max(w, h)
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        logger.debug("  Resized to %dx%d", new_w, new_h)

    # 3. Convert to OpenCV (PIL RGB → OpenCV BGR)
    img_cv = np.array(img)
    img_cv = cv2.cvtColor(img_cv, cv2.COLOR_RGB2BGR)

    # 4. Blur check — compute Laplacian variance on grayscale
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    logger.debug("  Laplacian variance: %.1f (threshold: %d)", lap_var, config.BLUR_THRESHOLD)
    if lap_var < config.BLUR_THRESHOLD:
        raise BlurryImageError(
            f"Laplacian variance {lap_var:.1f} < threshold {config.BLUR_THRESHOLD}"
        )

    # 5. Bilateral filter — removes moire/noise from terminal screens while
    # preserving text edges. Critical for courthouse terminal photos where
    # screen refresh patterns and blue/gray backgrounds confuse Tesseract.
    filtered = cv2.bilateralFilter(gray, 15, 75, 75)
    logger.debug("  Bilateral filter applied (d=15, sigmaColor=75, sigmaSpace=75)")

    # 6. Perspective correction (optional) — uses edges from filtered image
    if correct_perspective:
        blurred = cv2.GaussianBlur(filtered, (5, 5), 0)
        edges = cv2.Canny(blurred, 75, 200)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
        image_area = gray.shape[0] * gray.shape[1]
        warped = False

        for c in contours:
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) == 4:
                contour_area = cv2.contourArea(approx)
                if contour_area >= 0.60 * image_area:
                    pts = approx.reshape(4, 2).astype("float32")
                    rect = _order_corners(pts)
                    widthA = np.linalg.norm(rect[2] - rect[3])
                    widthB = np.linalg.norm(rect[1] - rect[0])
                    maxW = int(max(widthA, widthB))
                    heightA = np.linalg.norm(rect[1] - rect[2])
                    heightB = np.linalg.norm(rect[0] - rect[3])
                    maxH = int(max(heightA, heightB))
                    dst = np.array([
                        [0, 0], [maxW - 1, 0],
                        [maxW - 1, maxH - 1], [0, maxH - 1]
                    ], dtype="float32")
                    M = cv2.getPerspectiveTransform(rect, dst)
                    result = cv2.warpPerspective(img_cv, M, (maxW, maxH))
                    filtered = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)
                    filtered = cv2.bilateralFilter(filtered, 15, 75, 75)
                    logger.debug("  Applied perspective correction (contour area %.0f%% of image)",
                                 contour_area / image_area * 100)
                    warped = True
                    break

        if not warped:
            logger.debug("  No document contour found — skipping perspective correction")

    # 7. Otsu threshold — auto-determines optimal binary threshold
    _, binary = cv2.threshold(filtered, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return Image.fromarray(binary)


def process_photos(
    folder: Path,
    county: str,
    notice_type: str,
    date_added: str | None = None,
    api_key: str | None = None,
    correct_perspective: bool = True,
) -> list[NoticeData]:
    """Process a folder of courthouse phone photos into NoticeData records.

    Args:
        folder: Path to folder containing JPG/PNG photos.
        county: "Knox" or "Blount".
        notice_type: e.g. "eviction", "foreclosure", "tax_sale".
        date_added: Date string (YYYY-MM-DD). Defaults to today.
        api_key: Anthropic API key for LLM parsing.
        correct_perspective: Whether to attempt perspective correction.

    Returns:
        List of NoticeData objects ready for enrichment.
    """
    date_str = date_added or config.run_date()

    # Collect image files
    image_files = sorted(
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in VALID_EXTENSIONS
    )

    if not image_files:
        logger.warning("No image files found in %s", folder)
        return []

    logger.info("Photo import: %d images found in %s", len(image_files), folder)

    notices = []
    rejected = []
    skipped = 0

    for path in image_files:
        logger.info("Processing %s", path.name)

        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:
            logger.warning("Failed to open %s: %s", path.name, e)
            skipped += 1
            continue

        # Note: skip fix_rotation (OSD) for phone photos — EXIF transpose in
        # preprocess_image handles rotation. OSD on raw phone photos often fails
        # and the 270° fallback incorrectly rotates correct images.

        # Full OpenCV preprocessing
        try:
            preprocessed = preprocess_image(img, correct_perspective=correct_perspective)
        except BlurryImageError as e:
            logger.warning("SKIPPED (blurry): %s — %s", path.name, e)
            rejected.append(path.name)
            continue

        # Tesseract OCR with PSM 6 (single uniform block)
        ocr_text = ocr_page(preprocessed, psm=config.TESSERACT_PSM_PHOTO)

        if not ocr_text or len(ocr_text.strip()) < 20:
            logger.warning("OCR produced insufficient text for %s (%d chars)",
                           path.name, len(ocr_text.strip()) if ocr_text else 0)
            skipped += 1
            continue

        # LLM parsing via existing llm_parser
        parsed = {}
        if api_key:
            try:
                from llm_parser import extract_with_llm
                parsed = asyncio.run(extract_with_llm(
                    raw_text=ocr_text,
                    notice_type=notice_type,
                    county=county,
                    api_key=api_key,
                ))
            except Exception as e:
                logger.warning("LLM parsing failed for %s: %s", path.name, e)

        # Build NoticeData from parsed fields
        notice = NoticeData(
            address=parsed.get("address", ""),
            city=parsed.get("city", ""),
            state=parsed.get("state", "TN"),
            zip=parsed.get("zip", ""),
            owner_name=parsed.get("owner_name", ""),
            notice_type=notice_type,
            county=county,
            date_added=date_str,
            raw_text=ocr_text[:8000],
            source_url=f"photo:{path.name}",
        )

        # Copy any extra probate fields if present
        for field in ("decedent_name",):
            if field in parsed:
                setattr(notice, field, parsed[field])

        notices.append(notice)

    logger.info(
        "Photo import: %d processed, %d rejected (blurry), %d skipped, %d records extracted",
        len(image_files), len(rejected), skipped, len(notices),
    )
    if rejected:
        logger.info("Rejected files (re-capture needed): %s", ", ".join(rejected))

    return notices
