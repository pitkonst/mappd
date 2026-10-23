"""A content-addressed response cache: same model, prompt and seed, no second model call."""

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path


class Cache:
    """JSON files under `directory`, one per response, named by the SHA-256 of the request."""

    def __init__(self, directory: Path) -> None:
        """Use (and create on first write) `directory`."""
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self._count = threading.Lock()  # the synth runs cases on several threads

    def key(self, model: str, messages: list[dict[str, str]], seed: int, *, backend: str) -> str:
        """The cache key of one request; `backend` is the model that actually answers it."""
        blob = json.dumps(
            {"model": model, "backend": backend, "messages": messages, "seed": seed},
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> str | None:
        """The stored response, or None."""
        path = self.directory / f"{key}.json"
        if path.is_file():
            with self._count:
                self.hits += 1
            value: str = json.loads(path.read_text(encoding="utf-8"))["response"]
            return value
        with self._count:
            self.misses += 1
        return None

    def put(self, key: str, value: str) -> None:
        """Store a response atomically: concurrent readers see the old file or the new one."""
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{key}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps({"response": value}, ensure_ascii=False))
        Path(tmp).replace(self.directory / f"{key}.json")
