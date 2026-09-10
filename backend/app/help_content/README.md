# Справка LivingBraiding (контент)

Тексты встроенной справки. Меняются вместе с продуктом и попадают в прод при деплое.

## Структура

```text
help_content/
  faq/           # обзор кабинета по роли (страница /help)
  pages/         # справка конкретной страницы (кнопка «?»)
  manifest.yaml  # реестр page_id → title / roles / file
```

## Роли → файлы FAQ

| Роль (`UserRole`) | Файл |
|-------------------|------|
| `MASTER` | `faq/master.md` |
| `HELPER` | `faq/helper.md` |
| `ADMIN` | `faq/admin.md` |
| `ADMIN_SENIOR` | `faq/admin_senior.md` |
| `ADMIN_SUPER` | `faq/admin_super.md` |
| `TECHSPEC` | `faq/techspec.md` |

FAQ показывается по **`active_role`** (текущий кабинет), не по максимальной роли пользователя.

## `help_page_id`

- Стабильный идентификатор в `snake_case` (не URL и не русское название).
- В шаблоне страницы: `{% set help_page_id = "admin_clients_list" %}`.
- Файл справки: `pages/<help_page_id>.md`.
- Если файла нет или роль не в `roles` front-matter — кнопку «?» не показывать.

## Front-matter (опционально)

```yaml
---
title: Клиенты
roles: [admin, admin_senior, admin_super]
---
```

- `title` — заголовок модалки / карточки.
- `roles` — список ролей в нижнем регистре (`master`, `helper`, `admin`, …). Пусто или отсутствует = доступно всем ролям, для которых страница вообще открыта.

## Правила контента

- В page-help — только «что на этой странице» и важные ограничения.
- Общее про кабинет — в FAQ роли, не копировать длинные куски в каждую страницу.
- Только русский язык (MVP).
- Произвольный HTML в `.md` не использовать; рендер идёт через безопасный Markdown.
