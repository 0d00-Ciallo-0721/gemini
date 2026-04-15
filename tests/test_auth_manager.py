import time

from reverse_runtime.auth_manager import AuthManager


def test_auth_manager_falls_back_when_no_active_ticket(tmp_path):
    manager = AuthManager(
        str(tmp_path),
        {
            "auth_mode": "relay_ticket",
            "accounts": {"1": {"label": "fallback"}},
        },
    )

    view = manager.get_auth_view()

    assert view["auth_mode"] == "relay_ticket"
    assert view["active_ticket"] is None
    assert view["status"] == "fallback"
    assert manager.fallback_active is True


def test_auth_manager_returns_active_ticket_when_ticket_is_fresh(tmp_path):
    manager = AuthManager(
        str(tmp_path),
        {
            "auth_mode": "relay_ticket",
            "accounts": {"1": {"label": "fallback"}},
            "relay_ticket_ttl_sec": 3600,
        },
    )
    manager.store.save_active_ticket(
        {
            "client_id": "desktop",
            "push_time": time.time(),
            "last_refresh_time": time.time(),
            "status": "healthy",
            "cookie_data": {
                "SECURE_1PSID": "psid",
                "SECURE_1PSIDTS": "psidts",
            },
        }
    )

    view = manager.get_auth_view()

    assert view["status"] == "healthy"
    assert view["active_ticket"]["client_id"] == "desktop"
    assert manager.fallback_active is False


def test_auth_manager_marks_expired_ticket_and_falls_back(tmp_path):
    manager = AuthManager(
        str(tmp_path),
        {
            "auth_mode": "relay_ticket",
            "accounts": {"1": {"label": "fallback"}},
            "relay_ticket_ttl_sec": 1,
        },
    )
    manager.store.save_active_ticket(
        {
            "client_id": "desktop",
            "push_time": time.time() - 10,
            "last_refresh_time": time.time() - 10,
            "status": "healthy",
            "cookie_data": {
                "SECURE_1PSID": "psid",
                "SECURE_1PSIDTS": "psidts",
            },
        }
    )

    view = manager.get_auth_view()
    saved = manager.store.load_active_ticket()

    assert view["active_ticket"] is None
    assert view["status"] == "expired"
    assert saved["status"] == "expired"
    assert manager.fallback_active is True
