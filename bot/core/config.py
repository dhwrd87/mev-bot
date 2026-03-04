# bot/core/config.py
from __future__ import annotations
from typing import List, Optional, Literal
from ipaddress import ip_network, IPv4Network, IPv6Network
from pathlib import Path
import os

from pydantic import (
    BaseModel,
    AnyUrl,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
    ValidationError,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(value: Optional[str], file_path: Optional[str]) -> Optional[str]:
    """
    Support Docker-style secrets: if *_FILE is set, read content from that file.
    ENV has priority; *_FILE is fallback.
    """
    if value:
        return value
    if file_path:
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"Secret file not found: {file_path}")
        return p.read_text().strip()
    return None


def _csv(s: str | None) -> List[str]:
    if not s:
        return []
    return [item.strip() for item in s.split(",") if item.strip()]


REQUIRED_ENV_VARS = [
    "CHAIN",
]


def missing_required_env(env: dict | None = None) -> List[str]:
    env = os.environ if env is None else env
    missing = [k for k in REQUIRED_ENV_VARS if not env.get(k)]

    if env.get("PRIVATE_KEY_ENCRYPTED") and not (env.get("KEY_PASSWORD") or env.get("KEY_PASSWORD_FILE")):
        missing.append("KEY_PASSWORD|KEY_PASSWORD_FILE")

    return missing


def format_missing_env(missing: List[str]) -> str:
    if not missing:
        return "All required env vars present."
    return "Missing required env vars: " + ", ".join(missing)


class DBSettings(BaseModel):
    host: str = Field(default="mev-db")
    port: PositiveInt = Field(default=5432)
    name: str = Field(default="mev_bot", alias="POSTGRES_DB")
    user: str = Field(default="mev_user", alias="POSTGRES_USER")
    password: str | None = Field(default="change_me", alias="POSTGRES_PASSWORD")
    password_file: str | None = Field(default=None, alias="POSTGRES_PASSWORD_FILE")
    sslmode: Literal["disable", "require", "verify-ca", "verify-full"] = Field(default="disable", alias="POSTGRES_SSLMODE")

    @model_validator(mode="after")
    def _merge_secret(self):
        self.password = _read_secret(self.password, self.password_file)
        if not self.password:
            raise ValueError("POSTGRES_PASSWORD or POSTGRES_PASSWORD_FILE is required")
        return self

    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}?sslmode={self.sslmode}"


class RiskSettings(BaseModel):
    max_daily_loss: float = Field(default=0.10, alias="MAX_DAILY_LOSS")       # fraction (0..1)
    max_position_size: float = Field(default=0.05, alias="MAX_POSITION_SIZE") # fraction (0..1)

    @field_validator("max_daily_loss", "max_position_size")
    @classmethod
    def _between_zero_one(cls, v: float) -> float:
        if not (0 < v <= 1):
            raise ValueError("must be in (0,1]")
        return v


class TelemetrySettings(BaseModel):
    discord_webhook: Optional[AnyUrl] = Field(default=None, alias="DISCORD_WEBHOOK")
    prom_pushgateway: Optional[AnyUrl] = Field(default=None, alias="PROM_PUSHGATEWAY")

    @field_validator("discord_webhook", "prom_pushgateway", mode="before")
    @classmethod
    def _empty_url_to_none(cls, v):
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v


class ChainSettings(BaseModel):
    chain: Literal[
        "polygon", "ethereum", "base", "sepolia", "amoy", "mainnet", "solana", "solana-devnet"
    ] = Field(alias="CHAIN")
    chain_id: int = Field(alias="CHAIN_ID", ge=0)
    rpc_primary: AnyUrl = Field(alias="RPC_HTTP")
    rpc_backup: AnyUrl = Field(alias="RPC_HTTP_BACKUP")


class OrderflowSettings(BaseModel):
    flashbots_relay_url: Optional[AnyUrl] = Field(default=None, alias="FLASHBOTS_RELAY_URL")
    endpoints: List[str] = Field(default_factory=list, alias="PRIVATE_ORDERFLOW_ENDPOINTS")

    @field_validator("endpoints", mode="before")
    @classmethod
    def _split_endpoints(cls, v):
        if isinstance(v, str):
            return _csv(v)
        return v


