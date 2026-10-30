"""
Tests for Web Views and UI Templates.
Verifies Dashboard, Composer, Calendar, Post Detail, and Static Asset delivery.
"""

from starlette.testclient import TestClient
from app.main import app


def test_static_files():
    client = TestClient(app)
    css_resp = client.get("/static/css/styles.css")
    assert css_resp.status_code == 200
    assert "--bg-canvas" in css_resp.text

    js_resp = client.get("/static/js/app.js")
    assert js_resp.status_code == 200
    assert "initTheme" in js_resp.text


def test_dashboard_view():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    assert "Up next" in resp.text
    assert "Recent activity" in resp.text


def test_composer_view():
    client = TestClient(app)
    resp = client.get("/posts/new")
    assert resp.status_code == 200
    assert "New post" in resp.text
    assert "composer-form" in resp.text
    assert "Live Preview" in resp.text
    assert "Telegram" in resp.text
    assert "Discord" in resp.text


def test_calendar_view():
    client = TestClient(app)
    resp = client.get("/calendar")
    assert resp.status_code == 200
    assert "Calendar" in resp.text
    assert "Mon" in resp.text
    assert "Sun" in resp.text


def test_post_detail_view():
    client = TestClient(app)

    # 1. Create a post first
    payload = {
        "title": "Web View Post",
        "body": "Checking detail view rendering",
        "targets": [{"platform": "telegram", "scheduled_at": "2026-10-30T10:00:00"}],
    }
    create_resp = client.post("/posts", json=payload)
    assert create_resp.status_code == 201
    post_id = create_resp.json()["id"]

    # 2. View post in HTML
    view_resp = client.get(f"/posts/{post_id}/view")
    assert view_resp.status_code == 200
    assert "Web View Post" in view_resp.text
    assert "Checking detail view rendering" in view_resp.text
    assert "Target platforms" in view_resp.text

