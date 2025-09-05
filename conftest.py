# Minimal fallback to run async tests if pytest-asyncio doesn't load
import asyncio
import inspect

def pytest_pyfunc_call(pyfuncitem):
    """Run async test functions via a fresh loop."""
    testfunc = pyfuncitem.obj
    if inspect.iscoroutinefunction(testfunc):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(testfunc(**pyfuncitem.funcargs))
        finally:
            loop.close()
        return True
