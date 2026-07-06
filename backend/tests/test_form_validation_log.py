from __future__ import annotations

from app.form_validation_log import (
    build_validation_log_payload,
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


def test_build_validation_log_payload_includes_related_amount_fields() -> None:
    snap = {
        "amount_from_client": "",
        "own_corr_custom_amount": "4500",
        "own_corr_use_custom_amount": "1",
        "own_correction": "on",
        "booking_id": "140",
        "client_is_self": "",
    }
    payload = build_validation_log_payload(
        message="Укажите сумму, взятую с клиента.",
        snapshot=snap,
        context="visit",
        extra={"booking_id": 140},
    )
    assert payload["related"]["own_corr_custom_amount"] == "4500"
    assert payload["highlights"]["booking_id"] == "140"
    assert payload["extra"]["booking_id"] == 140


def test_log_user_validation_error_sets_request_state(caplog) -> None:
    import logging

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
