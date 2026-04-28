"""
Связь `service_code` (стабильный код услуги) → файл описания полей формы в `data/forms/`.

При добавлении услуги:
1. Добавь код и метаданные в соответствующий файл в `data/catalog/`.
2. Укажи здесь путь к JSON формы (или переиспользуй общий файл формы).
"""

from __future__ import annotations

# service_code -> path относительно data/
SERVICE_FORM_FILES: dict[str, str] = {
    "full_head_kit_2h": "forms/v1/fields_full_head_kit_inlay_shared.json",
    "full_head_kit_4h": "forms/v1/fields_full_head_kit_inlay_shared.json",
}


def form_file_for_service(service_code: str) -> str | None:
    return SERVICE_FORM_FILES.get(service_code)
