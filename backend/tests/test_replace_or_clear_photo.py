from __future__ import annotations

import asyncio

from app.media_store import replace_or_clear_photo


class _Form:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str, default=None):
        return self._data.get(key, default)


def test_clear_flag_drops_current_photo() -> None:
    url, changed = asyncio.run(
        replace_or_clear_photo(_Form({"clear_photo_1": "1"}), "/media/old.jpg", "photo_1")
    )
    assert url is None
    assert changed is True


def test_without_clear_or_upload_keeps_current_photo() -> None:
    url, changed = asyncio.run(replace_or_clear_photo(_Form({}), "/media/old.jpg", "photo_1"))
    assert url == "/media/old.jpg"
    assert changed is False
