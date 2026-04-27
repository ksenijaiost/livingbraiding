from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.auth import AuthUser, get_current_user
from app.media_store import media_root_dir


router = APIRouter()


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

