"""The four systems the resolver reads and acts on, and their mock MCP server."""

from typing import Any

import dbos_async
import httpx2
from fastapi.testclient import TestClient

from mappd import mcp
from mappd.data.world import load
from processes.installation.mocks import mock_mcp, systems
from processes.installation.mocks.systems import Contact, Facts, Slot, Systems


def facts() -> Facts:
    return Facts(
        stock={"CTL-H400": 1, "SEN-T100": 5},
        slots=[Slot(site="Riverside Offices", at="2026-10-20T09:00"), Slot(at="2026-10-23T09:00")],
        contacts={
            "Riverside Offices": Contact(
                name="Marta Novak", phone="+49 000 5550 1201", access="Key at the front desk."
            )
        },
    )


def test_reads_answer_from_the_facts() -> None:
    s = Systems(facts())
    assert s.check_stock("CTL-H400") == {"sku": "CTL-H400", "on_hand": 1}
    assert s.check_stock("CTL-NOPE") == {"sku": "CTL-NOPE", "on_hand": 0}
    assert s.delivery_slots("Riverside Offices", ["CTL-H400"])["slots"] == [
        "2026-10-20T09:00",
        "2026-10-23T09:00",
    ]
    assert s.delivery_slots("Lindenhof School", [])["slots"] == ["2026-10-23T09:00"]
    assert s.site_contact("Riverside Offices")["phone"] == "+49 000 5550 1201"
    assert "error" in s.site_contact("Nowhere")


def test_reserving_takes_stock_once_per_key() -> None:
    s = Systems(facts())
    first = s.reserve_parts("j-1", ["CTL-H400"], "k-1")
    assert first["ok"] is True
    assert s.reserve_parts("j-1", ["CTL-H400"], "k-1") == first, "same key, same answer"
    assert s.check_stock("CTL-H400")["on_hand"] == 0
    assert s.reserve_parts("j-2", ["CTL-H400"], "k-2") == {"ok": False, "missing": ["CTL-H400"]}


def test_booking_and_tickets_are_idempotent_by_key() -> None:
    s = Systems(facts())
    booked = s.book_delivery("j-1", "2026-10-20T09:00", "k-1")
    assert booked["ok"] is True
    assert s.book_delivery("j-1", "2026-10-20T09:00", "k-1") == booked
    assert s.book_delivery("j-2", "2026-10-20T09:00", "k-2")["ok"] is False, "slot taken"
    ticket = s.open_ticket("j-1", "breakage", "cracked housing", "k-3")
    assert s.open_ticket("j-1", "breakage", "cracked housing", "k-3") == ticket
    assert [name for name, _ in s.calls].count("open_ticket") == 2


def test_facts_from_the_world_sum_the_depots() -> None:
    world = load()
    f = Facts.from_world(world)
    assert f.stock["CTL-H400"] == sum(d.stock.get("CTL-H400", 0) for d in world.depots)
    assert set(f.contacts) == {s.name for s in world.sites}
    assert len(f.slots) == len(world.delivery_slots)


def test_the_eval_toolset_leaves_out_confirmed_tools() -> None:
    async def names(include: bool) -> set[str]:
        toolset = systems.toolset(Systems(facts()), include_confirmed=include)
        return set(toolset.tools)

    assert dbos_async.run(names(include=False)) == {
        "check_stock",
        "delivery_slots",
        "open_ticket",
        "site_contact",
    }
    assert {"reserve_parts", "book_delivery"} <= dbos_async.run(names(include=True))
    assert {"reserve_parts", "book_delivery"} == systems.CONFIRMED


def test_the_mock_server_answers_over_mcp_and_records_calls() -> None:
    async def scenario() -> tuple[dict[str, Any], list[str]]:
        served = mock_mcp.create_app()
        async with mock_mcp.running(served):
            transport = httpx2.ASGITransport(app=served)
            got = await mcp.call(
                "http://mock-mcp/warehouse/mcp",
                "check_stock",
                {"sku": "SEN-T100"},
                transport=transport,
            )
            listed = mcp.toolset("http://mock-mcp/tickets/mcp", id="t", transport=transport)
            async with listed:
                names = [t.name for t in await listed.list_tools()]
        return got, names

    mock_mcp.reset()
    got, names = dbos_async.run(scenario())
    assert got["sku"] == "SEN-T100"
    assert names == ["open_ticket"]
    client = TestClient(mock_mcp.app)
    assert [c["tool"] for c in client.get("/test/calls").json()] == ["check_stock"]
    client.post("/test/reset")
    assert client.get("/test/calls").json() == []


def test_the_mock_is_registered_as_mcp() -> None:
    from processes.installation import process  # noqa: PLC0415

    spec = process.mocks["mcp"]
    assert (spec.app, spec.port) == (mock_mcp.app, 9400)
