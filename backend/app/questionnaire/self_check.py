"""
Проверка примеров из data/examples/ против Pydantic-схем.

Запуск из папки backend:
  ..\\.venv\\Scripts\\python -m app.questionnaire.self_check
"""

from __future__ import annotations

import json
from pathlib import Path

from app.questionnaire.schemas import VisitServiceDetailsPayload


def _examples_dir() -> Path:
    return Path(__file__).resolve().parent / "data" / "examples"


def validate_all_examples() -> None:
    root = _examples_dir()
    errors: list[str] = []
    for path in sorted(root.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            VisitServiceDetailsPayload.model_validate(raw)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    if errors:
        raise SystemExit("Validation failed:\n" + "\n".join(errors))


if __name__ == "__main__":
    validate_all_examples()
    print("OK: all examples match VisitServiceDetailsPayload")
