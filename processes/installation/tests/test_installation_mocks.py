import json
from typing import Any

from fastapi.testclient import TestClient

from processes.installation.mocks import mock_llm, mock_telegram

tg = TestClient(mock_telegram.app)
llm = TestClient(mock_llm.app)


def push(update_id: int, text: str = "hi") -> None:
    tg.post(
        "/test/push", json={"update_id": update_id, "message": {"chat": {"id": 501}, "text": text}}
    )


def test_get_updates_honors_the_offset_and_keeps_duplicates() -> None:
    tg.post("/test/reset")
    push(1)
    push(2)
    push(2)
    got = tg.post("/bottok/getUpdates", json={"offset": 2}).json()
    assert got["ok"] is True
    assert [u["update_id"] for u in got["result"]] == [2, 2]


def test_send_message_is_recorded_and_files_download() -> None:
    tg.post("/test/reset")
    assert tg.post("/bottok/sendMessage", json={"chat_id": 501, "text": "hello"}).json()["ok"]
    assert tg.get("/test/sent").json() == [{"chat_id": 501, "text": "hello"}]
    path = tg.post("/bottok/getFile", json={"file_id": "photo-1"}).json()["result"]["file_path"]
    photo = tg.get(f"/file/bottok/{path}").content
    assert photo.startswith(b"\xff\xd8")
    assert len(photo) > 1000, "a photo a person can see, not a 1x1 pixel"


def test_a_push_without_ids_gets_fresh_ones() -> None:
    tg.post("/test/reset")
    message = {"chat": {"id": 501}, "text": "I have arrived"}
    first = tg.post("/test/push", json={"message": dict(message)}).json()["update_id"]
    second = tg.post("/test/push", json={"message": dict(message)}).json()["update_id"]
    assert second == first + 1
    got = tg.post("/bottok/getUpdates", json={"offset": first}).json()["result"]
    assert [(u["update_id"], u["message"]["message_id"]) for u in got] == [
        (first, first),
        (second, second),
    ]
    explicit = tg.post("/test/push", json={"update_id": 7, "message": dict(message)}).json()
    assert explicit["update_id"] == 7, "an explicit id is kept (a redelivery test needs it)"


def interpreter_request(text: str) -> dict[str, Any]:
    return {
        "model": "mock-smart",
        "messages": [
            {"role": "system", "content": "You read messages from installation technicians."},
            {
                "role": "user",
                "content": f"Current step: arrived\nNext step: access\nMessage: {text}",
            },
        ],
        "tools": [{"type": "function", "function": {"name": "final_result"}}],
    }


def interpretation(text: str) -> dict[str, Any]:
    reply = llm.post("/v1/chat/completions", json=interpreter_request(text)).json()
    call = reply["choices"][0]["message"]["tool_calls"][0]["function"]
    return dict(json.loads(call["arguments"]))


def test_interpreter_rules() -> None:
    assert interpretation("access granted, I am in")["reached_next"] is True
    missing = interpretation("parts are missing")
    assert (missing["intent"], missing["exception"]) == ("exception", "missing_parts")
    assert interpretation("what time is it?")["intent"] == "question"


def test_the_reply_names_the_step_reached_and_what_comes_next() -> None:
    reply = interpretation("access granted")["reply"]
    assert reply == "Noted, you have access. Tell me when you have found the equipment."


def test_vision_describes_a_photo() -> None:
    body = {
        "model": "mock-vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": mock_llm.VISION_MARK,
                    },
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
                ],
            }
        ],
    }
    reply = llm.post("/v1/chat/completions", json=body).json()
    assert "installed" in reply["choices"][0]["message"]["content"]


def test_claims_only_installation_requests() -> None:
    assert mock_llm.claims(interpreter_request("x"))
    assert mock_llm.claims(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": mock_llm.VISION_MARK,
                        }
                    ],
                }
            ]
        }
    )
    assert mock_llm.claims({"__path__": "/installation/calls"})
    assert not mock_llm.claims({"messages": [{"role": "user", "content": "Sensor offline"}]})
