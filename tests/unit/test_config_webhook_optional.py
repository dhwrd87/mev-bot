from bot.core.config import AppSettings


def test_config_loads_with_empty_discord_webhook():
    settings = AppSettings(
        chain={
            "CHAIN": "sepolia",
            "CHAIN_ID": 11155111,
            "RPC_HTTP": "http://localhost:8545",
            "RPC_HTTP_BACKUP": "http://localhost:8545",
        },
        db={
            "host": "localhost",
            "port": 5432,
            "POSTGRES_DB": "mev_bot",
            "POSTGRES_USER": "mev_user",
            "POSTGRES_PASSWORD": "test",
            "POSTGRES_SSLMODE": "disable",
        },
        risk={
            "MAX_DAILY_LOSS": 0.1,
            "MAX_POSITION_SIZE": 0.05,
        },
        telemetry={
            "DISCORD_WEBHOOK": "",
        },
    )
    assert settings.telemetry.discord_webhook is None