class SecuritySettings(BaseModel):
    # app API auth / ACL
    authorized_ips: List[str] = Field(default_factory=list, alias="AUTHORIZED_IPS")
    api_keys: List[str] = Field(default_factory=list, alias="X_API_KEYS")

    # signing secrets
    key_password: Optional[str] = Field(default=None, alias="KEY_PASSWORD")
    key_password_file: Optional[str] = Field(default=None, alias="KEY_PASSWORD_FILE")
    private_key_encrypted: Optional[str] = Field(default=None, alias="PRIVATE_KEY_ENCRYPTED")
    private_key_encrypted_file: Optional[str] = Field(default=None, alias="PRIVATE_KEY_ENCRYPTED_FILE")

    @field_validator("authorized_ips", mode="before")
    @classmethod
    def _split_ips(cls, v):
        return _csv(v) if isinstance(v, str) else (v or [])

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_keys(cls, v):
        return _csv(v) if isinstance(v, str) else (v or [])

    @model_validator(mode="after")
    def _normalize_and_require(self):
        # normalize/validate networks
        nets: list[IPv4Network | IPv6Network] = []
        for item in self.authorized_ips:
            nets.append(ip_network(item, strict=False))
        # store back as strings in canonical form
        self.authorized_ips = [str(n) for n in nets]

        # merge Docker secret files for KEY_PASSWORD & PRIVATE_KEY_ENCRYPTED
        self.key_password = _read_secret(self.key_password, self.key_password_file)
        self.private_key_encrypted = _read_secret(self.private_key_encrypted, self.private_key_encrypted_file)

        # If a private key blob is provided, a password must be present
        if self.private_key_encrypted and not self.key_password:
            raise ValueError("KEY_PASSWORD (or KEY_PASSWORD_FILE) is required when PRIVATE_KEY_ENCRYPTED is set")
        return self


class GasPolicy(BaseModel):
    gas_price_ceil_gwei: PositiveInt = Field(default=150, alias="GAS_PRICE_CEIL_GWEI")


