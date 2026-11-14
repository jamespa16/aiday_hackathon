from fastapi.testclient import TestClient

from aiday.api import create_app


def test_non_ascii_untrusted_auth_and_cursor_return_errors_instead_of_500():
    with TestClient(create_app(":memory:", admin_token="a" * 32, rate_limit=False)) as http:
        invalid_auth = http.get("/admin/runs", headers=[(b"authorization", b"Bearer \xff")])
        assert invalid_auth.status_code == 401
        created = http.post("/practice/runs", json={"tier": 1}).json()
        response = http.get("/v1/inbox", params={"cursor": "\u00e9"}, headers={"Authorization": "Bearer " + created["token"]})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cursor"
