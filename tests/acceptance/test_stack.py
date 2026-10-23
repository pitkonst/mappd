import httpx2
import pytest
import stack

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(900)]


def test_clean_stack_is_healthy_on_every_layer() -> None:
    assert httpx2.get(f"{stack.APP}/health", timeout=5).json() == {"ok": True, "version": "v1"}
    report = stack.doctor()
    assert report["ok"], report
