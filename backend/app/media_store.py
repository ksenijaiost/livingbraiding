from __future__ import annotations

import os
import uuid
from pathlib import Path

from starlette.datastructures import UploadFile


ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}
MAX_BYTES = 10 * 1024 * 1024  # 10MB


def media_root_dir() -> Path:
    # Under backend/data/uploads (same server as code).
    root = Path(os.environ.get("LB_MEDIA_ROOT") or "data/uploads")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_ext(filename: str | None, content_type: str | None) -> str | None:
    name = (filename or "").strip()
    ext = Path(name).suffix.lower() if name else ""
    if ext in ALLOWED_EXTS:
        return ext
    # fallback by content-type
    ct = (content_type or "").strip().lower()
    if ct == "image/jpeg":
        return ".jpg"
    if ct == "image/png":
        return ".png"
    if ct == "image/webp":
        return ".webp"
    if ct in ("image/heic", "image/heif"):
        return ".heic"
    return None


def _is_upload_file_like(v: object) -> bool:
    if isinstance(v, UploadFile):
        return True
    return hasattr(v, "read") and hasattr(v, "filename") and hasattr(v, "content_type")


def get_nonempty_upload(form: object, name: str) -> object | None:
    """
    Extract the first non-empty file upload for `name` from a Starlette `FormData`.

    Some clients/proxies may yield compatible upload objects that are not the exact
    `UploadFile` class; we accept UploadFile and simple duck-typed upload objects.
    """
    items: list[object] = []
    if hasattr(form, "getlist"):
        try:
            items = list(form.getlist(name))  # type: ignore[assignment]
        except Exception:
            items = []
    if not items and hasattr(form, "get"):
        v = form.get(name)  # type: ignore[assignment]
        if v is not None:
            items = [v]
    for it in items:
        if not _is_upload_file_like(it):
            continue
        fn = getattr(it, "filename", None)
        if not (str(fn or "").strip()):
            continue
        return it
    return None


async def save_upload_image(upload: object) -> str:
    """
    Save an uploaded image to local disk and return a DB-safe URL: `/media/<rel_path>`.
    """
    ext = _safe_ext(upload.filename, upload.content_type)
    if not ext:
        raise ValueError("Некорректный файл: допускаются JPG/PNG/WebP/HEIC.")

    # yyyy-mm style folders are overkill; keep it simple for now.
    heic_out_ext = ".jpg" if ext in (".heic", ".heif") else ext
    rel_name = f"{uuid.uuid4().hex}{heic_out_ext}"
    root = media_root_dir()
    dest = (root / rel_name)

    # HEIC/HEIF: конвертируем в JPG перед сохранением.
    if ext in (".heic", ".heif"):
        # Read upload into memory (лимит 10MB уже стоит).
        data_parts: list[bytes] = []
        total = 0
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("Файл слишком большой (лимит 10MB).")
            data_parts.append(chunk)
        data = b"".join(data_parts)

        try:
            from io import BytesIO

            from PIL import Image  # type: ignore
            from pillow_heif import register_heif_opener  # type: ignore

            register_heif_opener()
            img = Image.open(BytesIO(data))
            img.load()

            # HEIC может содержать прозрачность; JPEG её не поддерживает.
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.save(dest, format="JPEG", quality=90, optimize=True)
        except ImportError as e:
            raise ValueError("Для HEIC нужна библиотека pillow-heif.") from e
        except Exception as e:
            # Do not leak internals to UI; keep message actionable.
            raise ValueError("Не удалось обработать HEIC-фото.") from e

        # reset for potential reuse
        try:
            await upload.seek(0)
        except Exception:
            pass
        return f"/media/{rel_name}"

    # JPG/PNG/WebP: сохраняем как есть (стримом).
    total = 0
    with dest.open("wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                try:
                    f.close()
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                raise ValueError("Файл слишком большой (лимит 10MB).")
            f.write(chunk)

    # reset for potential reuse
    try:
        await upload.seek(0)
    except Exception:
        pass

    return f"/media/{rel_name}"


def delete_media_by_url(url: str | None) -> None:
    """
    Best-effort delete of a previously stored local media URL.
    Only deletes if it looks like our `/media/<name>` url.
    """
    if not url:
        return
    u = str(url).strip()
    if not u.startswith("/media/"):
        return
    rel = u[len("/media/") :].strip().lstrip("/")
    if not rel:
        return
    p = media_root_dir() / rel
    try:
        p.unlink(missing_ok=True)
    except Exception:
        return
