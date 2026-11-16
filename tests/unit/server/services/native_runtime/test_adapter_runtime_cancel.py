import asyncio

import pytest

from src.server.services.native_runtime.registry import AdapterNativeRuntime


@pytest.mark.asyncio
async def test_cancel_unwinds_a_one_shot_adapter_turn():
    class SlowAdapter:
        async def run(self, prompt, *, session_id=None, cwd=None):
            await asyncio.sleep(30)

    runtime = AdapterNativeRuntime("zcode", adapter=SlowAdapter())
    task = asyncio.create_task(_drain(runtime))
    await asyncio.sleep(0.05)
    await runtime.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _drain(runtime):
    async for _event in runtime.stream_turn("x"):
        pass
