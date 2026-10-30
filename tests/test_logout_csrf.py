import os
import tempfile
import re

import pytest
from fastapi.testclient import TestClient

# Configure writable storage before importing the application.
TEST_STORAGE = tempfile.mkdtemp(prefix="crisis_logout_tests_")
os.environ["DATABASE_PATH"] = os.path.join(TEST_STORAGE, "app.db")
os.environ["UPLOAD_DIR"] = os.path.join(TEST_STORAGE, "uploads")

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    upload_path = tmp_path / "uploads"

    upload_path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("UPLOAD_DIR", str(upload_path))
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_path)

    main.init_db()

    with TestClient(main.app) as test_client:
        yield test_client


def login_admin(client):
    login_page = client.get("/login")
    assert login_page.status_code == 200

    match = re.search(
        r'name="csrf_token" value="([^"]+)"',
        login_page.text
    )
    assert match, "Login CSRF token not found"

    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "ChangeThisNow-123!",
            "csrf_token": match.group(1)
        },
        follow_redirects=False
    )

    assert response.status_code == 303

    homepage = client.get("/")
    assert homepage.status_code == 200

    match = re.search(
        r'<meta name="csrf-token" content="([^"]+)"',
        homepage.text
    )
    assert match, "Authenticated CSRF token not found"

    return match.group(1)


def test_logout_with_valid_csrf_token(client):
    token = login_admin(client)

    response = client.post(
        "/logout",
        headers={"X-CSRF-Token": token},
        follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_logout_without_csrf_token(client):
    login_admin(client)

    response = client.post(
        "/logout",
        follow_redirects=False
    )

    assert response.status_code == 403


def test_logout_with_invalid_csrf_token(client):
    login_admin(client)

    response = client.post(
        "/logout",
        headers={"X-CSRF-Token": "invalid-token"},
        follow_redirects=False
    )

    assert response.status_code == 403


def test_failed_logout_preserves_authentication(client):
    login_admin(client)

    response = client.post(
        "/logout",
        headers={"X-CSRF-Token": "invalid-token"},
        follow_redirects=False
    )

    assert response.status_code == 403

    me = client.get("/api/me")
    assert me.status_code == 200


def test_successful_logout_invalidates_session(client):
    token = login_admin(client)

    response = client.post(
        "/logout",
        headers={"X-CSRF-Token": token},
        follow_redirects=False
    )

    assert response.status_code == 303

    me = client.get("/api/me")
    assert me.status_code == 401


def test_unauthenticated_logout(client):
    response = client.post(
        "/logout",
        follow_redirects=False
    )

    assert response.status_code == 403
