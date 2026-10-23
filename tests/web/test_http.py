import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mappd.web import errors
from mappd.web.http import conflict, idempotency_key

app = FastAPI()
errors.install(app)


@app.post("/echo")
async def echo(key: str | None = Depends(idempotency_key)) -> dict[str, str | None]:
    return {"key": key}


@app.post("/api/stale")
async def stale() -> None:
    conflict("ticket changed", 3, "sent")


client = TestClient(app)


def test_idempotency_key_is_optional() -> None:
    assert client.post("/echo").json() == {"key": None}
    assert client.post("/echo", headers={"Idempotency-Key": "k-1"}).json() == {"key": "k-1"}


@pytest.mark.parametrize("bad", ["", "x" * 201, "naïve"])
def test_bad_idempotency_key_is_422(bad: str) -> None:
    headers = {b"Idempotency-Key": bad.encode()}  # bytes: the client refuses non-ASCII str
    assert client.post("/echo", headers=headers).status_code == 422


def test_conflict_carries_the_current_version() -> None:
    r = client.post("/api/stale")
    assert r.status_code == 409
    assert r.json() == {"detail": "ticket changed", "current_version": 3, "status": "sent"}
