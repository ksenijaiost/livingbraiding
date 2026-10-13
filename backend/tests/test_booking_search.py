"""3.29: поиск брони по номеру не выбирает бронь, пока номер ещё набирают."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    User,
    UserRole,
    VisitMastersScope,
)
from app.routes.bookings import suggest_bookings_for_work_order

_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "booking_search.js"


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _booking(db, user, client, booking_id: int) -> Booking:
    row = Booking(
        id=booking_id,
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 10, 10, 9, 0, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(row)
    db.flush()
    return row


def test_prefix_query_lists_41_and_416(memory_db) -> None:
    user = User(
        username="master",
        password_hash="x",
        display_name="Мастер",
        role=UserRole.MASTER,
        is_active=True,
    )
    client = Client(name="Лариса", phone="79000000001", is_confirmed=True)
    memory_db.add_all([user, client])
    memory_db.flush()
    _booking(memory_db, user, client, 41)
    _booking(memory_db, user, client, 416)
    _booking(memory_db, user, client, 141)
    memory_db.commit()

    ids = [item["id"] for item in suggest_bookings_for_work_order(memory_db, "41")]
    assert 41 in ids
    assert 416 in ids
    assert 141 not in ids


def test_picker_does_not_select_while_typing(tmp_path) -> None:
    check = tmp_path / "booking_search_check.js"
    check.write_text(
        _JS.read_text(encoding="utf-8")
        + r"""
function fail(msg) { WScript.Echo(msg); WScript.Quit(1); }
function ids(items) {
  var out = [];
  for (var i = 0; i < items.length; i++) out.push(String(items[i].id));
  return out.join(",");
}
var api = lbBookingSearch;
var both = [{id: 41, client_name: "a"}, {id: 416, client_name: "b"}];
var typed = api.applySuggestResponse(
  { seq: 2, query: "41", bookingId: "", items: [], highlight: -1 },
  { seq: 2, query: "41", items: both, commit: false }
);
if (typed.selected) fail("typing 41 must not select");
if (typed.bookingId) fail("bookingId must stay empty");
if (ids(typed.items) !== "41,416") fail("list " + ids(typed.items));
var entered = api.applySuggestResponse(
  { seq: 4, query: "416", bookingId: "", items: [], highlight: -1 },
  { seq: 4, query: "416", items: [{id: 416}], commit: true }
);
if (!entered.selected || entered.bookingId !== 416) fail("enter 416 must select 416");
var stale = api.applySuggestResponse(
  { seq: 5, query: "416", bookingId: "", items: [], highlight: -1 },
  { seq: 3, query: "41", items: both, commit: false }
);
if (!stale.ignored || stale.bookingId) fail("stale 41 must not select");
var afterChange = api.applySuggestResponse(
  { seq: 8, query: "41", bookingId: "", items: [], highlight: -1 },
  { seq: 8, query: "41", items: both, commit: false }
);
if (afterChange.selected) fail("after change, typing must not select");
var keys = api.bookingQueryKeyAction("Enter", 1, 2);
if (keys.type !== "select-highlight") fail("arrows plus enter");
var plain = api.bookingQueryKeyAction("Enter", -1, 2);
if (plain.type !== "commit") fail("enter without highlight commits");
if (api.nextHighlight(-1, 1, 2) !== 0) fail("arrow down");
""",
        encoding="utf-8",
    )
    host = shutil.which("cscript") or r"C:\Windows\System32\cscript.exe"
    proc = subprocess.run(
        [host, "//Nologo", "//E:JScript", str(check)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
