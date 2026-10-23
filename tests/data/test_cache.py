from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from mappd.data.cache import Cache

if TYPE_CHECKING:
    from pathlib import Path


def test_put_get_and_counters(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    key = cache.key("synth", [{"role": "user", "content": "hi"}], 42, backend="mock")
    assert cache.get(key) is None
    cache.put(key, "hello")
    assert cache.get(key) == "hello"
    assert (cache.hits, cache.misses) == (1, 1)


def test_key_depends_on_model_messages_and_seed(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    m = [{"role": "user", "content": "hi"}]
    keys = {
        cache.key("synth", m, 1, backend="mock"),
        cache.key("check", m, 1, backend="mock"),
        cache.key("synth", m, 2, backend="mock"),
        cache.key("synth", [{"role": "user", "content": "ho"}], 1, backend="mock"),
        cache.key("synth", m, 1, backend="gpt-real"),
    }
    assert len(keys) == 5


def test_concurrent_writers_never_expose_a_partial_file(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    key = cache.key("judge", [{"role": "user", "content": "x"}], 0, backend="b")
    value = "v" * 200_000

    def hammer() -> None:
        for _ in range(50):
            cache.put(key, value)
            assert cache.get(key) == value

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(hammer) for _ in range(8)]:
            future.result()
