# Minimal fallback to run async tests if pytest-asyncio doesn't load
import asyncio
import inspect
from pathlib import Path

import pytest

def pytest_pyfunc_call(pyfuncitem):
    """Run async test functions via a fresh loop."""
    testfunc = pyfuncitem.obj
    if inspect.iscoroutinefunction(testfunc):
        sig = inspect.signature(testfunc)
        allowed_kwargs = {
            name: value
            for name, value in pyfuncitem.funcargs.items()
            if name in sig.parameters
        }
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(testfunc(**allowed_kwargs))
        finally:
            loop.close()
        return True


def pytest_collection_modifyitems(items):
    for item in items:
        p = Path(str(item.fspath))
        name = p.name.lower()
        if "integration" in p.parts:
            item.add_marker(pytest.mark.integration)
        if "contract" in p.parts:
            item.add_marker(pytest.mark.contract)
        if "e2e" in name:
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.integration)
        if not any(
            item.get_closest_marker(m) is not None
            for m in ("integration", "contract", "e2e")
        ):
            item.add_marker(pytest.mark.unit)