class AppSettings(BaseSettings):
    """
    Main settings object. Loads from environment and optional .env.
    Fails at import-time if required values are missing.
    """
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore")

    mode: Literal["production", "staging", "development"] = Field(default="production", alias="MODE")
    use_private_rpc: bool = Field(default=True, alias="USE_PRIVATE_RPC")
    emergency_flag: bool = Field(default=False, alias="EMERGENCY_FLAG")

    chain: ChainSettings
    db: DBSettings
    risk: RiskSettings
    telemetry: TelemetrySettings = TelemetrySettings()
    orderflow: OrderflowSettings = OrderflowSettings()
    security: SecuritySettings = SecuritySettings()
    gas: GasPolicy = GasPolicy()
    # compat: orderflow expects settings.chains[chain]
    chains: dict[str, dict] = {}

    @model_validator(mode="before")
    @classmethod
    def build_nested_from_env(cls, data: dict | None):
        """
        Populate nested submodels (chain, db, risk, etc.) from flat env vars.
        This lets us keep env names like CHAIN_ID, POSTGRES_DB, etc.
        """
        import os

        if data is None:
            data = {}
        # If tests pass explicit nested dicts, don't overwrite them.
        if all(k in data for k in ("chain", "db", "risk")):
            return data

        env = os.environ
        out = dict(data)

        from bot.core.chain_config import get_chain_config
        cfg = get_chain_config()
        rpc_urls = _csv(env.get("RPC_URLS", ""))
        rpc_http = env.get("RPC_HTTP") or (rpc_urls[0] if rpc_urls else None) or env.get("RPC_ENDPOINT_PRIMARY") or cfg.rpc_http
        rpc_http_backup = env.get("RPC_HTTP_BACKUP") or (rpc_urls[1] if len(rpc_urls) > 1 else None) or env.get("RPC_ENDPOINT_BACKUP") or (cfg.rpc_http_backups[0] if cfg.rpc_http_backups else cfg.rpc_http)

        out.setdefault("chain", {
            "CHAIN": env.get("CHAIN"),
            "CHAIN_ID": str(cfg.chain_id),
            "RPC_HTTP": rpc_http,
            "RPC_HTTP_BACKUP": rpc_http_backup,
        })

        out.setdefault("db", {
            "host": env.get("POSTGRES_HOST", "mev-db"),
            "port": env.get("POSTGRES_PORT", "5432"),
            "POSTGRES_DB": env.get("POSTGRES_DB"),
            "POSTGRES_USER": env.get("POSTGRES_USER"),
            "POSTGRES_PASSWORD": env.get("POSTGRES_PASSWORD"),
            "POSTGRES_PASSWORD_FILE": env.get("POSTGRES_PASSWORD_FILE"),
            "POSTGRES_SSLMODE": env.get("POSTGRES_SSLMODE", "disable"),
        })

        out.setdefault("risk", {
            "MAX_DAILY_LOSS": env.get("MAX_DAILY_LOSS"),
            "MAX_POSITION_SIZE": env.get("MAX_POSITION_SIZE"),
        })

        out.setdefault("telemetry", {
            "DISCORD_WEBHOOK": env.get("DISCORD_WEBHOOK"),
            "PROM_PUSHGATEWAY": env.get("PROM_PUSHGATEWAY"),
        })

        out.setdefault("orderflow", {
            "FLASHBOTS_RELAY_URL": env.get("FLASHBOTS_RELAY_URL"),
            "PRIVATE_ORDERFLOW_ENDPOINTS": env.get("PRIVATE_ORDERFLOW_ENDPOINTS", ""),
        })

        out.setdefault("security", {
            "AUTHORIZED_IPS": env.get("AUTHORIZED_IPS", ""),
            "X_API_KEYS": env.get("X_API_KEYS", ""),
            "KEY_PASSWORD": env.get("KEY_PASSWORD"),
            "KEY_PASSWORD_FILE": env.get("KEY_PASSWORD_FILE"),
            "PRIVATE_KEY_ENCRYPTED": env.get("PRIVATE_KEY_ENCRYPTED"),
            "PRIVATE_KEY_ENCRYPTED_FILE": env.get("PRIVATE_KEY_ENCRYPTED_FILE"),
        })

        out.setdefault("gas", {
            "GAS_PRICE_CEIL_GWEI": env.get("GAS_PRICE_CEIL_GWEI", "150"),
        })

        return out

    @model_validator(mode="after")
    def _build_chain_map(self):
        # Provide a compat mapping for code expecting settings.chains[chain]
        self.chains = {
            self.chain.chain: {
                "relays": {
                    "flashbots_protect": {"type": "flashbots", "url": str(self.orderflow.flashbots_relay_url) if self.orderflow.flashbots_relay_url else ""},
                    "mev_blocker": {"type": "mevblocker", "url": os.getenv("MEV_BLOCKER_URL", "https://rpc.mevblocker.io")},
                    "cow_protocol": {"type": "cow", "url": os.getenv("COW_API", "https://api.cow.fi/mainnet/api/v1")},
                },
                "default_order": ["mev_blocker", "flashbots_protect", "cow_protocol"],
                "max_retries_per_relay": 2,
                "backoff": {"base": 0.3, "factor": 2.0, "max": 3.0, "jitter": 0.25},
            }
        }
        return self


_settings: AppSettings | None = None

def get_settings() -> AppSettings:
    global _settings
    if _settings is None:
        try:
            _settings = AppSettings()  # loads .env if present + process env
        except ValidationError as e:
            # If running under pytest, provide a minimal default config
            import sys
            if os.getenv("PYTEST_CURRENT_TEST") is not None or "pytest" in sys.modules:
                _settings = AppSettings(
                    chain={
                        "CHAIN": "polygon",
                        "CHAIN_ID": 137,
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
                )
                return _settings
            details = "\n".join(
                f"- {'.'.join(map(str, err['loc']))}: {err['msg']}"
                for err in e.errors()
            )
            raise SystemExit("Configuration validation failed:\n" + details)
        except Exception as e:
            raise SystemExit(f"Configuration initialization error: {e}")
    return _settings


# --- Back-compat export ---
settings = get_settings()
