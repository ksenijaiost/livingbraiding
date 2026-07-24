from __future__ import annotations

from datetime import datetime

from app.display_time import format_naive_utc_datetime
from app.webui import templates


def test_format_naive_utc_to_novosibirsk() -> None:
    # 12:00 UTC → 19:00 Новосибирск (UTC+7)
    assert format_naive_utc_datetime(datetime(2026, 7, 23, 12, 0), "Asia/Novosibirsk") == "23.07.2026 19:00"
    assert (
        format_naive_utc_datetime(datetime(2026, 7, 23, 12, 0), "Asia/Novosibirsk", "%Y-%m-%d %H:%M")
        == "2026-07-23 19:00"
    )


def test_jinja_dt_local_filter() -> None:
    env = templates.env
    tpl = env.from_string("{{ ts|dt_local }} / {{ ts|dt_local('%H:%M') }}")
    out = tpl.render(ts=datetime(2026, 7, 23, 12, 0), display_tz="Asia/Novosibirsk")
    assert out == "23.07.2026 19:00 / 19:00"
