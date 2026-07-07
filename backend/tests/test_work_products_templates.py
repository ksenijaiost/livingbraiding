from __future__ import annotations


def test_work_products_templates_expose_client_payment_kind_label() -> None:
    from app.work_products import templates

    assert "client_payment_kind_label" in templates.env.globals


def test_product_sales_templates_expose_client_payment_kind_label() -> None:
    from app.product_sales import templates

    assert "client_payment_kind_label" in templates.env.globals
