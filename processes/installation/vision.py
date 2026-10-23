"""Photos described by the gateway's vision model.

A plain call rather than an agent. The workflow wraps download and description in one durable
step (`workflow.describe_photo`), so the journal records the description, never the bytes, and
a replay never resends the image.
"""

import base64
from typing import Any

from mappd import telemetry
from mappd.gateway import GATEWAY_API_KEY
from mappd.settings import Settings

VISION_MODEL = "vision"
PROMPT = (
    "Describe this photo for a dispatcher of an installation company: what equipment is visible,"
    " whether it looks mounted and connected, and any damage. Two sentences at most."
)
HTTP = telemetry.traced_client(timeout=120)


def describe_image(image: bytes) -> str:
    """A short description of a photo by the gateway model `vision`."""
    url = f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"
    body: dict[str, Any] = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ],
    }
    response = HTTP.post(
        f"{Settings.from_env().gateway_url}/chat/completions",
        json=body,
        headers={"authorization": f"Bearer {GATEWAY_API_KEY}"},
    )
    response.raise_for_status()
    return str(response.json()["choices"][0]["message"]["content"]).strip()
