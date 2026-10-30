import os
import sys
import re
import tempfile
import pytest
from fastapi.testclient import TestClient

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Configure writable test storage BEFORE importing app.main
TEST_STORAGE = tempfile.mkdtemp(prefix="crisis_management_tests_")

os.environ["DATABASE_PATH"] = os.path.join(TEST_STORAGE, "app.db")
os.environ["UPLOAD_DIR"] = os.path.join(TEST_STORAGE, "uploads")

from app import main

# Import application only after environment configuration
from app.main import app, init_db

# Use isolated temporary storage for every test
@pytest.fixture(autouse=True)
def reset_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    upload_path = tmp_path / "uploads"

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("UPLOAD_DIR", str(upload_path))

    upload_path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "UPLOAD_DIR", upload_path)
    client.cookies.clear()

    init_db()
    yield


client = TestClient(app)


def get_csrf():
    """Get the CSRF token from the login page."""
    response = client.get("/login")
    assert response.status_code == 200

    match = re.search(
        r'name="csrf_token" value="([^"]+)"',
        response.text
    )

    assert match, "Login CSRF token not found"
    return match.group(1)


def get_authenticated_csrf():
    """Get the rotated CSRF token from the authenticated homepage."""
    response = client.get("/")
    assert response.status_code == 200

    match = re.search(
        r'<meta name="csrf-token" content="([^"]+)"',
        response.text
    )

    assert match, "Authenticated CSRF token not found"
    return match.group(1)


def logout():
    """Logout using the authenticated CSRF token."""
    token = get_authenticated_csrf()

    return client.post(
        "/logout",
        headers={"X-CSRF-Token": token},
        follow_redirects=False
    )


def test_login_logout():
    csrf = get_csrf()

    payload = {
        "username": "admin",
        "password": "ChangeThisNow-123!",
        "csrf_token": csrf
    }

    response = client.post(
        "/login",
        data=payload,
        follow_redirects=False
    )

    assert response.status_code == 303
    assert "set-cookie" in response.headers

    # Verify authenticated access
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200

    # Logout with the NEW authenticated CSRF token
    logout_response = logout()
    assert logout_response.status_code == 303

    # Verify protected page redirects after logout
    home_after_logout = client.get("/", follow_redirects=False)
    assert home_after_logout.status_code == 303


def test_user_isolation():
    # Login as admin
    csrf_admin = get_csrf()

    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "ChangeThisNow-123!",
            "csrf_token": csrf_admin
        },
        follow_redirects=False
    )

    assert response.status_code == 303

    # Create another user
    csrf_token = get_authenticated_csrf()

    create_response = client.post(
        "/api/admin/users",
        json={
            "username": "user1",
            "password": "Password123456",
            "role": "user"
        },
        headers={"X-CSRF-Token": csrf_token}
    )

    assert create_response.status_code in (200, 201)

    # Logout admin
    assert logout().status_code == 303

    # Login as user1
    csrf_user = get_csrf()

    response = client.post(
        "/login",
        data={
            "username": "user1",
            "password": "Password123456",
            "csrf_token": csrf_user
        },
        follow_redirects=False
    )

    assert response.status_code == 303

    # User1 must see only their own conversations
    chats = client.get("/api/chats")
    assert chats.status_code == 200
    assert chats.json() == []

    # Logout user1
    assert logout().status_code == 303

    # Login as admin again
    csrf_admin = get_csrf()

    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "ChangeThisNow-123!",
            "csrf_token": csrf_admin
        },
        follow_redirects=False
    )

    assert response.status_code == 303

    # Admin can see the created user
    users = client.get("/api/admin/users")
    assert users.status_code == 200
    assert any(user["username"] == "user1" for user in users.json())


def login_admin_for_settings_tests():
    """Authenticate the admin and return the active CSRF token."""
    csrf = get_csrf()
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "ChangeThisNow-123!",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return get_authenticated_csrf()


def test_api_key_is_encrypted_and_never_returned(monkeypatch):
    monkeypatch.setattr(
        main, "MASTER_KEY", main.Fernet.generate_key().decode()
    )
    csrf = login_admin_for_settings_tests()
    secret = "test-api-key-do-not-expose"

    response = client.put(
        "/api/settings",
        json={
            "provider": "openai",
            "model": "test-model",
            "api_key": secret,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    with main.db() as c:
        row = c.execute(
            "SELECT api_key FROM settings WHERE user_id=1"
        ).fetchone()

    assert row is not None
    encrypted = row["api_key"]
    assert encrypted != secret
    assert encrypted.startswith("gAAAA")
    assert main.decrypt_value(encrypted) == secret

    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.json()["has_api_key"] == 1
    assert "api_key" not in settings.json()
    assert secret not in settings.text


def test_settings_update_preserves_existing_api_key(monkeypatch):
    monkeypatch.setattr(
        main, "MASTER_KEY", main.Fernet.generate_key().decode()
    )
    csrf = login_admin_for_settings_tests()
    secret = "test-api-key-preserve"

    first = client.put(
        "/api/settings",
        json={"provider": "openai", "api_key": secret},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200

    with main.db() as c:
        before = c.execute(
            "SELECT api_key FROM settings WHERE user_id=1"
        ).fetchone()["api_key"]

    csrf = get_authenticated_csrf()
    second = client.put(
        "/api/settings",
        json={"provider": "openai", "model": "updated-model"},
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 200

    with main.db() as c:
        after = c.execute(
            "SELECT api_key FROM settings WHERE user_id=1"
        ).fetchone()["api_key"]

    assert after == before
    assert main.decrypt_value(after) == secret


def test_clearing_api_key_removes_stored_value(monkeypatch):
    monkeypatch.setattr(
        main, "MASTER_KEY", main.Fernet.generate_key().decode()
    )
    csrf = login_admin_for_settings_tests()

    first = client.put(
        "/api/settings",
        json={"provider": "openai", "api_key": "temporary-key"},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200

    csrf = get_authenticated_csrf()
    response = client.put(
        "/api/settings",
        json={"provider": "openai", "api_key": ""},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    with main.db() as c:
        stored = c.execute(
            "SELECT api_key FROM settings WHERE user_id=1"
        ).fetchone()["api_key"]

    assert stored == ""

    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.json()["has_api_key"] == 0


def test_api_key_rejects_non_string_value(monkeypatch):
    monkeypatch.setattr(
        main, "MASTER_KEY", main.Fernet.generate_key().decode()
    )
    csrf = login_admin_for_settings_tests()

    response = client.put(
        "/api/settings",
        json={"provider": "openai", "api_key": 12345},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
