## Бронь/резерв: дизайн (MVP, без календаря)

### Цели
- Админ создаёт **бронь** на будущую запись/продажу: клиент, плановая дата, опционально мастер, услуга/товар, предоплата, озвученная стоимость, комментарий.
- Если выбран **комплект из наличия**, бронь удерживает реальные остатки: количество заготовок или весь комплект.
- Бронь можно конвертировать в факт (визит/продажа/заказ) и автоматически снять удержание.

### Новые сущности

#### `booking`
- **id**: int PK
- **created_at**: datetime (utc)
- **created_by_user_id**: FK `users.id`
- **updated_at**: datetime nullable
- **updated_by_user_id**: FK `users.id` nullable
- **client_id**: FK `clients.id`
- **planned_at**: datetime (плановая дата/время)
- **planned_master_user_id**: FK `users.id` nullable (если указан мастер)
- **kind**: enum (например: `VISIT`, `PRODUCT_SALE`, `STUDIO_ORDER`)
- **quoted_price_amount**: int nullable (озвученная стоимость)
- **deposit_amount**: int nullable (предоплата)
- **comment**: text nullable
- **status**: enum (например: `ACTIVE`, `CANCELLED`, `CONVERTED`)
- **cancelled_at/cancelled_by_user_id**: nullable

Опциональные “что планируется” поля:
- **planned_service_id**: FK `services.id` nullable (если бронь именно на услугу)
- **planned_product_kind**: enum nullable (если бронь именно на товар: комплект/материал/другое)

#### `booking_kit_holds`
Позволяет удерживать склад **по количеству**, а не “флажком”.
- **id**: int PK
- **booking_id**: FK `booking.id` (CASCADE)
- **kit_id**: FK `kits.id`
- **hold_kind**: enum (`ENTIRE_KIT` или `PIECES`)
- **pieces_reserved**: int nullable (если `PIECES`)
- **created_at**: datetime

### Правила удержания остатков
- При создании/редактировании брони:
  - `ENTIRE_KIT`: допустимо только если `kit.pieces_available > 0` и комплект активен/в наличии.
  - `PIECES`: `pieces_reserved > 0` и `pieces_reserved <= kit.pieces_available - already_held`.
- Хранить удержания отдельно от текущего `Kit.reserved_*`:
  - `Kit.reserved_*` оставить как быстрый “MVP-флажок” в текущих потоках.
  - Для брони всегда использовать `booking_kit_holds` как источник правды по количеству.

### Конвертация брони → факт
- **booking.kind=VISIT**: создаём визит по выбранным параметрам (или открываем мастер-форму с префиллом), затем:
  - уменьшаем остатки комплектов по `booking_kit_holds`
  - помечаем бронь `CONVERTED`
- **booking.kind=PRODUCT_SALE / STUDIO_ORDER**: аналогично, создаём факт и списываем удержания.

### Освобождение удержаний
- При отмене брони (`CANCELLED`) удержания перестают действовать (физически строки можно оставить как историю).
- При редактировании брони удержания пересчитываются: “снять старые” → “поставить новые”.

### UI (контуры, без календаря)
- Роуты для ADMIN/ADMIN_SUPER:
  - список броней (фильтр по дате, статусу, клиенту)
  - создание/редактирование
  - действия: отменить, конвертировать

### Миграции
Проектная политика: всё в `alembic/versions/0001_init.py`, т.к. БД пересоздаётся в dev.

