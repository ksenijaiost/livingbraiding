from __future__ import annotations

import logging

from app.access_logging import access_log_level_for_status
from app.form_validation_log import (
    build_validation_log_payload,
    hourly_help_summary_from_snapshot,
    log_user_validation_error,
    relevant_fields_for_message,
    snapshot_form_fields,
)


class _Form:
    def __init__(self, data: dict[str, str | list[str]]):
        self._data = data

    def keys(self):
        return self._data.keys()

    def get(self, key, default=None):
        v = self._data.get(key, default)
        if isinstance(v, list):
            return v[-1] if v else default
        return v

    def getlist(self, key):
        v = self._data.get(key, [])
        return v if isinstance(v, list) else [v]


def test_snapshot_form_fields() -> None:
    form = _Form(
        {
            "amount_from_client": "",
            "own_corr_custom_amount": "4500",
            "own_corr_use_custom_amount": "1",
            "booking_id": "140",
            "photo_1": "<ignored>",
        }
    )
    snap = snapshot_form_fields(form)
    assert snap["own_corr_custom_amount"] == "4500"
    assert snap["booking_id"] == "140"
    assert "photo_1" in snap


def test_relevant_fields_for_amount_message() -> None:
    hints = relevant_fields_for_message("Укажите сумму, взятую с клиента.")
    assert "amount_from_client" in hints
    assert "own_corr_custom_amount" in hints


def test_build_validation_log_payload_always_includes_full_form() -> None:
    snap = {f"f{i}": str(i) for i in range(50)}
    snap.update(
        {
            "amount_from_client": "",
            "own_corr_custom_amount": "4500",
            "own_corr_use_custom_amount": "1",
            "own_correction": "on",
            "booking_id": "140",
            "client_is_self": "",
            "hourly_help_amount_9": "3000",
            "hourly_help_hours_9": "2",
            "hourly_help_minutes_9": "0",
        }
    )
    payload = build_validation_log_payload(
        message="Сумма почасовой помощи превышает пул ЗП мастеров визита (помощь 3000 ₽, пул 2000 ₽).",
        snapshot=snap,
        context="visit",
        extra={"booking_id": 140},
    )
    assert payload["form"] == snap
    assert payload["related"]["hourly_help_amount_9"] == "3000"
    assert payload["hourly_help"]["hourly_help_total"] == 3000.0
    assert payload["extra"]["booking_id"] == 140


def test_hourly_help_summary_from_snapshot() -> None:
    summary = hourly_help_summary_from_snapshot(
        {
            "hourly_help_amount_3": "1000",
            "hourly_help_hours_3": "1",
            "hourly_help_minutes_3": "30",
            "hourly_help_amount_9": "500,5",
        }
    )
    assert summary is not None
    assert summary["hourly_help_total"] == 1500.5
    assert len(summary["hourly_help_rows"]) == 2


def test_log_user_validation_error_sets_request_state(caplog) -> None:
    from starlette.requests import Request

    scope = {"type": "http", "method": "POST", "path": "/master/visit/new", "headers": []}
    request = Request(scope)
    logger = logging.getLogger("test.form_validation")
    with caplog.at_level(logging.WARNING, logger="test.form_validation"):
        log_user_validation_error(
            logger,
            request=request,
            route="POST /master/visit/new",
            message="Укажите сумму, взятую с клиента.",
            form=_Form({"booking_id": "140", "own_corr_custom_amount": "4500"}),
            user_id=3,
            username="ira",
            context="visit",
        )
    assert request.state.validation_error == "Укажите сумму, взятую с клиента."
    assert any("form validation failed" in r.message for r in caplog.records)
    assert any("own_corr_custom_amount" in r.message for r in caplog.records)
    assert any('"form"' in r.message for r in caplog.records)


def test_access_log_level_for_status() -> None:
    assert access_log_level_for_status(200, has_validation_error=False) == logging.INFO
    assert access_log_level_for_status(303, has_validation_error=False) == logging.INFO
    assert access_log_level_for_status(400, has_validation_error=True) == logging.WARNING
    assert access_log_level_for_status(404, has_validation_error=False) == logging.ERROR
    assert access_log_level_for_status(500, has_validation_error=False) == logging.ERROR
    assert access_log_level_for_status(499, has_validation_error=False) == logging.WARNING
