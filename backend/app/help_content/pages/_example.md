---
title: Пример страничной справки
roles: [admin, admin_senior, admin_super, master, helper, techspec]
---

# Пример

Это образец файла в `pages/`. Рабочие тексты добавляются отдельными коммитами (шаги 6–7).

Имя файла без префикса `_` и без `.md` = `help_page_id` в шаблоне.

Пример в шаблоне:

```jinja
{% set help_page_id = "admin_clients_list" %}
```

соответствует файлу `pages/admin_clients_list.md`.
