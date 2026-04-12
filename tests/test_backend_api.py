from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test_service_bus.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("PUBLIC_IP", "37.200.79.56")
    monkeypatch.setenv("PORT", "8000")
    monkeypatch.setenv("BASE_URL", "http://37.200.79.56:8000")
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "10000")
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", str(5 * 1024 * 1024))

    import service_bus_backend_main as backend

    backend = importlib.reload(backend)
    return backend


@pytest.fixture()
def client(app_module):
    with TestClient(app_module.app) as c:
        yield c


def login_token(client: TestClient, username: str, password: str, field_name: str = "username") -> str:
    response = client.post("/auth/login", data={field_name: username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_public(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "timestamp" in payload
    assert "version" in payload


def test_login_success_username_and_login_alias(client: TestClient):
    token_1 = login_token(client, "admin", "admin123", field_name="username")
    token_2 = login_token(client, "admin", "admin123", field_name="login")
    assert token_1
    assert token_2


def test_login_fail_and_unauthorized(client: TestClient):
    bad = client.post("/auth/login", data={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401
    assert bad.json()["code"] == "HTTP_ERROR"

    protected = client.get("/admin/users")
    assert protected.status_code == 401
    assert protected.json()["code"] == "HTTP_ERROR"


def test_admin_users_crud_and_roles(client: TestClient):
    admin_token = login_token(client, "admin", "admin123")
    headers = auth_headers(admin_token)

    roles = client.get("/admin/roles", headers=headers)
    assert roles.status_code == 200
    role_codes = {item["code"] for item in roles.json()}
    assert {"admin", "driver", "passenger", "customer"}.issubset(role_codes)

    create = client.post(
        "/admin/users",
        headers=headers,
        json={
            "login": "driver1",
            "password": "pass1234",
            "role": "driver",
            "vehicle_model": "Bus",
            "license_plate": "A001AA",
            "is_active": True,
            "can_track": True,
            "can_manage_users": False,
            "can_view_logs": False,
        },
    )
    assert create.status_code == 201, create.text
    user_id = create.json()["id"]

    patch = client.patch(f"/admin/users/{user_id}/permissions", headers=headers, json={"can_view_logs": True})
    assert patch.status_code == 200
    assert patch.json()["can_view_logs"] is True

    delete = client.delete(f"/admin/users/{user_id}", headers=headers)
    assert delete.status_code == 200


def test_driver_route_and_location_flow(client: TestClient):
    admin_token = login_token(client, "admin", "admin123")
    admin_headers = auth_headers(admin_token)

    created = client.post(
        "/admin/users",
        headers=admin_headers,
        json={
            "login": "driver2",
            "password": "pass1234",
            "role": "driver",
            "vehicle_model": "CityBus",
            "license_plate": "B002BB",
            "is_active": True,
            "can_track": True,
            "can_manage_users": False,
            "can_view_logs": False,
        },
    )
    driver_id = created.json()["id"]
    driver_token = login_token(client, "driver2", "pass1234")
    driver_headers = auth_headers(driver_token)

    start = client.post(
        "/route/start",
        headers=driver_headers,
        json={
            "start_name": "A",
            "start_lat": 1.0,
            "start_lng": 2.0,
            "end_name": "B",
            "end_lat": 3.0,
            "end_lng": 4.0,
            "start_time": "08:00",
        },
    )
    assert start.status_code == 201, start.text

    upd = client.post("/location/update", headers=driver_headers, json={"latitude": 10.0, "longitude": 20.0})
    assert upd.status_code == 200

    active = client.get("/routes/active", headers=driver_headers)
    assert active.status_code == 200
    assert len(active.json()) >= 1

    location = client.get(f"/location/{driver_id}", headers=driver_headers)
    assert location.status_code == 200

    finish = client.post("/route/finish", headers=driver_headers)
    assert finish.status_code == 200


def test_requests_flow_customer_to_admin(client: TestClient):
    admin_token = login_token(client, "admin", "admin123")
    admin_headers = auth_headers(admin_token)

    customer_create = client.post(
        "/admin/users",
        headers=admin_headers,
        json={
            "login": "customer1",
            "password": "pass1234",
            "role": "customer",
            "is_active": True,
            "can_track": False,
            "can_manage_users": False,
            "can_view_logs": False,
        },
    )
    assert customer_create.status_code == 201
    customer_token = login_token(client, "customer1", "pass1234")

    created_req = client.post(
        "/requests",
        headers=auth_headers(customer_token),
        json={
            "requester_kind": "company",
            "company_name": "Acme",
            "route_from": "Office",
            "route_to": "Airport",
            "trip_time": "2026-04-12T10:00:00Z",
            "passenger_count": 20,
            "comment": "Need one bus",
        },
    )
    assert created_req.status_code == 201, created_req.text
    req_id = created_req.json()["id"]

    queue = client.get("/admin/requests", headers=admin_headers)
    assert queue.status_code == 200
    assert any(item["id"] == req_id for item in queue.json())

    approve = client.post(f"/admin/requests/{req_id}/approve", headers=admin_headers)
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"
