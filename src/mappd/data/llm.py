"""Chat completions through the gateway, cached. Keys live only in the gateway."""

import hashlib
import json
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx2
import yaml

from mappd.gateway import GATEWAY_API_KEY

if TYPE_CHECKING:
    from pathlib import Path

    from mappd.data.cache import Cache
    from mappd.settings import Settings

TIMEOUT = 120.0
# A slow or overloaded provider is normal over hundreds of calls: retry before giving up.
RETRIES = 3
BACKOFF = 2.0  # seconds before the first retry, doubled each time
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def gateway_defaults(path: Path) -> dict[str, str]:
    """Per gateway model, a hash of the request `defaults` the gateway adds (reasoning, hosts).

    Part of every cache key: changing a default changes the answers, so it must miss the cache.
    A missing or unreadable file gives {}.
    """
    try:
        config = yaml.safe_load(path.read_text())
    except OSError, yaml.YAMLError:
        return {}
    llm = (config or {}).get("llm") if isinstance(config, dict) else None
    models = (llm or {}).get("models") or [] if isinstance(llm, dict) else []
    out: dict[str, str] = {}
    for entry in models:
        if not isinstance(entry, dict):
            continue
        defaults = entry.get("defaults")
        blob = json.dumps(defaults, sort_keys=True) if defaults else ""
        out[str(entry.get("name"))] = hashlib.sha256(blob.encode()).hexdigest() if blob else ""
    return out


class GatewayError(RuntimeError):
    """The gateway did not return a usable completion."""


class _TransientError(Exception):
    """A failure worth retrying: a timeout, a dropped connection, an overloaded provider."""


class Gateway:
    """Synchronous, cached chat completions for the data tools."""

    def __init__(
        self,
        settings: Settings,
        cache: Cache,
        client: httpx2.Client | None = None,
        *,
        retries: int = RETRIES,
        backoff: float = BACKOFF,
    ) -> None:
        """Talk to `settings.gateway_url`, caching in `cache`; safe to share across threads.

        Timeouts, connection errors, 408, 429 and 5xx are retried `retries` times with
        exponential backoff from `backoff` seconds; other failures raise at once.
        """
        self._url = f"{settings.gateway_url}/chat/completions"
        self._cache = cache
        self._client = client or httpx2.Client(timeout=TIMEOUT)
        self._backends: dict[str, str] = {}
        self._probe = threading.Lock()  # one backend probe per model, even from many threads
        self._defaults = gateway_defaults(settings.gateway_config)
        self._retries = retries
        self._backoff = backoff

    def backend(self, model: str) -> str:
        """The model the gateway routes `model` to, plus a hash of its gateway defaults.

        From one tiny probe per run. Part of every cache key: answers cached from the mocks are
        never served to a run against a real provider, nor one setting's to another's. The host
        (OpenRouter's `provider`) is left out: it changes from call to call, so keying on it would
        make every rerun miss; pin hosts in the gateway defaults, which are part of the key.
        """
        with self._probe:
            return self._probe_backend(model)

    def _probe_backend(self, model: str) -> str:
        if model not in self._backends:
            body = {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            answered = str(self._post(model, body).get("model") or "unknown")
            self._backends[model] = f"{answered}|{self._defaults.get(model, '')}"
        return self._backends[model]

    def _post(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._retries + 1):
            try:
                return self._post_once(model, body)
            except _TransientError as e:
                if attempt == self._retries:
                    raise GatewayError(f"{model}: {e} (after {attempt + 1} attempts)") from e
                time.sleep(self._backoff * 2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises

    def _post_once(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(
                self._url, json=body, headers={"authorization": f"Bearer {GATEWAY_API_KEY}"}
            )
        except (httpx2.TimeoutException, httpx2.TransportError) as e:
            raise _TransientError(str(e)) from e
        if response.status_code in RETRY_STATUS:
            raise _TransientError(f"HTTP {response.status_code}")
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx2.HTTPError, ValueError) as e:
            raise GatewayError(f"{model}: {e}") from e
        if not isinstance(data, dict):
            raise GatewayError(
                f"{model}: the gateway answered {type(data).__name__}, not an object"
            )
        return data

    def complete(self, model: str, messages: list[dict[str, str]], seed: int) -> str:
        """The assistant's text for `messages` from gateway model `model`."""
        key = self._cache.key(model, messages, seed, backend=self.backend(model))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = self._post(model, {"model": model, "messages": messages, "seed": seed})
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise GatewayError(f"{model}: unexpected completion shape: {e}") from e
        if not isinstance(content, str):
            raise GatewayError(f"{model}: the completion has no text")
        self._cache.put(key, content)
        return content
