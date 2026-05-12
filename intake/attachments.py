"""Traitement des pièces jointes : compression, extraction texte, stockage disque.

Aucun appel LLM ici. Les pièces jointes sont stockées avec leurs métadonnées,
et ne seront soumises à un modèle vision qu'en stage 2 de l'investigation
(voir investigation/llm_analyze) si stage 1 demande explicitement l'analyse.
"""
import hashlib
import io
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image
from pypdf import PdfReader


def _attachments_path() -> Path:
    return Path(os.getenv("CARE_ATTACHMENTS_PATH", "/data/care/attachments"))


def _max_upload_size() -> int:
    return int(os.getenv("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024


ACCEPTED_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "application/pdf",
}

IMAGE_MAX_DIM = 1920
IMAGE_JPEG_QUALITY = 85
IMAGE_THUMBNAIL_DIM = 200
PDF_MAX_PAGES = 20
PDF_MAX_EXTRACTED_CHARS = 50000


@dataclass
class ProcessedAttachment:
    id: str
    original_filename: str
    mime_type: str
    size_original: int
    size_compressed: int
    storage_path: str
    thumbnail_path: Optional[str]
    content_hash: str
    extracted_text: Optional[str]
    page_count: Optional[int]


class AttachmentTooLarge(Exception):
    """Upload au-delà de MAX_UPLOAD_SIZE_MB."""


class AttachmentTypeNotAllowed(Exception):
    """MIME non listé dans ACCEPTED_MIMES."""


def _ensure_dir(ticket_id: str) -> Path:
    target = _attachments_path() / ticket_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def _compress_image(raw: bytes) -> tuple[bytes, bytes]:
    """Redimensionne, recompresse en JPEG sans EXIF, génère la miniature.

    Pillow ne réécrit pas les tags EXIF par défaut lors d'un `save()`, donc
    repasser par un buffer JPEG suffit à les supprimer.
    """
    img = Image.open(io.BytesIO(raw))

    # Aplatit la transparence sur fond blanc (sinon JPEG ne supporte pas alpha)
    if img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        mask = img.split()[-1] if img.mode in ("RGBA", "LA") else None
        background.paste(img, mask=mask)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    img.thumbnail((IMAGE_MAX_DIM, IMAGE_MAX_DIM), Image.LANCZOS)

    compressed_buf = io.BytesIO()
    img.save(compressed_buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
    compressed = compressed_buf.getvalue()

    thumb = img.copy()
    thumb.thumbnail((IMAGE_THUMBNAIL_DIM, IMAGE_THUMBNAIL_DIM), Image.LANCZOS)
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="JPEG", quality=80, optimize=True)
    thumbnail = thumb_buf.getvalue()

    return compressed, thumbnail


def _extract_pdf(raw: bytes) -> tuple[bytes, str, int]:
    """Extrait le texte des 20 premières pages. Le PDF brut n'est pas recompressé.

    Retourne (raw_unchanged, text, page_count_total).
    """
    reader = PdfReader(io.BytesIO(raw))
    page_count = len(reader.pages)

    chunks: list[str] = []
    total_chars = 0
    for page in reader.pages[:PDF_MAX_PAGES]:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        chunks.append(text)
        total_chars += len(text)
        if total_chars > PDF_MAX_EXTRACTED_CHARS:
            break

    extracted = "\n".join(chunks)[:PDF_MAX_EXTRACTED_CHARS]
    return raw, extracted, page_count


def process_attachment(
    ticket_id: str,
    raw: bytes,
    filename: str,
    mime_type: str,
) -> ProcessedAttachment:
    """Compresse, stocke, extrait les métadonnées. Aucun appel LLM."""

    if len(raw) > _max_upload_size():
        raise AttachmentTooLarge(f"max {_max_upload_size() // (1024*1024)}MB")

    mime_normalized = mime_type.lower()
    if mime_normalized not in ACCEPTED_MIMES:
        raise AttachmentTypeNotAllowed(mime_normalized)

    attachment_id = f"att_{uuid.uuid4().hex[:16]}"
    content_hash = hashlib.sha256(raw).hexdigest()
    target_dir = _ensure_dir(ticket_id)

    extracted_text: Optional[str] = None
    page_count: Optional[int] = None
    thumbnail_path: Optional[str] = None

    if mime_normalized.startswith("image/"):
        compressed, thumb = _compress_image(raw)
        storage_path = target_dir / f"{attachment_id}.jpg"
        thumbnail_target = target_dir / f"{attachment_id}.thumb.jpg"
        storage_path.write_bytes(compressed)
        thumbnail_target.write_bytes(thumb)
        thumbnail_path = str(thumbnail_target)
        size_compressed = len(compressed)
    elif mime_normalized == "application/pdf":
        _, extracted_text, page_count = _extract_pdf(raw)
        storage_path = target_dir / f"{attachment_id}.pdf"
        storage_path.write_bytes(raw)
        size_compressed = len(raw)
    else:  # pragma: no cover - filtré plus haut
        raise AttachmentTypeNotAllowed(mime_normalized)

    return ProcessedAttachment(
        id=attachment_id,
        original_filename=filename,
        mime_type=mime_normalized,
        size_original=len(raw),
        size_compressed=size_compressed,
        storage_path=str(storage_path),
        thumbnail_path=thumbnail_path,
        content_hash=content_hash,
        extracted_text=extracted_text,
        page_count=page_count,
    )
