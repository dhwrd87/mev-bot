def test_runtime_dependency_imports():
    # Fail fast when runtime deps are missing from the test image/environment.
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    import pydantic  # noqa: F401
    import pydantic_settings  # noqa: F401
    import prometheus_client  # noqa: F401
    import psycopg  # noqa: F401
    import redis  # noqa: F401
    import requests  # noqa: F401
    import web3  # noqa: F401
    import websockets  # noqa: F401
