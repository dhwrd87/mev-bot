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


class DBSettings(BaseModel):
    host: str = Field(default="mev-db")
    port: PositiveInt = Field(default=5432)
    name: str = Field(alias="POSTGRES_DB")
    user: str = Field(alias="POSTGRES_USER")
    password: str | None = Field(default=None, alias="POSTGRES_PASSWORD")
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
    max_daily_loss: float = Field(alias="MAX_DAILY_LOSS")       # fraction (0..1)
    max_position_size: float = Field(alias="MAX_POSITION_SIZE") # fraction (0..1)

    @field_validator("max_daily_loss", "max_position_size")
    @classmethod
    def _between_zero_one(cls, v: float) -> float:
        if not (0 < v <= 1):
            raise ValueError("must be in (0,1]")
        return v


class TelemetrySettings(BaseModel):
    discord_webhook: Optional[AnyUrl] = Field(default=None, alias="DISCORD_WEBHOOK")
    prom_pushgateway: Optional[AnyUrl] = Field(default=None, alias="PROM_PUSHGATEWAY")


class ChainSettings(BaseModel):
    chain: Literal["polygon", "ethereum", "base"] = Field(alias="CHAIN")
    chain_id: PositiveInt = Field(alias="CHAIN_ID")
    rpc_primary: AnyUrl = Field(alias="RPC_ENDPOINT_PRIMARY")
    rpc_backup: AnyUrl = Field(alias="RPC_ENDPOINT_BACKUP")


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

        out.setdefault("chain", {
            "CHAIN": env.get("CHAIN"),
            "CHAIN_ID": env.get("CHAIN_ID"),
            "RPC_ENDPOINT_PRIMARY": env.get("RPC_ENDPOINT_PRIMARY"),
            "RPC_ENDPOINT_BACKUP": env.get("RPC_ENDPOINT_BACKUP"),
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


_settings: AppSettings | None = None

def get_settings() -> AppSettings:
    global _settings
    if _settings is None:
        try:
            _settings = AppSettings()  # loads .env if present + process env
        except ValidationError as e:
            details = "\n".join(
                f"- {'.'.join(map(str, err['loc']))}: {err['msg']}"
                for err in e.errors()
            )
            raise SystemExit("Configuration validation failed:\n" + details)
        except Exception as e:
            raise SystemExit(f"Configuration initialization error: {e}")
    return _settings

from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import AnyUrl, Field

class Settings(BaseSettings):
    # chain / rpc
    CHAIN: str = "polygon"
    CHAIN_ID: int = 137
    RPC_ENDPOINT_PRIMARY: AnyUrl
    RPC_ENDPOINT_BACKUP: AnyUrl | None = None

    # private orderflow
    FLASHBOTS_RELAY_URL: str | None = None
    PRIVATE_ORDERFLOW_ENDPOINTS: str | None = None  # comma-sep

    # db
    POSTGRES_HOST: str = "mev-db"
    POSTGRES_DB: str = "mev_bot"
    POSTGRES_USER: str = "mev_user"
    POSTGRES_PASSWORD: str
    POSTGRES_SSLMODE: str = "disable"

    # telemetry & ops
    DISCORD_WEBHOOK: str | None = None
    MAX_DAILY_LOSS: float = 0.10
    MAX_POSITION_SIZE: float = 0.05
    GAS_PRICE_CEIL_GWEI: int = 150
    EMERGENCY_FLAG: bool = False
    AUTHORIZED_IPS: str = "127.0.0.1"  # comma/ranges ok
    X_API_KEYS: str | None = None

    class Config:
        env_file = ".env"
        extra = "ignore"

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore
