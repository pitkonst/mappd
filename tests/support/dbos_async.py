"""Run a coroutine that awaits DBOS APIs without breaking the session's DBOS.

DBOS's async APIs install DBOS's thread pool as the running loop's default executor, and
asyncio.run shuts the default executor down when the loop closes: afterwards every DBOS call in
this process fails with "cannot schedule new futures after shutdown". Swap in a throwaway
executor before the loop closes.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    async def guarded() -> T:
        try:
            return await coroutine
        finally:
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))

    return asyncio.run(guarded())


@asynccontextmanager
async def lifespan(_app: object) -> AsyncIterator[None]:
    """The same swap for a test app whose handlers await DBOS (`with TestClient(app)`)."""
    try:
        yield
    finally:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
