"""Tests attachments : compression, EXIF, limite taille, type MIME, miniature."""
import io
import importlib

import pytest
from PIL import Image


def _make_image_bytes(width=3000, height=2000, mode="RGB", color=(255, 0, 0)):
    img = Image.new(mode, (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _reload_attachments_with_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("CARE_ATTACHMENTS_PATH", str(tmp_path))
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "10")
    import intake.attachments as attach_module
    importlib.reload(attach_module)
    return attach_module


def test_image_is_resized(tmp_path, monkeypatch):
    attach = _reload_attachments_with_tmp(tmp_path, monkeypatch)

    raw = _make_image_bytes(3000, 2000)
    result = attach.process_attachment(
        ticket_id="ticket_abcdef012345",
        raw=raw,
        filename="screenshot.png",
        mime_type="image/png",
    )

    assert result.size_compressed < result.size_original

    img = Image.open(result.storage_path)
    assert max(img.size) <= attach.IMAGE_MAX_DIM


def test_image_strips_alpha(tmp_path, monkeypatch):
    attach = _reload_attachments_with_tmp(tmp_path, monkeypatch)

    raw = _make_image_bytes(200, 200, mode="RGBA", color=(255, 0, 0, 128))
    result = attach.process_attachment(
        ticket_id="ticket_abcdef012345",
        raw=raw,
        filename="x.png",
        mime_type="image/png",
    )

    img = Image.open(result.storage_path)
    assert img.mode == "RGB"


def test_realistic_screenshot_compresses_significantly(tmp_path, monkeypatch):
    """Capture d'écran typique (gradient + texte) compressée à < 500 KB.

    Le test mime un screenshot 2500x1800 qui ressemble à de l'UI : large
    fond uni avec quelques zones contrastées. Cas représentatif du payload
    réel envoyé par les clients.
    """
    attach = _reload_attachments_with_tmp(tmp_path, monkeypatch)

    img = Image.new("RGB", (2500, 1800), color=(20, 25, 40))
    # Quelques rectangles pour simuler une UI
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    for x in range(0, 2500, 300):
        for y in range(0, 1800, 200):
            draw.rectangle([x, y, x + 250, y + 150], fill=(40, 50, 80))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()

    result = attach.process_attachment(
        ticket_id="ticket_abcdef012345",
        raw=raw,
        filename="screenshot.png",
        mime_type="image/png",
    )
    # Garantie clé : taille de sortie bornée pour le stockage et l'envoi LLM
    assert result.size_compressed < 500 * 1024


def test_oversized_file_rejected(tmp_path, monkeypatch):
    attach = _reload_attachments_with_tmp(tmp_path, monkeypatch)
    raw = b"\x00" * (11 * 1024 * 1024)

    with pytest.raises(attach.AttachmentTooLarge):
        attach.process_attachment(
            ticket_id="ticket_abcdef012345",
            raw=raw,
            filename="big.png",
            mime_type="image/png",
        )


def test_unsupported_mime_rejected(tmp_path, monkeypatch):
    attach = _reload_attachments_with_tmp(tmp_path, monkeypatch)
    with pytest.raises(attach.AttachmentTypeNotAllowed):
        attach.process_attachment(
            ticket_id="ticket_abcdef012345",
            raw=b"x",
            filename="bad.exe",
            mime_type="application/x-msdownload",
        )


def test_thumbnail_generated(tmp_path, monkeypatch):
    attach = _reload_attachments_with_tmp(tmp_path, monkeypatch)
    raw = _make_image_bytes(800, 600)
    result = attach.process_attachment(
        ticket_id="ticket_abcdef012345",
        raw=raw,
        filename="x.png",
        mime_type="image/png",
    )

    assert result.thumbnail_path
    from pathlib import Path
    assert Path(result.thumbnail_path).exists()
    thumb = Image.open(result.thumbnail_path)
    assert max(thumb.size) <= attach.IMAGE_THUMBNAIL_DIM


def test_exif_is_stripped(tmp_path, monkeypatch):
    """Vérifie qu'aucune donnée EXIF GPS ne survit à la compression."""
    attach = _reload_attachments_with_tmp(tmp_path, monkeypatch)

    img = Image.new("RGB", (400, 300), (200, 100, 50))
    buf = io.BytesIO()
    # Pillow encode l'EXIF si on le passe explicitement
    img.save(buf, format="JPEG", exif=b"Exif\x00\x00II*\x00\x08\x00\x00\x00")
    raw = buf.getvalue()

    result = attach.process_attachment(
        ticket_id="ticket_abcdef012345",
        raw=raw,
        filename="geotagged.jpg",
        mime_type="image/jpeg",
    )

    out_img = Image.open(result.storage_path)
    assert not out_img.info.get("exif"), "EXIF should be stripped"


def test_attachment_id_format(tmp_path, monkeypatch):
    attach = _reload_attachments_with_tmp(tmp_path, monkeypatch)
    raw = _make_image_bytes(100, 100)
    result = attach.process_attachment(
        ticket_id="ticket_abcdef012345",
        raw=raw,
        filename="x.png",
        mime_type="image/png",
    )
    assert result.id.startswith("att_")
    assert len(result.id) == len("att_") + 16
