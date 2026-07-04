"""Поиск по ID в списках (Fix 105)."""

from __future__ import annotations

from app.list_search import parse_list_id_search


def test_parse_list_id_search() -> None:
    assert parse_list_id_search(None) is None
    assert parse_list_id_search("") is None
    assert parse_list_id_search("  ") is None
    assert parse_list_id_search("abc") is None
    assert parse_list_id_search("0") is None
    assert parse_list_id_search("#146") == 146
    assert parse_list_id_search(" 42 ") == 42
