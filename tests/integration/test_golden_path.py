import pytest

from bot.smoke.golden_path import run_golden_path


@pytest.mark.asyncio
async def test_golden_path_stub_repo():
    res = await run_golden_path(force_repo="stub")
    assert res.ok is True
    assert res.records_written >= 1
    assert len(res.records) >= 1
