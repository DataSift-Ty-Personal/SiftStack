"""Generate a 1-page disposition flyer PDF and upload to Google Drive.

Pulls property data from Jefferson County PVA (beds prompt + baths/sqft/
acreage/year_built auto), downloads photos from a Drive property folder,
and produces a buyer-facing flyer with hero image, key stats,
asking/ARV numbers, and a clickable "View All Photos" link.

Drive folder layout:
    <REDNOUR_DRIVE_PARENT_FOLDER_ID>/
        1521 Sale Ave/
            Photos/
                01_front.jpg
                02_kitchen.jpg
                ...
            (the generated PDF lands here after run)
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import config
import kentucky_pva_lookup as pva

logger = logging.getLogger(__name__)


# ── Brand palette ─────────────────────────────────────────────────────
RED_BRAND = colors.HexColor("#C8102E")
DARK = colors.HexColor("#1a1a2e")
MUTED = colors.HexColor("#7f8c8d")
LIGHT_BG = colors.HexColor("#f5f6fa")
BORDER = colors.HexColor("#dcdde1")
WHITE = colors.white


@dataclass
class FlyerData:
    address: str
    city: str
    state: str
    zip_code: str
    bedrooms: int
    bathrooms: float
    sqft: int
    sqft_basement: int   # finished basement sqft (sub-label only)
    acreage: float
    year_built: str
    # Accept either a number (formatted as $XXX,XXX) or freeform text like
    # "Taking All Offers" / "$360,000+" / "Make Offer" — wholesalers often
    # use phrases for asking price and ranges for ARV.
    asking_price: int | str
    arv: int | str
    photos_link: str
    additional_info: str = ""  # ";" or newline-separated bullets
    hero_image_bytes: bytes | None = None


# ── Formatting helpers ────────────────────────────────────────────────


def _format_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return raw


def _format_baths(b: float) -> str:
    if not b:
        return "—"
    s = f"{b:.1f}".rstrip("0").rstrip(".")
    return s


def _safe_int(s: str) -> int:
    if not s:
        return 0
    cleaned = re.sub(r"[^\d-]", "", s)
    if not cleaned or cleaned == "-":
        return 0
    try:
        return int(cleaned)
    except ValueError:
        return 0


def _safe_float(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except ValueError:
        return 0.0


# Register HEIC/HEIF decoder so iPhone photos (.HEIC) work via Pillow.
# Property runners commonly upload straight from iPhone with no conversion.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    logger.warning(
        "pillow-heif not installed — HEIC photos won't decode. "
        "Run: pip install pillow-heif"
    )


def _normalize_image(image_bytes: bytes) -> bytes:
    """Apply EXIF orientation so phone photos display upright in the PDF
    and convert HEIC/HEIF to JPEG so reportlab can embed them.

    reportlab does not honor EXIF orientation tags, so portrait photos
    taken on a phone end up rotated unless we bake the rotation into
    the pixels first. reportlab also can't read HEIC at all — we re-encode
    to JPEG here.
    """
    try:
        from PIL import Image as PILImage, ImageOps
        img = PILImage.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue()
    except Exception:
        logger.exception("EXIF normalize failed — using raw bytes")
        return image_bytes


# ── PVA fetch ─────────────────────────────────────────────────────────


def _basement_finished_or_zero(detail: dict[str, str]) -> int:
    raw = detail.get("Area:Basement:Finished", "") or ""
    return _safe_int(raw)


def _main_unit_finished(detail: dict[str, str]) -> int:
    """Headline sqft = Main Unit Finished, falling back to Gross if Finished is dashed."""
    fin = detail.get("Area:Main Unit:Finished", "") or ""
    val = _safe_int(fin)
    if val:
        return val
    gross = detail.get("Area:Main Unit:Gross", "") or ""
    return _safe_int(gross)


def fetch_pva(address: str) -> dict[str, object]:
    """Look up an address on Jefferson PVA and return parsed flyer fields.

    Returns dict with keys: bathrooms, acreage, year_built, sqft,
    sqft_basement, owner, parcel_id, mailing_address. Empty dict on any
    failure (auth, network timeout, no match, parse error) — the caller
    is expected to fall back to CLI flags or interactive prompts.

    NOTE: the PVA plan permits one concurrent session, so this WILL kick
    you out of any active jeffersonpva.ky.gov browser tab.
    """
    try:
        session = pva._make_session()
    except Exception:
        logger.exception("PVA session setup failed")
        return {}

    try:
        if not pva._login(session):
            logger.error("PVA login failed — check PVA_EMAIL / PVA_PASSWORD")
            return {}
    except Exception:
        logger.exception("PVA login raised — likely network timeout")
        return {}

    try:
        # Long streets exceed one page (Sale Ave has 205 properties),
        # so widen the pagination cap.
        rows = pva.search_by_address(session, address, max_pages=12)
        if not rows:
            logger.error("PVA returned no rows for %r", address)
            return {}

        norm = pva._normalize_street_address(address)
        target_house = pva._house_number(norm)
        match = None
        for r in rows:
            row_norm = pva._normalize_street_address(r.address)
            if row_norm and pva._house_number(row_norm) == target_house:
                match = r
                break

        if not match:
            logger.error(
                "No PVA row matched house number %r. First few results: %s",
                target_house, [r.address for r in rows[:5]],
            )
            return {}

        detail = pva.get_detail(session, match.lrsn)

        full = _safe_int(detail.get("Full Bathrooms", ""))
        half = _safe_int(detail.get("Half Bathrooms", ""))
        baths = float(full) + 0.5 * float(half)

        return {
            "bathrooms": baths,
            "acreage": _safe_float(detail.get("Approximate Acreage", "")),
            "year_built": detail.get("Year Built", "").strip(),
            "sqft": _main_unit_finished(detail),
            "sqft_basement": _basement_finished_or_zero(detail),
            "owner": detail.get("Owner", ""),
            "parcel_id": detail.get("Parcel ID", ""),
            "mailing_address": detail.get("Mailing Address", ""),
            "matched_address": match.address,
        }
    except Exception:
        logger.exception("PVA lookup raised — likely network or parse failure")
        return {}
    finally:
        try:
            pva._logout(session)
        except Exception:
            pass


# ── Interactive prompts (only used for fields PVA didn't supply) ──────


def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{question}{suffix}: ").strip()
    return val or default


def _prompt_int(question: str, default: int = 0) -> int:
    while True:
        raw = _prompt(question, str(default) if default else "")
        if not raw:
            return default
        try:
            return int(re.sub(r"[^\d-]", "", raw) or "0")
        except ValueError:
            print("  Not a number. Try again.")


def _prompt_float(question: str, default: float = 0.0) -> float:
    while True:
        raw = _prompt(question, f"{default:.1f}" if default else "")
        if not raw:
            return default
        try:
            return float(re.sub(r"[^\d.]", "", raw) or "0")
        except ValueError:
            print("  Not a number. Try again.")


# ── Drive helpers ─────────────────────────────────────────────────────


def _drive_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    key_b64 = config.GOOGLE_SERVICE_ACCOUNT_KEY
    if not key_b64:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_KEY not set — add a base64-encoded "
            "service account JSON key to .env"
        )
    creds = Credentials.from_service_account_info(
        json.loads(base64.b64decode(key_b64)),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# Drive API calls need these flags to traverse Shared Drives (Team Drives).
# Without them, the parent folder + everything inside is invisible to a
# service account even when explicitly shared. Standard "My Drive" folders
# work either way, so it's safe to set unconditionally.
_SHARED_DRIVE_KW = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}


def _find_subfolder(service, parent_id: str, name: str) -> str | None:
    """Find a child folder by case-insensitive name match. Tries exact match,
    then loose substring match. Returns folder ID or None."""
    q = (
        f"'{parent_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    res = service.files().list(
        q=q, fields="files(id,name)", pageSize=500, **_SHARED_DRIVE_KW,
    ).execute()
    files = res.get("files", [])
    target = name.strip().lower()

    for f in files:
        if f["name"].strip().lower() == target:
            return f["id"]
    for f in files:
        if target in f["name"].strip().lower():
            return f["id"]
    return None


def _list_images(service, folder_id: str) -> list[dict]:
    q = (
        f"'{folder_id}' in parents and "
        "mimeType contains 'image/' and trashed=false"
    )
    res = service.files().list(
        q=q, fields="files(id,name,mimeType)", pageSize=500, orderBy="name",
        **_SHARED_DRIVE_KW,
    ).execute()
    return res.get("files", [])


def _download_image(service, file_id: str) -> bytes:
    return service.files().get_media(
        fileId=file_id, supportsAllDrives=True,
    ).execute()


def _folder_link(service, folder_id: str) -> str:
    res = service.files().get(
        fileId=folder_id, fields="webViewLink", supportsAllDrives=True,
    ).execute()
    return res.get("webViewLink", "")


def fetch_drive_assets(
    address: str, parent_folder_id: str,
) -> tuple[bytes | None, str, str]:
    """Locate the property folder by address, download the first image
    (alphabetically), get a shareable link to wherever the photos live.

    Image discovery: prefer a ``Photos/`` subfolder (cleaner separation
    from PDFs/docs). If absent, scan the property folder directly —
    iPhone-uploaded photos often sit at the property root.

    Returns (hero_bytes, photos_webViewLink, property_folder_id).
    """
    service = _drive_service()
    prop_id = _find_subfolder(service, parent_folder_id, address)
    if not prop_id:
        logger.error(
            "Drive: no folder named %r under parent %s. Create the property "
            "folder before running.",
            address, parent_folder_id,
        )
        return None, "", ""

    photos_id = _find_subfolder(service, prop_id, "Photos")
    if photos_id:
        scan_id = photos_id
        link_id = photos_id
    else:
        logger.info(
            "Drive: no 'Photos' subfolder for %r — scanning property folder directly",
            address,
        )
        scan_id = prop_id
        link_id = prop_id

    images = _list_images(service, scan_id)
    if not images:
        logger.warning("Drive: no images found in %r", address)
        return None, _folder_link(service, link_id), prop_id

    hero = _normalize_image(_download_image(service, images[0]["id"]))
    return hero, _folder_link(service, link_id), prop_id


def _trash_existing_flyers(service, folder_id: str, filename: str) -> int:
    """Move any prior flyers with the same name to Drive trash.

    Without this, every re-run accumulates a new copy in the property folder
    (Drive does not dedupe by name). Buyers should see one definitive PDF.
    Returns the count of files trashed (0 on first upload, 1+ on re-runs).
    """
    safe = filename.replace("'", r"\'")
    res = service.files().list(
        q=(f"'{folder_id}' in parents and name='{safe}' and trashed=false"),
        fields="files(id,name)",
        **_SHARED_DRIVE_KW,
    ).execute()
    existing = res.get("files", [])
    for f in existing:
        service.files().update(
            fileId=f["id"], body={"trashed": True}, supportsAllDrives=True,
        ).execute()
        logger.info("Trashed prior flyer %r (id=%s)", f["name"], f["id"])
    return len(existing)


def upload_pdf(local_path: Path, folder_id: str) -> str:
    from drive_uploader import upload_file
    try:
        n = _trash_existing_flyers(_drive_service(), folder_id, local_path.name)
        if n:
            print(f"      Replaced {n} prior flyer(s) in the folder")
    except Exception as e:
        # Most common cause: the service account has Contributor (default
        # mapping when you share a Shared Drive folder as Editor), which
        # cannot delete even files it created. Upgrade to Content Manager
        # to enable cleanup. Upload itself doesn't need this — it works
        # with Contributor — so we keep going.
        msg = str(e)
        if "insufficientFilePermissions" in msg or "403" in msg:
            print(
                "      WARNING: cannot replace prior flyer (service account "
                "lacks delete permission on Shared Drive). "
                "Upgrade the bot's role to 'Content Manager' to enable auto-cleanup."
            )
        else:
            logger.exception("Could not clean up prior flyers — continuing anyway")
    return upload_file(
        local_path, folder_id, config.GOOGLE_SERVICE_ACCOUNT_KEY,
    ) or ""


# ── PDF builder ───────────────────────────────────────────────────────

PAGE_W, PAGE_H = letter
MARGIN = 0.4 * inch
CONTENT_W = PAGE_W - 2 * MARGIN  # 540pt at 0.4" margins


def _logo_or_text():
    """Logo Image flowable if assets/rednour_logo.png exists, else text fallback."""
    path = config.COMPANY_LOGO_PATH
    if path.exists():
        try:
            return Image(str(path), width=110, height=55, kind="proportional")
        except Exception:
            logger.warning("Could not load logo at %s", path)
    style = ParagraphStyle(
        "LogoText", fontName="Helvetica-Bold", fontSize=16,
        textColor=RED_BRAND, alignment=TA_LEFT, leading=18,
    )
    return Paragraph("REDNOUR", style)


def _stat_box(label: str, value: str, sub: str = "") -> Table:
    label_s = ParagraphStyle(
        "StatL", fontName="Helvetica", fontSize=8, textColor=MUTED,
        alignment=TA_CENTER, leading=10,
    )
    value_s = ParagraphStyle(
        "StatV", fontName="Helvetica-Bold", fontSize=20, textColor=DARK,
        alignment=TA_CENTER, leading=22,
    )
    sub_s = ParagraphStyle(
        "StatS", fontName="Helvetica", fontSize=7, textColor=MUTED,
        alignment=TA_CENTER, leading=9,
    )
    rows: list[list] = [
        [Paragraph(label.upper(), label_s)],
        [Paragraph(value, value_s)],
    ]
    if sub:
        rows.append([Paragraph(sub, sub_s)])
    t = Table(rows, colWidths=[CONTENT_W / 4 - 6])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _format_money(amount: int | str) -> tuple[str, int]:
    """Render asking/ARV value. Returns (display_string, font_size).

    - Pure digits → ``$XXX,XXX`` with the headline font size.
    - Already-formatted strings ("$360,000+", "$1.2M") → verbatim.
    - Freeform text ("Taking All Offers", "Make Offer") → verbatim with
      auto-shrunk font so it fits the box.
    """
    if isinstance(amount, (int, float)):
        if amount > 0:
            return f"${amount:,.0f}", 28
        return "—", 28

    s = str(amount).strip()
    if not s:
        return "—", 28

    # Bare digits (with optional commas/dots) → format as currency
    digits_only = re.sub(r"[,$\s]", "", s)
    if re.fullmatch(r"\d+(\.\d+)?", digits_only):
        return f"${float(digits_only):,.0f}", 28

    # Otherwise pass through verbatim, shrinking for long strings
    if len(s) > 18:
        return s, 14
    if len(s) > 12:
        return s, 18
    return s, 22


def _parse_info_items(raw: str) -> list[str]:
    """Split a freeform additional-info string into bullet items.

    Accepts ``;``, ``\\n``, or ``|`` as separators so callers can use
    whichever is convenient (semicolons are easiest from a CLI flag).
    """
    if not raw:
        return []
    parts = re.split(r"[;\n|]+", raw)
    return [p.strip() for p in parts if p.strip()]


def _additional_info_box(items: list[str], height: float) -> Table:
    """Card next to the photos button — red header strip + bullet body.

    ``height`` lets the caller match the photos button's overall height
    so the row stays balanced.
    """
    header_s = ParagraphStyle(
        "InfoH", fontName="Helvetica-Bold", fontSize=11, textColor=WHITE,
        alignment=TA_CENTER, leading=14,
    )
    item_s = ParagraphStyle(
        "InfoI", fontName="Helvetica", fontSize=10, textColor=DARK,
        alignment=TA_LEFT, leading=14,
    )
    body_lines = items[:6] if items else ["(none provided)"]
    body_html = "<br/>".join(f"&bull;&nbsp; {line}" for line in body_lines)

    t = Table(
        [[Paragraph("ADDITIONAL INFO", header_s)],
         [Paragraph(body_html, item_s)]],
        colWidths=[CONTENT_W / 2 - 6],
        rowHeights=[26, height - 26],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), RED_BRAND),
        ("TOPPADDING", (0, 0), (0, 0), 6),
        ("BOTTOMPADDING", (0, 0), (0, 0), 6),
        ("BACKGROUND", (0, 1), (0, 1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LEFTPADDING", (0, 1), (0, 1), 14),
        ("RIGHTPADDING", (0, 1), (0, 1), 10),
        ("TOPPADDING", (0, 1), (0, 1), 10),
        ("BOTTOMPADDING", (0, 1), (0, 1), 8),
        ("VALIGN", (0, 1), (0, 1), "TOP"),
    ]))
    return t


def _money_box(label: str, amount: int | str) -> Table:
    label_s = ParagraphStyle(
        "MoneyL", fontName="Helvetica-Bold", fontSize=10, textColor=WHITE,
        alignment=TA_CENTER, leading=12,
    )
    display, value_size = _format_money(amount)
    value_s = ParagraphStyle(
        "MoneyV", fontName="Helvetica-Bold", fontSize=value_size,
        textColor=WHITE, alignment=TA_CENTER, leading=value_size + 4,
    )
    # Fixed rowHeights so both boxes render at identical size regardless of
    # whether the value is "$199,999" (28pt font) or "Taking All Offers"
    # (auto-shrunk to 18pt). The value row height is sized for the largest
    # font (28pt + 4 leading + cushion); shorter text vertically centers.
    t = Table(
        [[Paragraph(label, label_s)],
         [Paragraph(display, value_s)]],
        colWidths=[CONTENT_W / 2 - 6],
        rowHeights=[14, 38],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), RED_BRAND),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def build_pdf(data: FlyerData, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path), pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=0.4 * inch, bottomMargin=0.4 * inch,
        title=f"Investment Property — {data.address}",
        author=config.COMPANY_NAME,
    )
    flow: list = []

    # ── Header ───────────────────────────────────────────────────────
    company_s = ParagraphStyle(
        "Company", fontName="Helvetica-Bold", fontSize=15,
        textColor=DARK, alignment=TA_CENTER, leading=18,
    )
    tag_s = ParagraphStyle(
        "Tag", fontName="Helvetica-Oblique", fontSize=9, textColor=MUTED,
        alignment=TA_CENTER, leading=11,
    )
    phone_lbl_s = ParagraphStyle(
        "PhL", fontName="Helvetica", fontSize=8, textColor=MUTED,
        alignment=TA_RIGHT, leading=10,
    )
    phone_val_s = ParagraphStyle(
        "PhV", fontName="Helvetica-Bold", fontSize=14, textColor=RED_BRAND,
        alignment=TA_RIGHT, leading=16,
    )

    header = Table(
        [[
            _logo_or_text(),
            [Paragraph(config.COMPANY_NAME, company_s),
             Paragraph("Investment Opportunity — Off-Market", tag_s)],
            [Paragraph("CALL DIRECT", phone_lbl_s),
             Paragraph(_format_phone(config.COMPANY_PHONE), phone_val_s)],
        ]],
        colWidths=[1.4 * inch, CONTENT_W - 3.2 * inch, 1.8 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, -1), 2, RED_BRAND),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    flow.append(header)
    flow.append(Spacer(1, 8))

    # ── Hero photo ───────────────────────────────────────────────────
    hero_h = 2.9 * inch
    if data.hero_image_bytes:
        try:
            flow.append(Image(
                io.BytesIO(data.hero_image_bytes),
                width=CONTENT_W, height=hero_h, kind="proportional",
            ))
        except Exception:
            logger.exception("Hero image embed failed")
            flow.append(Spacer(1, hero_h))
    else:
        ph = Table([[""]], colWidths=[CONTENT_W], rowHeights=[hero_h])
        ph.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ]))
        flow.append(ph)
    flow.append(Spacer(1, 8))

    # ── Address line ─────────────────────────────────────────────────
    addr_s = ParagraphStyle(
        "Addr", fontName="Helvetica-Bold", fontSize=18, textColor=DARK,
        alignment=TA_CENTER, leading=22,
    )
    addr_text = (
        f"{data.address.upper()} &middot; "
        f"{data.city.upper()}, {data.state.upper()} {data.zip_code}"
    )
    flow.append(Paragraph(addr_text, addr_s))
    flow.append(Spacer(1, 8))

    # ── Stats strip ──────────────────────────────────────────────────
    sqft_value = f"{data.sqft:,}" if data.sqft else "—"
    sqft_sub_parts = []
    if data.year_built:
        sqft_sub_parts.append(f"BUILT {data.year_built}")
    if data.sqft_basement:
        sqft_sub_parts.append(f"+{data.sqft_basement:,} BSMT")
    sqft_sub = " · ".join(sqft_sub_parts)

    stats = Table([[
        _stat_box("Beds", str(data.bedrooms) if data.bedrooms else "—"),
        _stat_box("Baths", _format_baths(data.bathrooms)),
        _stat_box("Sqft", sqft_value, sub=sqft_sub),
        _stat_box("Acreage", f"{data.acreage:.2f}" if data.acreage else "—"),
    ]], colWidths=[CONTENT_W / 4] * 4)
    stats.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    flow.append(stats)
    flow.append(Spacer(1, 10))

    # ── Asking + ARV ─────────────────────────────────────────────────
    money = Table([[
        _money_box("ASKING PRICE", data.asking_price),
        _money_box("ARV", data.arv),
    ]], colWidths=[CONTENT_W / 2] * 2)
    money.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    flow.append(money)
    flow.append(Spacer(1, 12))

    # ── Additional info + photos button ──────────────────────────────
    info_height = 1.5 * inch
    info_box = _additional_info_box(_parse_info_items(data.additional_info), info_height)

    btn_s = ParagraphStyle(
        "Btn", fontName="Helvetica-Bold", fontSize=14, textColor=WHITE,
        alignment=TA_CENTER, leading=20,
    )
    btn_cap_s = ParagraphStyle(
        "BtnC", fontName="Helvetica", fontSize=9, textColor=MUTED,
        alignment=TA_CENTER, leading=12,
    )
    if data.photos_link:
        btn_inner = Paragraph(
            f'<link href="{data.photos_link}" color="#FFFFFF">'
            f'VIEW ALL PHOTOS &rarr;</link>',
            btn_s,
        )
    else:
        btn_inner = Paragraph("PHOTOS UNAVAILABLE", btn_s)

    btn = Table(
        [[btn_inner],
         [Paragraph("Click to see the full photo set", btn_cap_s)]],
        colWidths=[CONTENT_W / 2 - 6],
        rowHeights=[info_height - 26, 26],
    )
    btn.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, 1), 8),
    ]))

    sec_row = Table([[info_box, btn]], colWidths=[CONTENT_W / 2] * 2)
    sec_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    flow.append(sec_row)
    flow.append(Spacer(1, 10))

    # ── CTA footer ───────────────────────────────────────────────────
    cta_s = ParagraphStyle(
        "CTA", fontName="Helvetica-Bold", fontSize=14, textColor=WHITE,
        alignment=TA_CENTER, leading=18,
    )
    cta_sub_s = ParagraphStyle(
        "CTASub", fontName="Helvetica", fontSize=9, textColor=WHITE,
        alignment=TA_CENTER, leading=12,
    )
    cta = Table(
        [[Paragraph(
            f"CALL {_format_phone(config.COMPANY_PHONE)} TO LOCK UP THIS DEAL TODAY",
            cta_s,
        )],
         [Paragraph(config.COMPANY_NAME, cta_sub_s)]],
        colWidths=[CONTENT_W],
    )
    cta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), RED_BRAND),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    flow.append(cta)

    doc.build(flow)
    return output_path


# ── Top-level entry ───────────────────────────────────────────────────


def run_disposition_flyer(
    address: str,
    city: str,
    state: str,
    zip_code: str,
    asking_price: int | str,
    arv: int | str,
    bedrooms: int = 0,
    bathrooms: float = 0.0,
    sqft: int = 0,
    year_built: str = "",
    acreage: float = 0.0,
    additional_info: str = "",
    parent_folder_id: str = "",
    output_dir: Path | None = None,
    interactive: bool = True,
    skip_upload: bool = False,
) -> Path | None:
    """End-to-end: PVA lookup → resolve missing fields → fetch Drive photos →
    build PDF → upload to property folder.

    Returns the local PDF path on success, None on a hard failure
    (no Drive folder for the address, no service account key, etc.).
    """
    parent = parent_folder_id or config.REDNOUR_DRIVE_PARENT_FOLDER_ID
    drive_available = bool(parent and config.GOOGLE_SERVICE_ACCOUNT_KEY)
    if not drive_available:
        missing = []
        if not parent:
            missing.append("REDNOUR_DRIVE_PARENT_FOLDER_ID")
        if not config.GOOGLE_SERVICE_ACCOUNT_KEY:
            missing.append("GOOGLE_SERVICE_ACCOUNT_KEY")
        logger.warning(
            "Drive integration disabled — missing %s. Producing PDF with "
            "no photos and no 'View All Photos' link.",
            ", ".join(missing),
        )

    # 1. PVA lookup
    print(f"\n[1/4] Looking up {address} on Jefferson PVA...")
    print("      (this kicks you out of any active jeffersonpva.ky.gov session)")
    pva_fields = fetch_pva(address)
    if pva_fields:
        print(
            f"      Matched parcel {pva_fields.get('parcel_id', '?')}  "
            f"owner: {pva_fields.get('owner', '?')}"
        )
        if pva_fields.get("sqft"):
            print(f"      PVA sqft: {pva_fields['sqft']:,} (Main Unit Finished)")
        if pva_fields.get("sqft_basement"):
            print(f"      PVA basement finished: {pva_fields['sqft_basement']:,}")
    else:
        print("      PVA lookup failed — you'll enter all stats manually.")

    # 2. Resolve missing fields. Resolution priority: explicit CLI arg
    # wins, otherwise fall back to PVA, otherwise prompt (or stay 0/blank
    # in non-interactive mode).
    final_baths = bathrooms or float(pva_fields.get("bathrooms") or 0.0)
    final_acreage = acreage or float(pva_fields.get("acreage") or 0.0)
    final_year = year_built or (pva_fields.get("year_built", "") or "")
    final_sqft = sqft or int(pva_fields.get("sqft") or 0)
    final_basement = int(pva_fields.get("sqft_basement") or 0)

    if interactive:
        print("\n[2/4] Confirm property stats (press Enter to accept default):")
        if not bedrooms:
            bedrooms = _prompt_int("        Bedrooms (PVA does not provide)", 3)
        if final_sqft == 0:
            final_sqft = _prompt_int("        Sqft (PVA blank)", 0)
        else:
            override = _prompt(
                f"        Sqft (PVA: {final_sqft:,})",
                str(final_sqft),
            )
            try:
                final_sqft = int(re.sub(r"[^\d]", "", override) or "0")
            except ValueError:
                pass
        if final_baths == 0:
            final_baths = _prompt_float("        Baths (PVA blank)", 0.0)
        if not final_year:
            final_year = _prompt("        Year built (PVA blank)", "")
        if final_acreage == 0:
            final_acreage = _prompt_float("        Acreage (PVA blank)", 0.0)

    # 3. Fetch Drive assets (skipped if Drive integration unavailable)
    hero = None
    photos_link = ""
    prop_folder_id = ""
    if drive_available:
        print(f"\n[3/4] Fetching photos from Drive for '{address}'...")
        try:
            hero, photos_link, prop_folder_id = fetch_drive_assets(
                address, parent,
            )
        except Exception as e:
            logger.exception("Drive fetch failed: %s", e)
            print(f"      WARNING: Drive fetch failed ({e}) — building text-only PDF")
        if not prop_folder_id:
            print("      Property folder not found in Drive — building text-only PDF")
        if hero:
            print(f"      Hero image: {len(hero):,} bytes")
        if photos_link:
            print(f"      Photos folder link: {photos_link}")
    else:
        print("\n[3/4] Drive integration disabled — skipping photo fetch")

    # 4. Build + upload
    print("\n[4/4] Building PDF...")
    data = FlyerData(
        address=address, city=city, state=state, zip_code=zip_code,
        bedrooms=bedrooms, bathrooms=final_baths,
        sqft=final_sqft, sqft_basement=final_basement,
        acreage=final_acreage, year_built=final_year,
        asking_price=asking_price, arv=arv,
        photos_link=photos_link,
        additional_info=additional_info,
        hero_image_bytes=hero,
    )

    out_dir = output_dir or (config.OUTPUT_DIR / "flyers")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_addr = re.sub(r"[^A-Za-z0-9]+", "_", address).strip("_")
    pdf_path = out_dir / f"{safe_addr}_Flyer.pdf"
    build_pdf(data, pdf_path)
    print(f"      Local PDF: {pdf_path}")

    if skip_upload:
        print("      (--no-upload set — skipping Drive upload)")
        return pdf_path
    if not drive_available:
        print("      (Drive integration disabled — skipping upload)")
        return pdf_path
    if not prop_folder_id:
        print("      (property folder unknown — skipping upload)")
        return pdf_path

    link = upload_pdf(pdf_path, prop_folder_id)
    if link:
        print(f"      Uploaded to Drive: {link}")
    else:
        print("      WARNING: Drive upload failed (PDF saved locally)")
    return pdf_path
