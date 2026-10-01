from __future__ import annotations

from app.db.models import UserRole
from app.user_roles import staff_list_group


def test_staff_list_group_order() -> None:
    assert staff_list_group([UserRole.MASTER]) == 0
    assert staff_list_group([UserRole.MASTER, UserRole.ADMIN]) == 0
    assert staff_list_group([UserRole.HELPER, UserRole.ADMIN]) == 1
    assert staff_list_group([UserRole.ADMIN_SENIOR, UserRole.ADMIN]) == 2
    assert staff_list_group([UserRole.ADMIN_SUPER]) == 2
