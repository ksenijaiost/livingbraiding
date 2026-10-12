from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import AuthUser, get_current_user
from app.db.session import get_db
from app.media_store import media_root_dir


router = APIRouter()


@router.get("/media/v/{token}")
def media_video_public(token: str, db: Session = Depends(get_db)):
    """Публичное видео студии по неугадываемому токену. Без авторизации."""
    from app.studio_template_variables import resolve_public_video

    found = resolve_public_video(db, token)
    if found is None:
        raise HTTPException(status_code=404, detail="Not found")
    path, media_type = found
    return FileResponse(str(path), media_type=media_type)


@router.get("/media/{path:path}")
def media_get(
    path: str,
    current_user: AuthUser = Depends(get_current_user),
):
    # Auth is enough; TECHSPEC is handled by auth layer.
    if not path or ".." in path or path.startswith(("/", "\\")):
        raise HTTPException(status_code=404, detail="Not found")

    root = media_root_dir().resolve()
    p = (root / path).resolve()
    try:
        p.relative_to(root)
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")

    if not p.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(str(p))

