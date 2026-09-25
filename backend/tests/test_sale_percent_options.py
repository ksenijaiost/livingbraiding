from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.sale_percent_options import (
    apply_sale_percent_delete,
    apply_sale_percent_save,
    list_sale_percents,
    parse_sale_percent_input,
    sale_percent_choices,
)


def test_default_percents_when_setting_missing() -> None:
    db = MagicMock()
    db.scalar.return_value = None
    assert list_sale_percents(db) == [10, 15]


def test_reads_sorted_unique_list() -> None:
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(value_json="[15, 5, 10, 5]")
    assert list_sale_percents(db) == [5, 10, 15]


def test_save_adds_and_replaces() -> None:
    assert apply_sale_percent_save([10, 15], original=None, new_value=5) == [5, 10, 15]
    assert apply_sale_percent_save([10, 15], original=15, new_value=5) == [5, 10]


def test_save_rejects_duplicate() -> None:
    with pytest.raises(ValueError, match="уже есть"):
        apply_sale_percent_save([10, 15], original=None, new_value=10)


def test_delete_keeps_last_percent() -> None:
    assert apply_sale_percent_delete([10, 15], 10) == [15]
    with pytest.raises(ValueError, match="последний"):
        apply_sale_percent_delete([15], 15)


def test_form_keeps_stored_percent_outside_the_list() -> None:
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(value_json="[5, 15]")
    assert sale_percent_choices(db, 10) == [5, 10, 15]


def test_parse_accepts_only_allowed() -> None:
    assert parse_sale_percent_input("5", [5, 10, 15]) == 5
    with pytest.raises(ValueError):
        parse_sale_percent_input("7", [5, 10, 15])
