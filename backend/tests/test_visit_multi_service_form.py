"""Парсинг формы визита: вторая услуга только при явном service_id."""

from __future__ import annotations

from starlette.datastructures import FormData

from app.visit_multi_service import (
    _discover_line_indices,
    form_uses_multi_service_lines,
    parse_multi_service_visit_form,
)


def _form(data: dict[str, str]) -> FormData:
    return FormData([(k, v) for k, v in data.items()])


def test_hidden_line_1_stubs_do_not_enable_multi() -> None:
    """Скрытые line_1_* без service_id не считаются второй услугой."""
    form = _form(
        {
            "service_id": "5",
            "amount_from_client": "5000",
            "line_1_kit_kind": "STOCK",
            "line_1_kanekalon_grams": "0",
            "line_1_kudri_grams": "0",
            "line_1_mix_source": "NO_MIX",
            "client_mode": "existing",
            "existing_client_id": "1",
            "performed_date": "2026-05-24",
        }
    )
    assert form_uses_multi_service_lines(form) is False
    assert _discover_line_indices(form) == [0]


def test_second_service_when_line_1_service_id_set() -> None:
    form = _form(
        {
            "service_id": "5",
            "amount_from_client": "3000",
            "line_1_service_id": "7",
            "line_1_amount_from_client": "2000",
            "line_1_kit_kind": "STOCK",
            "line_1_kanekalon_grams": "0",
            "line_1_kudri_grams": "0",
            "line_1_mix_source": "NO_MIX",
            "client_mode": "existing",
            "existing_client_id": "1",
            "performed_date": "2026-05-24",
        }
    )
    assert form_uses_multi_service_lines(form) is True
    assert _discover_line_indices(form) == [0, 1]


def test_parse_multi_single_line_indices() -> None:
    form = _form(
        {
            "service_id": "10",
            "amount_from_client": "4000",
            "line_1_kit_kind": "STOCK",
            "client_mode": "existing",
            "existing_client_id": "2",
            "performed_date": "2026-05-24",
            "amortization_level": "MIN",
            "kanekalon_grams": "0",
            "kudri_grams": "0",
            "mix_source": "NO_MIX",
            "kit_kind": "STOCK",
        }
    )
    multi = parse_multi_service_visit_form(form, single_master_default_id=1)
    assert len(multi.lines) == 1
    assert multi.lines[0].service_id == 10
    assert multi.lines[0].amount_from_client == 4000.0


def test_second_line_does_not_inherit_line0_visit_service_id() -> None:
    from app.visit_multi_service import _parse_line_from_form

    form = _form(
        {
            "service_id": "5",
            "visit_service_id": "100",
            "amount_from_client": "3000",
            "line_1_service_id": "7",
            "line_1_amount_from_client": "2000",
            "line_1_kit_kind": "STOCK",
            "line_1_kanekalon_grams": "0",
            "line_1_kudri_grams": "0",
            "line_1_mix_source": "NO_MIX",
            "client_mode": "existing",
            "existing_client_id": "1",
            "performed_date": "2026-05-24",
        }
    )
    line0 = _parse_line_from_form(form, 0)
    line1 = _parse_line_from_form(form, 1)
    assert line0.visit_service_id == 100
    assert line1.visit_service_id is None
    assert line1.service_id == 7
    assert line1.amount_from_client == 2000.0


def test_discover_includes_line0_when_only_visit_service_id_hidden() -> None:
    form = _form(
        {
            "visit_service_id": "100",
            "amount_from_client": "3000",
            "line_1_service_id": "7",
            "line_1_amount_from_client": "2000",
            "line_1_kit_kind": "STOCK",
            "line_1_mix_source": "NO_MIX",
            "client_mode": "existing",
            "existing_client_id": "1",
            "performed_date": "2026-05-24",
        }
    )
    assert _discover_line_indices(form) == [0, 1]
    multi = parse_multi_service_visit_form(form, single_master_default_id=1)
    assert len(multi.lines) == 2
    assert multi.lines[0].visit_service_id == 100
    assert multi.lines[1].visit_service_id is None


def test_parse_per_service_master_allocations() -> None:
    items = [
        ("service_id", "5"),
        ("amount_from_client", "3000"),
        ("masters_scope", "PER_SERVICE"),
        ("line_0_service_master_on", "10"),
        ("line_0_service_master_on", "11"),
        ("line_0_service_master_pct_10", "50"),
        ("line_0_service_master_pct_11", "50"),
        ("line_1_service_id", "7"),
        ("line_1_amount_from_client", "2000"),
        ("line_1_service_master_on", "10"),
        ("line_1_service_master_pct_10", "100"),
        ("line_1_kit_kind", "STOCK"),
        ("line_1_mix_source", "NO_MIX"),
        ("client_mode", "existing"),
        ("existing_client_id", "1"),
        ("performed_date", "2026-05-24"),
        ("kit_kind", "STOCK"),
        ("mix_source", "NO_MIX"),
    ]
    form = FormData(items)
    multi = parse_multi_service_visit_form(form, single_master_default_id=99)
    assert multi.header.masters_scope.value == "PER_SERVICE"
    assert multi.lines[0].service_master_allocations == [(10, 50), (11, 50)]
    assert multi.lines[1].service_master_allocations == [(10, 100)]
