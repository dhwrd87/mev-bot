import os, time, asyncio, logging, contextlib, secrets
from typing import Optional, List, Any, Dict
from urllib.parse import urlparse
from pathlib import Path
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from web3 import Web3, HTTPProvider
import requests
import aiohttp
import redis.asyncio as aioredis

from bot.api.metrics import metrics_endpoint
from bot.mempool.monitor import WSMempoolMonitor
from bot.telemetry.alerts import AlertManager, AlertCfg
from bot.strategy.stealth import StealthStrategy
from bot.core.config import get_settings, missing_required_env, format_missing_env
from bot.core.telemetry import rpc_gettx_ok_total, rpc_gettx_errors_total
from bot.core.chain_config import get_chain_config, _reset_chain_config_cache_for_tests
from bot.core.chain_adapter import parse_chain_selection, validate_chain_selection
from bot.core.state import BotState, build_state_machine, parse_bot_state, set_runtime_state
from bot.core.invariants import get_runtime_invariants
from bot.core.operator_control import get_operator_state
from bot.core.canonical_chain import canonicalize_chain_target
from bot.core.canonical_chain import canonicalize_labels
from bot.core.switch_controller import SwitchController
from bot.core.sol_runtime import SolSlotTracker
from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.router import TradeRouter
from bot.core.types_dex import TradeIntent
from bot.net.instrumented_rpc import AsyncInstrumentedRpcClient
from ops.metrics import (
    seed_default_series,
    set_runtime_bot_state,
    set_desired_bot_state,
    start_metrics_http_server,
    set_heartbeat,
    set_chain_head,
    set_chain_slot,
    set_head_lag,
    set_slot_lag,
    record_stream_events_observed,
)
from ops.health_snapshot_writer import HealthSnapshotWriter

from bot.core.telemetry import (
    canonical_metric_labels,
    seed_zeroes,
    mempool_unique_tx_total,
    private_submit_attempts, private_submit_success, private_submit_errors,
    stealth_decisions_total, orchestrator_decisions_total, risk_blocks_total,
    set_bot_state, record_bot_state_transition,
)

# ---- App & /metrics ----
app = FastAPI(title="MEV Bot API")
app.add_api_route("/metrics/", metrics_endpoint, methods=["GET"])
app.add_api_route("/metrics", metrics_endpoint, methods=["GET"])

_monitor: Optional[WSMempoolMonitor] = None
_sol_tracker: Optional[SolSlotTracker] = None
_runtime_monitor_task: Optional[asyncio.Task] = None
_rpc_ping_task: Optional[asyncio.Task] = None
_health_snapshot_writer: Optional[HealthSnapshotWriter] = None
_dex_registry: Optional[DEXPackRegistry] = None
_dex_router: Optional[TradeRouter] = None
_rpc_metrics_client: Optional[AsyncInstrumentedRpcClient] = None

log = logging.getLogger(__name__)


def _ops_dsn_candidates() -> List[str]:
    candidates: List[str] = []
    dsn = os.getenv("DATABASE_URL", "").strip()
    if dsn:
        candidates.append(dsn)

    user = os.getenv("POSTGRES_USER", "mevbot")
    pwd = os.getenv("POSTGRES_PASSWORD", "mevbot_pw")
    db = os.getenv("POSTGRES_DB", "mevbot")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    candidates.append(f"postgresql://{user}:{pwd}@{host}:{port}/{db}")

    # Conservative runtime fallbacks for this stack's local defaults.
    candidates.append("postgresql://mevbot:mevbot_pw@postgres:5432/mevbot")
    candidates.append("postgresql://mevbot:mevbot_pw@127.0.0.1:5432/mevbot")
    # Deduplicate while preserving order.
    seen = set()
    deduped: List[str] = []
    for d in candidates:
        if d in seen:
            continue
        seen.add(d)
        deduped.append(d)
    return deduped


def _ensure_ops_state_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_state(
          k TEXT PRIMARY KEY,
          v TEXT NOT NULL,
          updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        INSERT INTO ops_state(k, v)
        VALUES ('paused', 'false')
        ON CONFLICT (k) DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO ops_state(k, v)
        VALUES ('mode', 'paper')
        ON CONFLICT (k) DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO ops_state(k, v)
        VALUES ('kill_switch', 'false')
        ON CONFLICT (k) DO NOTHING
        """
    )
    default_chain = str(os.getenv("CHAIN", "sepolia")).strip().lower() or "sepolia"
    conn.execute(
        """
        INSERT INTO ops_state(k, v)
        VALUES ('chain_selection', %s)
        ON CONFLICT (k) DO NOTHING
        """,
        (f"EVM:{default_chain}",),
    )


def _ensure_operator_events_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_events(
          op_id TEXT PRIMARY KEY,
          ts TIMESTAMPTZ DEFAULT now(),
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          value TEXT,
          reason TEXT,
          applied BOOLEAN NOT NULL DEFAULT false,
          error TEXT,
          desired_state TEXT,
          desired_mode TEXT,
          desired_chain TEXT,
          effective_state TEXT,
          effective_chain TEXT,
          created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_operator_events_created_at
          ON operator_events(created_at DESC)
        """
    )


def _db_connect():
    import psycopg

    last_err = None
    for dsn in _ops_dsn_candidates():
        try:
            return psycopg.connect(dsn, autocommit=True)
        except Exception as e:
            last_err = e
    raise last_err


def _read_paused_flag() -> bool:
    try:
        with _db_connect() as conn:
            _ensure_ops_state_table(conn)
            row = conn.execute("SELECT v FROM ops_state WHERE k='paused'").fetchone()
            if not row:
                return False
            return str(row[0]).strip().lower() == "true"
    except Exception as e:
        log.warning("Failed to read paused flag from DB: %s", e)
        return False


def _write_paused_flag(value: bool) -> None:
    with _db_connect() as conn:
        _ensure_ops_state_table(conn)
        conn.execute(
            """
            INSERT INTO ops_state(k, v, updated_at)
            VALUES ('paused', %s, now())
            ON CONFLICT (k) DO UPDATE SET
                v=EXCLUDED.v,
                updated_at=now()
            """,
            ("true" if value else "false",),
        )


def _read_ops_state_values() -> Dict[str, str]:
    try:
        with _db_connect() as conn:
            _ensure_ops_state_table(conn)
            rows = conn.execute("SELECT k, v FROM ops_state").fetchall()
            out: Dict[str, str] = {}
            for r in rows:
                out[str(r[0])] = str(r[1])
            return out
    except Exception as e:
        log.warning("failed reading ops_state values: %s", e)
        return {}


def _read_ops_value(key: str, default: str) -> str:
    raw = _read_ops_state_values()
    v = str(raw.get(key, default) or "").strip()
    return v if v else default


def _effective_chain_key() -> str:
    cfg = _get_chain_snapshot()
    fam = str(os.getenv("CHAIN_FAMILY", "evm")).strip().upper() or "EVM"
    return f"{fam}:{str(cfg.get('chain', 'unknown')).strip().lower()}"


def _mask_url(url: str) -> str:
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        p = urlparse(s)
        if p.scheme and p.hostname:
            return f"{p.scheme}://{p.hostname}"
    except Exception:
        return s
    return s


def _set_chain_scoped_stream_env(*, family: str, chain: str) -> str:
    chain_key = f"{str(family).strip().lower()}:{str(chain).strip().lower()}"
    stream_key = f"mempool:{chain_key}:pending:txs"
    os.environ["REDIS_STREAM"] = stream_key
    os.environ["MEMPOOL_STREAM"] = stream_key
    os.environ["CANDIDATES_STREAM_PREFIX"] = f"candidates:{chain_key}"
    return stream_key


def _supported_families() -> set[str]:
    raw = str(os.getenv("SUPPORTED_FAMILIES", "evm")).strip().lower()
    vals = [x.strip().lower() for x in raw.split(",") if x.strip()]
    out = {v for v in vals if v in {"evm", "sol"}}
    return out or {"evm"}


def _load_chain_registry() -> Dict[str, Dict[str, Any]]:
    p = Path(os.getenv("CHAINS_CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "chains.yaml")))
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    chains = raw.get("chains", {})
    if not isinstance(chains, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in chains.items():
        if not isinstance(v, dict):
            continue
        out[str(k).strip().lower()] = dict(v)
    return out


def _effective_state_payload() -> Dict[str, Any]:
    cfg = _get_chain_snapshot()
    sm = getattr(app.state, "bot_state_machine", None)
    effective_state = sm.state.value if sm else ("PAUSED" if bool(getattr(app.state, "paused", False)) else "READY")
    return {
        "effective_state": effective_state,
        "effective_chain": str(cfg.get("chain", "unknown")),
        "resolved_chain_id": int(cfg.get("chain_id", 0) or 0),
        "rpc_url": str(cfg.get("rpc_http_selected", "") or ""),
        "head": int(getattr(app.state, "_chain_obs_last_head", 0) or 0),
        "slot": int(getattr(app.state, "_chain_obs_last_slot", 0) or 0),
        "lag_blocks": int(getattr(app.state, "_chain_obs_max_head", 0) or 0) - int(getattr(app.state, "_chain_obs_last_head", 0) or 0),
        "lag_slots": int(getattr(app.state, "_chain_obs_max_slot", 0) or 0) - int(getattr(app.state, "_chain_obs_last_slot", 0) or 0),
    }


def _last_operator_event_fields() -> Dict[str, Any]:
    try:
        with _db_connect() as conn:
            _ensure_operator_events_table(conn)
            applied = conn.execute(
                """
                SELECT op_id
                FROM operator_events
                WHERE applied IS TRUE
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            failed = conn.execute(
                """
                SELECT op_id, error
                FROM operator_events
                WHERE (applied IS FALSE OR error IS NOT NULL)
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            return {
                "last_op_id_applied": str(applied[0]) if applied else None,
                "last_op_apply_error": str(failed[1]) if failed and failed[1] else None,
                "last_op_error_id": str(failed[0]) if failed else None,
            }
    except Exception as e:
        log.warning("failed reading operator_events summary: %s", e)
        return {
            "last_op_id_applied": None,
            "last_op_apply_error": f"operator_events_read_failed:{e}",
            "last_op_error_id": None,
        }


def _new_op_id() -> str:
    return secrets.token_hex(12)


def _base_operator_result(*, op_id: str, ok: bool, applied: bool, message: str) -> Dict[str, Any]:
    s = status()
    return {
        "op_id": op_id,
        "ok": bool(ok),
        "applied": bool(applied),
        "message": message,
        "desired_state": s.get("desired_state"),
        "effective_state": s.get("effective_state"),
        "desired_chain": s.get("desired_chain"),
        "effective_chain": s.get("effective_chain"),
        "last_op_id_applied": s.get("last_op_id_applied"),
        "last_op_apply_error": s.get("last_op_apply_error"),
    }


class KillSwitchPayload(BaseModel):
    enabled: bool


class ModePayload(BaseModel):
    mode: str


class ChainPayload(BaseModel):
    chain_key: str


class LiveCommitPayload(BaseModel):
    token: str


def _get_chain_snapshot() -> dict:
    try:
        cfg = get_chain_config()
        return {
            "chain": cfg.chain,
            "chain_id": cfg.chain_id,
            "rpc_http_selected": cfg.rpc_http_selected,
            "rpc_http_backups_selected": cfg.rpc_http_backups,
            "ws_endpoints_selected": cfg.ws_endpoints_selected,
        }
    except Exception:
        ws_env = [x.strip() for x in str(os.getenv("WS_ENDPOINTS_EXTRA", "")).split(",") if x.strip()]
        rpc_env = [x.strip() for x in str(os.getenv("RPC_HTTP_EXTRA", "")).split(",") if x.strip()]
        chain = str(os.getenv("CHAIN", "unknown")).strip().lower() or "unknown"
        chain_id = 0
        with contextlib.suppress(Exception):
            registry = _load_chain_registry()
            cfg = registry.get(chain, {})
            if isinstance(cfg, dict):
                chain_id = int(cfg.get("chain_id", 0) or 0)
        return {
            "chain": chain,
            "chain_id": chain_id,
            "rpc_http_selected": rpc_env[0] if rpc_env else "",
            "rpc_http_backups_selected": rpc_env[1:] if len(rpc_env) > 1 else [],
            "ws_endpoints_selected": ws_env,
        }


def _dex_override_signature(op_state: dict, *, family: str, chain: str, network: str) -> tuple:
    overrides = op_state.get("enabled_dex_overrides")
    allow = []
    deny = []
    if isinstance(overrides, dict):
        allow = [str(x).strip().lower() for x in overrides.get("allowlist", []) if str(x).strip()]
        deny = [str(x).strip().lower() for x in overrides.get("denylist", []) if str(x).strip()]
    if not allow:
        allow = [str(x).strip().lower() for x in op_state.get("dex_packs_enabled", []) if str(x).strip()]
    deny = sorted(set(deny + [str(x).strip().lower() for x in op_state.get("dex_packs_disabled", []) if str(x).strip()]))
    return (
        str(family).strip().lower(),
        str(chain).strip().lower(),
        str(network).strip().lower(),
        tuple(sorted(set(allow))),
        tuple(sorted(set(deny))),
    )


async def _maybe_reload_dex_router(op_state: dict) -> None:
    global _dex_registry, _dex_router
    if _dex_registry is None:
        return

    cfg = _get_chain_snapshot()
    family = str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm"
    chain = str(cfg.get("chain", "unknown")).strip().lower() or "unknown"
    _, _, network = canonicalize_labels(family=family, chain=chain)
    sig = _dex_override_signature(op_state, family=family, chain=chain, network=network)
    if sig == getattr(app.state, "_dex_overrides_sig", None):
        return

    try:
        _transition_state(BotState.PAUSED, actor="system", reason="dex_reconfigure_pause", force=True)
        _dex_registry.reload(family=family, chain=chain, network=network)
        if _dex_router is None:
            _dex_router = TradeRouter(
                registry=_dex_registry,
                quote_timeout_ms=int(os.getenv("ROUTER_QUOTE_TIMEOUT_MS", "800")),
            )
        app.state.dex_registry = _dex_registry
        app.state.dex_router = _dex_router
        app.state._dex_overrides_sig = sig
        app.state._dex_enabled = _dex_registry.enabled_names()
        log.info("dex registry reloaded chain=%s family=%s enabled=%s", chain, family, app.state._dex_enabled)
        _transition_state(BotState.READY, actor="system", reason="dex_reconfigure_ready", force=True)
    except Exception as e:
        log.warning("dex registry reload failed: %s", e)
        _transition_state(BotState.DEGRADED, actor="system", reason="dex_reconfigure_failed", force=True)


def _transition_state(
    target: str | BotState,
    *,
    actor: str,
    reason: str,
    force: bool = False,
) -> dict:
    state_machine = getattr(app.state, "bot_state_machine", None)
    if not state_machine:
        app.state.bot_state_machine = build_state_machine()
        state_machine = app.state.bot_state_machine

    rec = state_machine.transition(target, actor=actor, reason=reason, force=force)
    record_bot_state_transition(rec.from_state, rec.to_state, rec.reason)
    to_state = parse_bot_state(rec.to_state)
    set_runtime_state(to_state)

    app.state.paused = to_state == BotState.PAUSED
    _write_paused_flag(app.state.paused)
    return {
        "ok": True,
        "state": rec.to_state,
        "paused": app.state.paused,
        "transition": {
            "ts_ms": rec.ts_ms,
            "actor": rec.actor,
            "reason": rec.reason,
            "from": rec.from_state,
            "to": rec.to_state,
        },
    }


async def _reload_chain_runtime(selection_name: str) -> dict:
    global _monitor, _sol_tracker

    sel = parse_chain_selection(selection_name)
    os.environ["CHAIN_FAMILY"] = sel.family.lower()
    os.environ["CHAIN"] = sel.chain
    chain_key = f"{sel.family.lower()}:{sel.chain}"
    stream_key = _set_chain_scoped_stream_env(family=sel.family, chain=sel.chain)
    _reset_chain_config_cache_for_tests()

    if _monitor:
        await _monitor.stop()
        _monitor = None
    if _sol_tracker:
        await _sol_tracker.stop()
        _sol_tracker = None

    if sel.family == "EVM":
        cfg = get_chain_config()
        app.state.w3 = Web3(HTTPProvider(cfg.rpc_http_selected)) if cfg.rpc_http_selected else None
        if cfg.ws_endpoints_selected:
            _monitor = WSMempoolMonitor(
                endpoints=cfg.ws_endpoints_selected,
                metrics_port=None,
                redis_stream=stream_key,
                redis_url=os.getenv("REDIS_URL", "redis://mev-redis:6379/0"),
            )
            asyncio.create_task(_monitor.start())
        return {
            "family": sel.family,
            "chain": cfg.chain,
            "chain_key": chain_key,
            "chain_id": cfg.chain_id,
            "rpc_http_selected": cfg.rpc_http_selected,
            "ws_endpoints_selected": cfg.ws_endpoints_selected,
            "redis_stream": stream_key,
        }

    # SOL-family runtime support is intentionally limited to config/env selection.
    app.state.w3 = None
    sol_endpoint = str(os.getenv("SOL_RPC_HTTP", "")).strip()
    if not sol_endpoint:
        try:
            cfg = get_chain_config()
            sol_endpoint = str(cfg.rpc_http_selected or "").strip()
        except Exception:
            sol_endpoint = ""
    if sol_endpoint:
        _sol_tracker = SolSlotTracker(
            endpoint=sol_endpoint,
            on_slot=_on_sol_slot,
            poll_s=float(os.getenv("SOL_SLOT_POLL_S", "2.0")),
        )
        await _sol_tracker.start()
    return {
        "family": sel.family,
        "chain": sel.chain,
        "chain_key": chain_key,
        "chain_id": int(get_chain_config().chain_id),
        "rpc_http_selected": sol_endpoint,
        "ws_endpoints_selected": [],
        "redis_stream": stream_key,
    }


def _emit_chain_switch_labels_now(*, state: BotState, mode: str) -> None:
    cfg = _get_chain_snapshot()
    family = str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm"
    chain = str(cfg.get("chain", "unknown")).strip().lower() or "unknown"
    set_runtime_bot_state(family=family, chain=chain, state=state.value, mode=mode)
    set_heartbeat(family=family, chain=chain, unix_ts=time.time(), strategy="default", dex="unknown", provider="unknown")
    if _health_snapshot_writer is not None:
        _health_snapshot_writer.maybe_write(
            family=family,
            chain=chain,
            state=state.value,
            mode=mode,
            force=True,
        )


def _validate_dex_profiles_for_chain(*, family: str, chain: str) -> dict:
    if _dex_registry is None:
        return {"ok": True, "validated": 0, "errors": []}
    _, _, network = canonicalize_labels(family=family, chain=chain)
    return _dex_registry.validate_enabled_pack_configs(family=family, chain=chain, network=network)


def _default_verify_intent(*, family: str, chain: str, network: str, dex: str) -> TradeIntent:
    if family == "sol":
        return TradeIntent(
            family=family,
            chain=chain,
            network=network,
            dex_preference=dex,
            token_in=str(os.getenv("VERIFY_SOL_TOKEN_IN", "So11111111111111111111111111111111111111112")),
            token_out=str(os.getenv("VERIFY_SOL_TOKEN_OUT", "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU")),
            amount_in=int(os.getenv("VERIFY_SOL_AMOUNT_IN", "1000000")),
            slippage_bps=int(os.getenv("VERIFY_SLIPPAGE_BPS", "100")),
            ttl_s=30,
            strategy="chain_switch_verify",
        )
    return TradeIntent(
        family=family,
        chain=chain,
        network=network,
        dex_preference=dex,
        token_in=str(os.getenv("VERIFY_EVM_TOKEN_IN", "0x4200000000000000000000000000000000000006")),
        token_out=str(os.getenv("VERIFY_EVM_TOKEN_OUT", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")),
        amount_in=int(os.getenv("VERIFY_EVM_AMOUNT_IN", "1000000")),
        slippage_bps=int(os.getenv("VERIFY_SLIPPAGE_BPS", "100")),
        ttl_s=30,
        strategy="chain_switch_verify",
    )


def _warm_dex_pack_cache(*, family: str, chain: str) -> dict:
    if _dex_registry is None:
        return {"ok": True, "attempted": 0, "ok_count": 0, "errors": []}
    _, _, network = canonicalize_labels(family=family, chain=chain)
    _dex_registry.reload(family=family, chain=chain, network=network)
    packs = _dex_registry.list()
    if not packs:
        return {"ok": True, "attempted": 0, "ok_count": 0, "errors": []}
    errors = []
    ok_count = 0
    for pack in packs:
        try:
            intent = _default_verify_intent(family=family, chain=chain, network=network, dex=pack.name())
            q = pack.quote(intent)
            if int(getattr(q, "expected_out", 0) or 0) <= 0:
                raise RuntimeError("expected_out<=0")
            ok_count += 1
        except Exception as e:
            errors.append(f"{pack.name()}:{e}")
    return {"ok": not errors, "attempted": len(packs), "ok_count": ok_count, "errors": errors}

async def start_mempool_publisher():
    if not K.WS_ENDPOINTS:
        log.warning("No WS endpoints configured; mempool publisher disabled")
        return
    r = aioredis.from_url(os.getenv("REDIS_URL","redis://redis:6379/0"))
    rpc = RpcClient()  # reuses K.RPC_HTTP
    stream = os.getenv("MEMPOOL_STREAM","mempool:pending:txs")
    log.info("WS publisher starting: %d endpoints -> stream %s", len(K.WS_ENDPOINTS), stream)
    await ingest_to_queue(r, stream, K.WS_ENDPOINTS, rpc)

# ---- Debug: seed visible series at boot ----
@app.on_event("startup")
async def _warm_prom():
    seed_zeroes()

# ---- Startup / Shutdown ----
def _ws_env_endpoints() -> List[str]:
    return get_chain_config().ws_endpoints

def _rpc_http_from_env() -> str:
    return get_chain_config().rpc_http

@app.on_event("startup")
async def _startup():
    global _health_snapshot_writer, _dex_registry, _dex_router, _rpc_ping_task, _rpc_metrics_client
    logging.basicConfig(level=logging.INFO)
    start_metrics_http_server()
    seed_default_series(
        family=str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm",
        chain=str(os.getenv("CHAIN", "unknown")).strip().lower() or "unknown",
    )
    _health_snapshot_writer = HealthSnapshotWriter(
        path=os.getenv("HEALTH_SNAPSHOT_PATH", "ops/health_snapshot.json"),
        interval_s=float(os.getenv("HEALTH_SNAPSHOT_INTERVAL_S", "10")),
    )
    op_state_path = str(
        os.getenv("OPERATOR_STATE_PATH", os.getenv("OPERATOR_STATE_FILE", "/app/ops/operator_state.json"))
    ).strip()
    _dex_registry = DEXPackRegistry(operator_state_path=op_state_path)
    _dex_router = TradeRouter(
        registry=_dex_registry,
        quote_timeout_ms=int(os.getenv("ROUTER_QUOTE_TIMEOUT_MS", "800")),
    )
    app.state.dex_registry = _dex_registry
    app.state.dex_router = _dex_router
    app.state._dex_overrides_sig = None
    app.state._dex_enabled = []
    app.state.switch_controller = SwitchController()
    app.state.switch_controller.effective_chain = _effective_chain_key()

    missing = missing_required_env()
    if missing:
        logging.error(format_missing_env(missing))

    # Validate full settings (will raise SystemExit on invalid config)
    app.state.settings = get_settings()
    _set_chain_scoped_stream_env(
        family=str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm",
        chain=str(os.getenv("CHAIN", "sepolia")).strip().lower() or "sepolia",
    )

    app.state.alerts = AlertManager(AlertCfg(
        webhook=os.getenv("DISCORD_WEBHOOK",""),
        service=os.getenv("SERVICE_NAME","mev-bot"),
        enabled=os.getenv("ALERTS_ENABLED","true").lower() == "true",
        default_cooldown_s=int(os.getenv("ALERTS_DEFAULT_COOLDOWN","60")),
    ))

    rpc = _rpc_http_from_env()
    app.state.w3 = Web3(HTTPProvider(rpc)) if rpc else None
    if app.state.w3 and not app.state.w3.is_connected():
        logging.warning("[startup] cannot connect to RPC %s", rpc)
    app.state._chain_id_validation_error = ""
    app.state._chain_id_expected = None
    app.state._chain_id_actual = None

    endpoints = _ws_env_endpoints()
    global _monitor, _sol_tracker
    family_now = str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm"
    if family_now == "evm" and endpoints:
        _monitor = WSMempoolMonitor(
            endpoints=endpoints,
            metrics_port=None,
            redis_stream=os.getenv("REDIS_STREAM","mempool:pending:txs"),
            redis_url=os.getenv("REDIS_URL","redis://mev-redis:6379/0"),
        )
        asyncio.create_task(_monitor.start())
        logging.info("WSMempoolMonitor starting: chain=%s endpoints=%s", get_chain_config().chain, endpoints)
    elif family_now == "evm":
        logging.warning("No WS_POLYGON_* endpoints set; mempool monitor not started.")
    else:
        sol_endpoint = str(get_chain_config().rpc_http_selected).strip()
        if sol_endpoint:
            _sol_tracker = SolSlotTracker(
                endpoint=sol_endpoint,
                on_slot=_on_sol_slot,
                poll_s=float(os.getenv("SOL_SLOT_POLL_S", "2.0")),
            )
            await _sol_tracker.start()
            logging.info("SolSlotTracker started: chain=%s endpoint=%s", get_chain_config().chain, sol_endpoint)

    app.state.paused = _read_paused_flag()
    logging.info("Loaded paused flag from DB: %s", app.state.paused)
    app.state.bot_state_machine = build_state_machine()
    if app.state.paused and app.state.bot_state_machine.state != BotState.PAUSED:
        rec = app.state.bot_state_machine.transition(
            BotState.PAUSED, actor="system", reason="db_pause_flag", force=True
        )
        record_bot_state_transition(rec.from_state, rec.to_state, rec.reason)
    else:
        set_bot_state(app.state.bot_state_machine.state.value)
    app.state.paused = app.state.bot_state_machine.state == BotState.PAUSED
    set_runtime_state(app.state.bot_state_machine.state)
    # Runtime chain-id safety check (EVM only): expected config chain_id must match RPC eth_chainId.
    if family_now == "evm":
        with contextlib.suppress(Exception):
            expected_chain_id = int(get_chain_config().chain_id)
            app.state._chain_id_expected = expected_chain_id
            try:
                actual_chain_id = int(app.state.w3.eth.chain_id) if app.state.w3 else None
            except Exception:
                actual_chain_id = None
            app.state._chain_id_actual = actual_chain_id
            if actual_chain_id is None or int(actual_chain_id) != expected_chain_id:
                err = f"chain_id_mismatch expected={expected_chain_id} actual={actual_chain_id}"
                app.state._chain_id_validation_error = "chain_id_mismatch"
                rec = app.state.bot_state_machine.transition(
                    BotState.DEGRADED, actor="system", reason=err, force=True
                )
                record_bot_state_transition(rec.from_state, rec.to_state, rec.reason)
                set_runtime_state(BotState.DEGRADED)
                app.state.paused = True
                ctrl = getattr(app.state, "switch_controller", None)
                if ctrl is not None:
                    ctrl.last_transition_error = "chain_id_mismatch"
                logging.warning("startup chain-id validation failed: %s", err)

    logging.info("Bot state initialized: %s", app.state.bot_state_machine.state.value)

    global _runtime_monitor_task
    if _runtime_monitor_task is None or _runtime_monitor_task.done():
        _runtime_monitor_task = asyncio.create_task(_runtime_monitor_loop())
    if _rpc_metrics_client is None:
        cfg = get_chain_config()
        _rpc_metrics_client = AsyncInstrumentedRpcClient(
            urls=[cfg.rpc_http_selected] + list(cfg.rpc_http_backups),
            family=str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm",
            chain=str(cfg.chain).strip().lower() or "unknown",
        )
    if _rpc_ping_task is None or _rpc_ping_task.done():
        _rpc_ping_task = asyncio.create_task(_rpc_ping_loop())

@app.on_event("shutdown")
async def _shutdown():
    global _runtime_monitor_task, _rpc_ping_task
    if _runtime_monitor_task and not _runtime_monitor_task.done():
        _runtime_monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _runtime_monitor_task
        _runtime_monitor_task = None
    if _rpc_ping_task and not _rpc_ping_task.done():
        _rpc_ping_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _rpc_ping_task
        _rpc_ping_task = None
    if _monitor:
        await _monitor.stop()
    global _sol_tracker
    if _sol_tracker:
        await _sol_tracker.stop()
        _sol_tracker = None
    stream_obs_redis = getattr(app.state, "_stream_obs_redis", None)
    if stream_obs_redis is not None:
        with contextlib.suppress(Exception):
            await stream_obs_redis.close()
    if getattr(app.state, "alerts", None):
        await app.state.alerts.close()


async def _rpc_ping_loop() -> None:
    interval_s = max(10.0, float(os.getenv("RPC_PING_INTERVAL_S", "30")))
    timeout_s = max(1.0, float(os.getenv("RPC_PING_TIMEOUT_S", "5")))
    connector = aiohttp.TCPConnector(keepalive_timeout=30, ttl_dns_cache=60)
    timeout = aiohttp.ClientTimeout(total=timeout_s + 1.0)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
        while True:
            try:
                cfg = _get_chain_snapshot()
                family = str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm"
                chain = str(cfg.get("chain", "unknown")).strip().lower() or "unknown"
                urls = [str(cfg.get("rpc_http_selected", "")).strip()]
                for u in cfg.get("rpc_http_backups_selected", []) or []:
                    su = str(u).strip()
                    if su:
                        urls.append(su)
                urls = [u for u in urls if u]
                if _rpc_metrics_client is not None and family == "evm" and urls:
                    _rpc_metrics_client.set_context(family=family, chain=chain, urls=urls)
                    await _rpc_metrics_client.call(
                        sess,
                        method="eth_blockNumber",
                        params=[],
                        timeout_s=timeout_s,
                    )
            except Exception as e:
                log.debug("rpc ping loop error: %s", e)
            await asyncio.sleep(interval_s)


async def _runtime_monitor_loop() -> None:
    monitor_interval_s = max(5.0, float(os.getenv("INVAR_MONITOR_INTERVAL_S", "15")))
    snapshot_interval_s = max(1.0, float(os.getenv("HEALTH_SNAPSHOT_INTERVAL_S", "10")))
    interval_s = min(monitor_interval_s, snapshot_interval_s)
    while True:
        try:
            await _observe_chain_progress()
            await _observe_stream_progress()
            op_state = get_operator_state()
            desired_chain = canonicalize_chain_target(
                _read_ops_value("chain_selection", _effective_chain_key())
            )
            desired_mode_metric = "unknown"
            try:
                raw_desired = _read_ops_state_values()
                desired_paused = str(raw_desired.get("paused", "false")).strip().lower() == "true"
                desired_state_metric = "PAUSED" if desired_paused else "TRADING"
                desired_mode_metric = str(raw_desired.get("mode", "paper"))
                desired_sel = parse_chain_selection(desired_chain)
                set_desired_bot_state(
                    family=desired_sel.family,
                    chain_target=desired_sel.chain,
                    state=desired_state_metric,
                    mode=desired_mode_metric,
                )
            except Exception as e:
                log.debug("desired-state metric update failed chain=%s err=%s", desired_chain, e)
            ctrl = getattr(app.state, "switch_controller", None)
            if ctrl is not None:
                ctrl.desired_chain = desired_chain
                ctrl.effective_chain = _effective_chain_key()
                try:
                    await ctrl.reconcile(
                        desired_chain=desired_chain,
                        effective_chain=_effective_chain_key(),
                        apply_fn=_apply_chain_target_once,
                        validate_fn=_validate_chain_switch,
                    )
                except Exception as e:
                    log.warning("chain switch reconcile failed desired=%s err=%s", desired_chain, e)
            await _maybe_reload_dex_router(op_state)
            inv = get_runtime_invariants()
            target_state, reason = inv.evaluate(operator_state=op_state)
            target_state, reason = _apply_chain_ready_hold(
                op_state=op_state,
                suggested_state=target_state,
                suggested_reason=reason,
            )
            sm = getattr(app.state, "bot_state_machine", None)
            if sm and sm.state != target_state:
                rec = sm.transition(target_state, actor="system", reason=f"invariants:{reason}", force=True)
                record_bot_state_transition(rec.from_state, rec.to_state, rec.reason)
                set_runtime_state(target_state)
                app.state.paused = target_state == BotState.PAUSED
            else:
                set_bot_state(target_state.value)
                set_runtime_state(target_state)
                app.state.paused = target_state == BotState.PAUSED

            cfg = _get_chain_snapshot()
            set_runtime_bot_state(
                family=str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm",
                chain=str(cfg.get("chain", "unknown")),
                state=target_state.value,
                mode=desired_mode_metric,
            )
            if _health_snapshot_writer is not None:
                _health_snapshot_writer.maybe_write(
                    family=str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm",
                    chain=str(cfg.get("chain", "unknown")),
                    state=target_state.value,
                    mode=str(op_state.get("mode", "UNKNOWN")),
                )
        except Exception as e:
            log.warning("runtime monitor loop error: %s", e)
        await asyncio.sleep(interval_s)


def _apply_chain_ready_hold(
    *,
    op_state: dict,
    suggested_state: BotState,
    suggested_reason: str,
) -> tuple[BotState, str]:
    """
    After a successful chain switch we keep internal state at READY while operator is PAUSED.
    Trading still remains blocked by operator state until explicit !resume.
    """
    hold_target = str(getattr(app.state, "_chain_switch_ready_hold_target", "") or "").strip()
    op_state_value = str(op_state.get("state", "UNKNOWN")).strip().upper()
    if op_state_value == BotState.TRADING.value:
        app.state._chain_switch_ready_hold_target = ""
        return suggested_state, suggested_reason
    if not hold_target:
        return suggested_state, suggested_reason

    sm = getattr(app.state, "bot_state_machine", None)
    current_state = sm.state if sm else get_runtime_state(BotState.PAUSED)
    if (
        current_state == BotState.READY
        and suggested_state == BotState.PAUSED
        and str(suggested_reason) == "operator_not_trading"
    ):
        return BotState.READY, "chain_switch_ready_hold"
    return suggested_state, suggested_reason


def _provider_name(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "rpc").lower()
    except Exception:
        return "rpc"


def _evm_rpc_chain_id(w3: Web3) -> int:
    return int(w3.eth.chain_id)


def _chain_id_mismatch_error(*, expected: int, actual: int) -> RuntimeError:
    return RuntimeError(f"chain_id_mismatch expected={expected} actual={actual}")


async def _validate_evm_chain_id(*, expected: int, context: str = "runtime") -> int:
    w3 = getattr(app.state, "w3", None)
    if not w3:
        raise RuntimeError(f"{context}_no_rpc_client")
    actual = int(await asyncio.to_thread(_evm_rpc_chain_id, w3))
    app.state._chain_id_expected = int(expected)
    app.state._chain_id_actual = int(actual)
    if actual != int(expected):
        err = _chain_id_mismatch_error(expected=int(expected), actual=int(actual))
        app.state._chain_id_validation_error = "chain_id_mismatch"
        ctrl = getattr(app.state, "switch_controller", None)
        if ctrl is not None:
            ctrl.last_transition_error = "chain_id_mismatch"
        raise err
    app.state._chain_id_validation_error = ""
    return actual


def _sol_get_slot(endpoint: str) -> int:
    resp = requests.post(
        endpoint,
        json={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
        timeout=8,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"sol getSlot error: {body['error']}")
    return int(body.get("result") or 0)


async def _on_sol_slot(slot: int, endpoint: str) -> None:
    cfg = _get_chain_snapshot()
    family = str(os.getenv("CHAIN_FAMILY", "sol")).strip().lower() or "sol"
    chain = str(cfg.get("chain", "solana-devnet")).strip().lower() or "solana-devnet"
    provider = _provider_name(endpoint)
    max_seen = int(getattr(app.state, "_chain_obs_max_slot", slot) or slot)
    max_seen = max(max_seen, int(slot))
    app.state._chain_obs_max_slot = max_seen
    app.state._chain_obs_last_slot = int(slot)
    app.state._chain_obs_last_advance_ts = time.time()
    set_chain_slot(family=family, chain=chain, provider=provider, slot=int(slot))
    set_slot_lag(family=family, chain=chain, provider=provider, lag=max(0, max_seen - int(slot)))
    set_chain_head(family=family, chain=chain, provider=provider, height=0)
    set_head_lag(family=family, chain=chain, provider=provider, blocks=0)
    set_heartbeat(
        family=family,
        chain=chain,
        unix_ts=time.time(),
        provider=provider,
        strategy="default",
        dex="unknown",
    )


async def _observe_chain_progress() -> None:
    cfg = _get_chain_snapshot()
    family = str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm"
    chain = str(cfg.get("chain", "unknown")).strip().lower() or "unknown"
    provider = _provider_name(str(cfg.get("rpc_http_selected", "")))
    now = time.time()

    set_heartbeat(
        family=family,
        chain=chain,
        unix_ts=now,
        provider=provider,
        strategy="default",
        dex="unknown",
    )

    # Gate expensive RPC polling to a configurable cadence.
    obs_interval_s = max(2.0, float(os.getenv("CHAIN_OBSERVE_INTERVAL_S", "10")))
    next_due = float(getattr(app.state, "_chain_obs_next_due", 0.0) or 0.0)
    if now < next_due:
        return
    app.state._chain_obs_next_due = now + obs_interval_s

    if family == "evm":
        head = None
        provider_for_head = provider
        rpc_candidates: List[str] = []
        primary = str(cfg.get("rpc_http_selected", "")).strip()
        if primary:
            rpc_candidates.append(primary)
        with contextlib.suppress(Exception):
            c = get_chain_config()
            for ep in [c.rpc_http] + list(c.rpc_http_backups):
                ep = str(ep).strip()
                if ep and ep not in rpc_candidates:
                    rpc_candidates.append(ep)
        for ep in rpc_candidates:
            try:
                w3 = getattr(app.state, "w3", None) if ep == primary else Web3(
                    HTTPProvider(ep, request_kwargs={"timeout": 8})
                )
                if not w3:
                    continue
                head = int(await asyncio.to_thread(lambda w=w3: w.eth.block_number))
                provider_for_head = _provider_name(ep)
                if ep != primary:
                    app.state.w3 = w3
                break
            except Exception:
                continue
        if head is None:
            log.debug("chain head observe failed: no reachable RPC candidate")
            return
        try:
            with contextlib.suppress(Exception):
                expected_chain_id = int(get_chain_config().chain_id)
                actual_chain_id = int(await asyncio.to_thread(lambda w=app.state.w3: w.eth.chain_id))
                app.state._chain_id_expected = expected_chain_id
                app.state._chain_id_actual = actual_chain_id
                if actual_chain_id != expected_chain_id:
                    err = f"chain_id_mismatch expected={expected_chain_id} actual={actual_chain_id}"
                    app.state._chain_id_validation_error = "chain_id_mismatch"
                    ctrl = getattr(app.state, "switch_controller", None)
                    if ctrl is not None:
                        ctrl.last_transition_error = "chain_id_mismatch"
                else:
                    app.state._chain_id_validation_error = ""
            prev = getattr(app.state, "_chain_obs_last_head", None)
            max_seen = int(getattr(app.state, "_chain_obs_max_head", head) or head)
            max_seen = max(max_seen, head)
            app.state._chain_obs_max_head = max_seen
            app.state._chain_obs_last_head = head
            set_chain_head(family=family, chain=chain, provider=provider_for_head, height=head)
            set_head_lag(
                family=family,
                chain=chain,
                provider=provider_for_head,
                blocks=max(0, max_seen - head),
            )
            set_heartbeat(
                family=family,
                chain=chain,
                unix_ts=now,
                provider=provider_for_head,
                strategy="default",
                dex="unknown",
            )
            # Slot metric is not meaningful for EVM chains.
            set_chain_slot(family=family, chain=chain, provider=provider_for_head, slot=0)
            set_slot_lag(family=family, chain=chain, provider=provider_for_head, lag=0)
            if prev is None or head > int(prev):
                app.state._chain_obs_last_advance_ts = now
        except Exception as e:
            log.debug("chain head observe failed: %s", e)
        return

    if family == "sol":
        global _sol_tracker
        if _sol_tracker is not None and int(getattr(_sol_tracker, "current_slot", 0) or 0) > 0:
            return
        endpoint = str(cfg.get("rpc_http_selected", "")).strip()
        if not endpoint:
            return
        try:
            slot = int(await asyncio.to_thread(_sol_get_slot, endpoint))
            prev = getattr(app.state, "_chain_obs_last_slot", None)
            max_seen = int(getattr(app.state, "_chain_obs_max_slot", slot) or slot)
            max_seen = max(max_seen, slot)
            app.state._chain_obs_max_slot = max_seen
            app.state._chain_obs_last_slot = slot
            set_chain_slot(family=family, chain=chain, provider=provider, slot=slot)
            set_slot_lag(family=family, chain=chain, provider=provider, lag=max(0, max_seen - slot))
            # Head metric is not meaningful for slot-based chains.
            set_chain_head(family=family, chain=chain, provider=provider, height=0)
            set_head_lag(family=family, chain=chain, provider=provider, blocks=0)
            set_heartbeat(
                family=family,
                chain=chain,
                unix_ts=now,
                provider=provider,
                strategy="default",
                dex="unknown",
            )
            if prev is None or slot > int(prev):
                app.state._chain_obs_last_advance_ts = now
        except Exception as e:
            log.debug("chain slot observe failed: %s", e)

async def _observe_stream_progress() -> None:
    stream = str(os.getenv("REDIS_STREAM", "mempool:pending:txs")).strip() or "mempool:pending:txs"
    redis_url = str(os.getenv("REDIS_URL", "redis://redis:6379/0")).strip() or "redis://redis:6379/0"
    r = getattr(app.state, "_stream_obs_redis", None)
    if r is None:
        r = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=False)
        app.state._stream_obs_redis = r
    try:
        xlen = int(await r.xlen(stream))
        prev = getattr(app.state, "_stream_obs_prev_xlen", None)
        app.state._stream_obs_prev_xlen = xlen
        if prev is None:
            return
        delta = max(0, xlen - int(prev))
        if delta > 0:
            record_stream_events_observed(stream=stream, count=delta, source="api_probe")
    except Exception as e:
        log.debug("stream observe failed: %s", e)


async def _apply_chain_target_once(target: str) -> None:
    target = canonicalize_chain_target(target)
    if target == "UNKNOWN":
        raise RuntimeError("invalid desired chain target")
    old_chain = _get_chain_snapshot().get("chain", "unknown")
    try:
        ctrl = getattr(app.state, "switch_controller", None)
        if ctrl is not None:
            ctrl.switching_in_progress = True
            ctrl.last_transition_error = None
        app.state._chain_switch_ready_hold_target = ""
        _transition_state(BotState.PAUSED, actor="system", reason=f"chain_switch_pause:{target}", force=True)
        _transition_state(BotState.SYNCING, actor="system", reason=f"chain_switch_syncing:{target}", force=True)
        _emit_chain_switch_labels_now(
            state=BotState.SYNCING,
            mode=str(get_operator_state().get("mode", "UNKNOWN")),
        )
        await _reload_chain_runtime(target)
        await _validate_chain_switch(target)
        sel = parse_chain_selection(target)
        dex_validation = await asyncio.to_thread(
            _validate_dex_profiles_for_chain,
            family=sel.family.lower(),
            chain=sel.chain,
        )
        if not bool(dex_validation.get("ok", False)):
            raise RuntimeError(f"dex_profile_validation_failed:{'|'.join(dex_validation.get('errors', []))}")
        warm_result = await asyncio.to_thread(
            _warm_dex_pack_cache,
            family=sel.family.lower(),
            chain=sel.chain,
        )
        if not bool(warm_result.get("ok", True)):
            log.warning("dex_warmup_partial chain=%s errors=%s", target, warm_result.get("errors", []))
        _transition_state(BotState.READY, actor="system", reason=f"chain_switch_ready:{target}", force=True)
        _emit_chain_switch_labels_now(
            state=BotState.READY,
            mode=str(get_operator_state().get("mode", "UNKNOWN")),
        )
        app.state._chain_switch_ready_hold_target = target
        app.state._last_chain_target = target
        log.info(
            "chain_switch_apply old=%s new=%s result=ok dex_validated=%s dex_warm_ok=%s stream=%s",
            old_chain,
            target,
            int(dex_validation.get("validated", 0)),
            int(warm_result.get("ok_count", 0)),
            str(os.getenv("REDIS_STREAM", "")),
        )
    except Exception as e:
        _transition_state(BotState.DEGRADED, actor="system", reason=f"chain_switch_failed:{target}", force=True)
        app.state._chain_switch_ready_hold_target = ""
        app.state._last_chain_target = target
        ctrl = getattr(app.state, "switch_controller", None)
        if ctrl is not None:
            ctrl.last_transition_error = "chain_id_mismatch" if "chain_id_mismatch" in str(e) else str(e)
        log.warning("chain_switch_validation old=%s new=%s result=fail error=%s", old_chain, target, e)
        raise
    finally:
        ctrl = getattr(app.state, "switch_controller", None)
        if ctrl is not None:
            ctrl.switching_in_progress = False


async def _validate_chain_switch(target: str) -> None:
    sel = parse_chain_selection(target)
    if sel.family == "EVM":
        w3 = getattr(app.state, "w3", None)
        if not w3:
            raise RuntimeError("switch_validation_no_rpc_client")
        expected_chain_id = int(get_chain_config().chain_id)
        await _validate_evm_chain_id(expected=expected_chain_id, context="switch_validation")
        start = time.time()
        try:
            h0 = int(await asyncio.to_thread(lambda: w3.eth.block_number))
        except Exception as e:
            raise RuntimeError(f"switch_validation_head_read_failed:{e}") from e
        deadline = start + 15.0
        last = h0
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            try:
                h = int(await asyncio.to_thread(lambda: w3.eth.block_number))
            except Exception:
                continue
            last = h
            if h > h0:
                app.state._chain_obs_last_advance_ts = time.time()
                return
        raise RuntimeError(f"switch_validation_head_not_advancing start={h0} last={last} timeout_s=15")

    # SOL observe-only validation: slot must advance within 15s.
    global _sol_tracker
    endpoint = str(_get_chain_snapshot().get("rpc_http_selected", "")).strip()
    if _sol_tracker is not None and int(getattr(_sol_tracker, "current_slot", 0) or 0) > 0:
        s0 = int(getattr(_sol_tracker, "current_slot", 0) or 0)
        deadline = time.time() + 15.0
        last = s0
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            last = int(getattr(_sol_tracker, "current_slot", 0) or 0)
            if last > s0:
                return
        raise RuntimeError(f"switch_validation_slot_not_advancing start={s0} last={last} timeout_s=15")
    if not endpoint:
        raise RuntimeError("switch_validation_no_sol_endpoint")
    s0 = int(await asyncio.to_thread(_sol_get_slot, endpoint))
    deadline = time.time() + 15.0
    last = s0
    while time.time() < deadline:
        await asyncio.sleep(1.0)
        try:
            s = int(await asyncio.to_thread(_sol_get_slot, endpoint))
        except Exception:
            continue
        last = s
        if s > s0:
            return
    raise RuntimeError(f"switch_validation_slot_not_advancing start={s0} last={last} timeout_s=15")

# ---- Health ----
@app.get("/health")
def health():
    cfg = _get_chain_snapshot()
    family, chain, network = canonicalize_labels(family=os.getenv("CHAIN_FAMILY", "evm"), chain=cfg["chain"])
    ws_connected_endpoint = getattr(_monitor, "connected_endpoint", None) if _monitor else None
    state_machine = getattr(app.state, "bot_state_machine", None)
    state = state_machine.state.value if state_machine else ("PAUSED" if bool(getattr(app.state, "paused", False)) else "READY")
    return {
        "ok": True,
        "time": int(time.time()),
        "w3_connected": bool(getattr(app.state,"w3",None) and app.state.w3.is_connected()),
        "mempool_monitor": bool(_monitor),
        "chain_family": family,
        "chain": chain,
        "network": network,
        "chain_id": cfg["chain_id"],
        "rpc_http_selected": cfg["rpc_http_selected"],
        "ws_endpoints_selected": cfg["ws_endpoints_selected"],
        "ws_connected_endpoint": ws_connected_endpoint,
        # backward-compatible keys
        "endpoints": cfg["ws_endpoints_selected"],
        "rpc_http": cfg["rpc_http_selected"],
        "paused": bool(getattr(app.state, "paused", False)),
        "state": state,
    }


@app.get("/operator/state")
def operator_state():
    raw = _read_ops_state_values()
    paused = str(raw.get("paused", "false")).strip().lower() == "true"
    kill_switch = str(raw.get("kill_switch", "false")).strip().lower() == "true"
    mode = str(raw.get("mode", "paper")).strip().lower() or "paper"
    chain_selection = str(raw.get("chain_selection", "")).strip()
    if not chain_selection:
        chain_selection = f"EVM:{str(os.getenv('CHAIN', 'sepolia')).strip().lower() or 'sepolia'}"
    desired_chain = canonicalize_chain_target(chain_selection)
    return {
        "ok": True,
        "desired_state": "PAUSED" if paused else "TRADING",
        "desired_mode": mode,
        "desired_chain": desired_chain,
        "kill_switch": kill_switch,
        "raw": raw,
    }


@app.get("/chains")
def list_chains():
    registry = _load_chain_registry()
    supported = _supported_families()
    items = []
    for chain, cfg in sorted(registry.items()):
        family = str(cfg.get("family", "evm")).strip().lower() or "evm"
        if family not in {"evm", "sol"}:
            family = "evm"
        if family not in supported:
            continue
        key = f"{family.upper()}:{chain}"
        items.append(
            {
                "key": key,
                "family": family,
                "chain": chain,
                "chain_id": int(cfg.get("chain_id", 0) or 0),
            }
        )
    return {"ok": True, "supported_families": sorted(supported), "items": items}


@app.post("/operator/chain")
def operator_set_chain(name: str | None = None, payload: ChainPayload | None = None):
    op_id = _new_op_id()
    chain_name = str(name or "").strip()
    if payload is not None and str(payload.chain_key or "").strip():
        chain_name = str(payload.chain_key).strip()
    if not chain_name:
        raise HTTPException(status_code=400, detail="missing chain selection")
    try:
        sel = parse_chain_selection(chain_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid chain selection: {e}") from e

    family = sel.family.strip().lower()
    supported = _supported_families()
    if family not in supported:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported chain family '{family}'. supported_families={sorted(supported)}",
        )

    registry = _load_chain_registry()
    cfg = registry.get(sel.chain)
    if not cfg:
        raise HTTPException(status_code=400, detail=f"unsupported chain '{sel.chain}'")
    cfg_family = str(cfg.get("family", "evm")).strip().lower() or "evm"
    if cfg_family != family:
        raise HTTPException(
            status_code=400,
            detail=f"chain '{sel.chain}' family mismatch: requested={family} configured={cfg_family}",
        )

    chain_key = f"{sel.family}:{sel.chain}"
    try:
        with _db_connect() as conn:
            _ensure_ops_state_table(conn)
            conn.execute(
                """
                INSERT INTO ops_state(k, v, updated_at)
                VALUES ('chain_selection', %s, now())
                ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()
                """,
                (chain_key,),
            )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"failed to persist desired chain: {e}") from e

    out = _base_operator_result(op_id=op_id, ok=True, applied=True, message=f"desired_chain={chain_key}")
    out.update({"desired_chain": chain_key, "supported_families": sorted(supported)})
    return out


@app.get("/operator/events")
def operator_events(limit: int = 20):
    safe_limit = max(1, min(int(limit), 200))
    q = """
    SELECT
      op_id, ts, actor, action, value, reason, applied, error,
      desired_state, desired_mode, desired_chain, effective_state, effective_chain, created_at
    FROM operator_events
    ORDER BY created_at DESC
    LIMIT %s
    """
    try:
        with _db_connect() as conn:
            _ensure_operator_events_table(conn)
            rows = conn.execute(q, (safe_limit,)).fetchall()
            items = []
            for r in rows:
                items.append(
                    {
                        "op_id": str(r[0]),
                        "ts": r[1].isoformat() if r[1] else None,
                        "actor": r[2],
                        "action": r[3],
                        "value": r[4],
                        "reason": r[5],
                        "applied": bool(r[6]),
                        "error": r[7],
                        "desired_state": r[8],
                        "desired_mode": r[9],
                        "desired_chain": r[10],
                        "effective_state": r[11],
                        "effective_chain": r[12],
                        "created_at": r[13].isoformat() if r[13] else None,
                    }
                )
            return {"ok": True, "limit": safe_limit, "items": items}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"operator events query failed: {e}") from e


@app.get("/attempts")
def attempts(limit: int = 10):
    safe_limit = max(1, min(int(limit), 50))
    try:
        with _db_connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT
                      COALESCE(broadcasted_at, created_at) AS ts,
                      attempt_id, opportunity_id, strategy, status, reason_code,
                      expected_pnl_usd, gas_estimate, sim_outcome, sim_revert_reason,
                      tx_hash, chain
                    FROM attempts
                    ORDER BY COALESCE(broadcasted_at, created_at) DESC
                    LIMIT %s
                    """,
                    (safe_limit,),
                ).fetchall()
                items = []
                for r in rows:
                    items.append(
                        {
                            "ts": r[0].isoformat() if r[0] else None,
                            "attempt_id": r[1],
                            "opportunity_id": r[2],
                            "strategy": r[3],
                            "status": r[4],
                            "reason_code": r[5],
                            "expected_pnl_usd": float(r[6]) if r[6] is not None else None,
                            "gas_estimate": float(r[7]) if r[7] is not None else None,
                            "sim_outcome": r[8],
                            "sim_revert_reason": r[9],
                            "tx_hash": r[10],
                            "chain": r[11],
                        }
                    )
                return {"ok": True, "limit": safe_limit, "items": items}
            except Exception:
                rows = conn.execute(
                    """
                    SELECT created_at, id, tx_hash, decision, reject_reason, pnl_est, estimated_gas, sim_ok, chain
                    FROM candidates
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (safe_limit,),
                ).fetchall()
                items = []
                for r in rows:
                    items.append(
                        {
                            "ts": r[0].isoformat() if r[0] else None,
                            "attempt_id": str(r[1]),
                            "opportunity_id": None,
                            "strategy": "none_selected" if str(r[3] or "").upper() != "ACCEPT" else "default",
                            "status": str(r[3] or "UNKNOWN").upper(),
                            "reason_code": r[4] or "none",
                            "expected_pnl_usd": float(r[5]) if r[5] is not None else None,
                            "gas_estimate": float(r[6]) if r[6] is not None else None,
                            "sim_outcome": "OK" if bool(r[7]) else "FAIL",
                            "sim_revert_reason": None,
                            "tx_hash": None,
                            "chain": r[8] or str(os.getenv("CHAIN", "unknown")),
                        }
                    )
                return {"ok": True, "limit": safe_limit, "items": items}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"attempts query failed: {e}") from e


@app.get("/top")
def top(window: str = "24h"):
    iv = "24 hours"
    if str(window).strip().lower() in {"10m", "10min", "10mins"}:
        iv = "10 minutes"
    q = f"""
    SELECT COALESCE(reject_reason, 'none') AS reason, COUNT(*)::bigint AS c
    FROM candidates
    WHERE created_at >= now() - interval '{iv}'
    GROUP BY 1
    ORDER BY c DESC
    LIMIT 20
    """
    try:
        with _db_connect() as conn:
            rows = conn.execute(q).fetchall()
            return {
                "ok": True,
                "window": window,
                "items": [{"reason_code": str(r[0]), "count": int(r[1])} for r in rows],
            }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"top query failed: {e}") from e


@app.get("/pipeline")
def pipeline():
    window = "10 minutes"
    counts = {
        "mempool_msgs_10m": 0,
        "decode_ok_10m": 0,
        "decode_fail_10m": 0,
        "detector_hit_10m": 0,
        "detector_miss_10m": 0,
        "candidates_emitted_10m": 0,
    }
    decode_fail_reasons: list[dict] = []
    detector_miss_reasons: list[dict] = []
    last_errors = {
        "decode": None,
        "detector": None,
        "candidate": None,
    }
    try:
        with _db_connect() as conn:
            row = conn.execute(
                f"""
                WITH
                me AS (
                  SELECT COUNT(*)::bigint AS c
                  FROM mempool_events
                  WHERE created_at >= now() - interval '{window}'
                ),
                de AS (
                  SELECT COUNT(*)::bigint AS c
                  FROM mempool_errors
                  WHERE created_at >= now() - interval '{window}'
                    AND error_type LIKE 'decode_%'
                ),
                dh AS (
                  SELECT COUNT(*)::bigint AS c
                  FROM candidates
                  WHERE created_at >= now() - interval '{window}'
                    AND COALESCE(notes->>'detector_match','false') IN ('true','t','1','True')
                ),
                dm AS (
                  SELECT COUNT(*)::bigint AS c
                  FROM candidates
                  WHERE created_at >= now() - interval '{window}'
                    AND COALESCE(notes->>'detector_match','false') NOT IN ('true','t','1','True')
                ),
                ce AS (
                  SELECT COUNT(*)::bigint AS c
                  FROM candidates
                  WHERE created_at >= now() - interval '{window}'
                    AND UPPER(COALESCE(decision,'')) = 'ACCEPT'
                )
                SELECT
                  (SELECT c FROM me) AS mempool_msgs_10m,
                  GREATEST((SELECT c FROM me) - (SELECT c FROM de), 0)::bigint AS decode_ok_10m,
                  (SELECT c FROM de) AS decode_fail_10m,
                  (SELECT c FROM dh) AS detector_hit_10m,
                  (SELECT c FROM dm) AS detector_miss_10m,
                  (SELECT c FROM ce) AS candidates_emitted_10m
                """
            ).fetchone()
            if row:
                counts = {
                    "mempool_msgs_10m": int(row[0] or 0),
                    "decode_ok_10m": int(row[1] or 0),
                    "decode_fail_10m": int(row[2] or 0),
                    "detector_hit_10m": int(row[3] or 0),
                    "detector_miss_10m": int(row[4] or 0),
                    "candidates_emitted_10m": int(row[5] or 0),
                }

            dfr = conn.execute(
                f"""
                SELECT COALESCE(error_type, 'decode_unknown') AS reason, COUNT(*)::bigint AS c
                FROM mempool_errors
                WHERE created_at >= now() - interval '{window}'
                  AND error_type LIKE 'decode_%'
                GROUP BY 1
                ORDER BY c DESC
                LIMIT 10
                """
            ).fetchall()
            decode_fail_reasons = [{"reason": str(r[0]), "count": int(r[1])} for r in dfr]

            dmr = conn.execute(
                f"""
                SELECT COALESCE(reject_reason, 'detector_miss') AS reason, COUNT(*)::bigint AS c
                FROM candidates
                WHERE created_at >= now() - interval '{window}'
                  AND COALESCE(notes->>'detector_match','false') NOT IN ('true','t','1','True')
                GROUP BY 1
                ORDER BY c DESC
                LIMIT 10
                """
            ).fetchall()
            detector_miss_reasons = [{"reason": str(r[0]), "count": int(r[1])} for r in dmr]

            l_decode = conn.execute(
                """
                SELECT error_msg
                FROM mempool_errors
                WHERE error_type LIKE 'decode_%'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if l_decode and l_decode[0]:
                last_errors["decode"] = str(l_decode[0])[:300]

            l_detector = conn.execute(
                """
                SELECT reject_reason
                FROM candidates
                WHERE reject_reason IS NOT NULL AND reject_reason <> ''
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if l_detector and l_detector[0]:
                last_errors["detector"] = str(l_detector[0])[:300]

            l_candidate = conn.execute(
                """
                SELECT error_type, error_msg
                FROM mempool_errors
                WHERE error_type NOT LIKE 'decode_%'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if l_candidate:
                et = str(l_candidate[0] or "unknown")
                em = str(l_candidate[1] or "")
                last_errors["candidate"] = f"{et}: {em[:260]}".strip()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"pipeline query failed: {e}") from e

    return {
        "ok": True,
        "window": "10m",
        "stream": str(os.getenv("REDIS_STREAM", "mempool:pending:txs")),
        "candidates_stream_prefix": str(os.getenv("CANDIDATES_STREAM_PREFIX", "candidates:default")),
        "detectors_active": ["candidate_detector", "cross_dex_arb", "routing_improvement"],
        "token_universe_loaded": True,
        "dex_packs_loaded": True,
        "counts_10m": counts,
        "decode_fail_reasons": decode_fail_reasons,
        "detector_miss_reasons": detector_miss_reasons,
        "last_errors": last_errors,
    }


@app.get("/strategies")
def strategies():
    enabled = [x.strip() for x in str(os.getenv("ENABLED_STRATEGIES", "default")).split(",") if x.strip()]
    return {
        "ok": True,
        "items": [{"name": s, "seen_10m": 0, "scored_10m": 0, "selected_10m": 0, "sim_ok_10m": 0, "sim_fail_10m": 0} for s in enabled],
    }


@app.get("/readiness")
def readiness():
    checks = []
    try:
        h = health()
        checks.append({"name": "health", "ok": bool(h.get("ok"))})
        checks.append({"name": "rpc_connected", "ok": bool(h.get("w3_connected"))})
    except Exception as e:
        checks.append({"name": "health", "ok": False, "error": str(e)})
    try:
        with _db_connect() as conn:
            conn.execute("SELECT 1")
        checks.append({"name": "db", "ok": True})
    except Exception as e:
        checks.append({"name": "db", "ok": False, "error": str(e)})
    ok = all(bool(c.get("ok")) for c in checks)
    failed = [c for c in checks if not bool(c.get("ok"))]
    return {"ok": ok, "checks": checks, "failed": failed}


@app.get("/status")
def status():
    desired = operator_state()
    effective = _effective_state_payload()
    cfg = _get_chain_snapshot()
    ws_selected = cfg.get("ws_endpoints_selected", []) if isinstance(cfg, dict) else []
    ws_primary = ws_selected[0] if isinstance(ws_selected, list) and ws_selected else ""
    effective_chain_key = _effective_chain_key()
    ctrl = getattr(app.state, "switch_controller", None)
    ctrl_snap = ctrl.snapshot() if ctrl is not None else None
    last = _last_operator_event_fields()
    restart_required = str(desired.get("desired_chain", "")) != effective_chain_key
    family_now = str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm"
    head_value = effective.get("head")
    lag_blocks_value = max(0, int(effective.get("lag_blocks", 0) or 0))
    slot_value = effective.get("slot")
    lag_slots_value = max(0, int(effective.get("lag_slots", 0) or 0))
    if family_now == "sol":
        head_value = None
        lag_blocks_value = None
    else:
        slot_value = None
        lag_slots_value = None

    return {
        "ok": True,
        "desired_state": desired.get("desired_state"),
        "desired_mode": desired.get("desired_mode"),
        "desired_chain": desired.get("desired_chain"),
        "desired_family": str(desired.get("desired_chain", "UNKNOWN")).split(":", 1)[0].lower() if ":" in str(desired.get("desired_chain", "")) else None,
        "kill_switch": desired.get("kill_switch"),
        "effective_state": effective.get("effective_state"),
        "effective_chain": effective_chain_key,
        "effective_family": family_now,
        "resolved_chain_id": effective.get("resolved_chain_id"),
        "chain_id_expected": getattr(app.state, "_chain_id_expected", None),
        "chain_id_actual": getattr(app.state, "_chain_id_actual", None),
        "config_error": str(getattr(app.state, "_chain_id_validation_error", "") or ""),
        "rpc_url": _mask_url(str(effective.get("rpc_url", ""))),
        "ws_url": _mask_url(str(ws_primary)),
        "head": head_value,
        "slot": slot_value,
        "lag_blocks": lag_blocks_value,
        "lag_slots": lag_slots_value,
        "mempool_stream": str(os.getenv("REDIS_STREAM", "mempool:pending:txs")),
        "candidates_stream_prefix": str(os.getenv("CANDIDATES_STREAM_PREFIX", "candidates:default")),
        "switching_in_progress": bool(ctrl_snap.switching_in_progress) if ctrl_snap else False,
        "last_transition_error": ctrl_snap.last_transition_error if ctrl_snap else None,
        "restart_required": bool(restart_required),
        "last_op_id_applied": last.get("last_op_id_applied"),
        "last_op_apply_error": last.get("last_op_apply_error"),
        "last_op_error_id": last.get("last_op_error_id"),
    }

@app.get("/debug/mempool")
async def debug_mempool():
    import time as _time
    import redis.asyncio as aioredis

    stream = os.getenv("REDIS_STREAM", "mempool:pending:txs")
    group = os.getenv("REDIS_GROUP", "mempool")
    r = aioredis.from_url(os.getenv("REDIS_URL","redis://redis:6379/0"))

    xlen = await r.xlen(stream)
    groups = await r.xinfo_groups(stream)
    last = await r.xrevrange(stream, count=1)
    prod_info = await r.hgetall("mempool:producer")

    last_age_s = None
    if last:
        _id, fields = last[0]
        try:
            ts_ms = fields.get(b"ts_ms") if isinstance(fields, dict) else None
            if ts_ms is None and isinstance(fields, dict):
                ts_ms = fields.get("ts_ms")
            if ts_ms is not None:
                ts_ms = int(ts_ms)
                last_age_s = max(0.0, (_time.time()*1000.0 - ts_ms) / 1000.0)
        except Exception:
            last_age_s = None

    def _counter_value(c):
        try:
            return float(c._value.get())
        except Exception:
            return None

    out = {
        "stream": stream,
        "xlen": xlen,
        "groups": groups,
        "last_entry_age_s": last_age_s,
        "producer_endpoint": (prod_info.get(b"endpoint") or prod_info.get("endpoint") or "").decode() if isinstance(prod_info.get(b"endpoint"), (bytes, bytearray)) else (prod_info.get("endpoint") or prod_info.get(b"endpoint") or ""),
        "producer_last_ts_ms": (prod_info.get(b"ts_ms") or prod_info.get("ts_ms") or None),
        "rpc_gettx_ok_total": _counter_value(rpc_gettx_ok_total),
        "rpc_gettx_errors_total": _counter_value(rpc_gettx_errors_total),
    }

    await r.close()
    return out

@app.get("/debug/db_stats")
def debug_db_stats():
    import psycopg

    q = """
    SELECT
      (SELECT COUNT(*) FROM mempool_events WHERE created_at >= now() - interval '10 minutes') AS events_10m,
      (SELECT COUNT(*) FROM mempool_tx WHERE last_seen_ts_ms >= (extract(epoch from now() - interval '10 minutes') * 1000)::bigint) AS tx_10m,
      (SELECT COUNT(*) FROM mempool_errors WHERE created_at >= now() - interval '10 minutes') AS errors_10m,
      (SELECT COUNT(*) FROM mempool_events) AS events_total,
      (SELECT COUNT(*) FROM mempool_tx) AS tx_total,
      (SELECT COUNT(*) FROM mempool_errors) AS errors_total
    """
    try:
        with _db_connect() as conn:
            row = conn.execute(q).fetchone()
            return {
                "ok": True,
                "events_10m": int(row[0]),
                "tx_10m": int(row[1]),
                "errors_10m": int(row[2]),
                "events_total": int(row[3]),
                "tx_total": int(row[4]),
                "errors_total": int(row[5]),
            }
    except psycopg.errors.UndefinedTable as e:
        raise HTTPException(status_code=503, detail=f"missing mempool tables: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db_stats query failed: {e}") from e

@app.get("/candidates")
def list_candidates():
    q = """
    SELECT id, ts_ms, tx_hash, kind, score, notes, created_at
    FROM candidates
    ORDER BY created_at DESC
    LIMIT 50
    """
    try:
        with _db_connect() as conn:
            rows = conn.execute(q).fetchall()
            return {
                "ok": True,
                "items": [
                    {
                        "id": int(r[0]),
                        "ts_ms": int(r[1]),
                        "tx_hash": r[2],
                        "kind": r[3],
                        "score": float(r[4]),
                        "notes": r[5] if isinstance(r[5], dict) else {},
                        "created_at": r[6].isoformat() if r[6] else None,
                    }
                    for r in rows
                ],
            }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"candidates query failed: {e}") from e

@app.get("/debug/candidates")
def debug_candidates(limit: int = 50):
    safe_limit = max(1, min(int(limit), 200))
    q = """
    SELECT
      id, ts_ms, tx_hash, kind, score, notes, created_at,
      chain, seen_ts, to_addr, decoded_method, venue_tag,
      estimated_gas, estimated_edge_bps, sim_ok, pnl_est, decision, reject_reason
    FROM candidates
    ORDER BY created_at DESC
    LIMIT %s
    """
    try:
        with _db_connect() as conn:
            rows = conn.execute(q, (safe_limit,)).fetchall()
            return {
                "ok": True,
                "limit": safe_limit,
                "items": [
                    {
                        "id": int(r[0]),
                        "ts_ms": int(r[1]),
                        "tx_hash": r[2],
                        "kind": r[3],
                        "score": float(r[4]) if r[4] is not None else None,
                        "notes": r[5] if isinstance(r[5], dict) else {},
                        "created_at": r[6].isoformat() if r[6] else None,
                        "chain": r[7],
                        "seen_ts": int(r[8]) if r[8] is not None else None,
                        "to": r[9],
                        "decoded_method": r[10],
                        "venue_tag": r[11],
                        "estimated_gas": int(r[12]) if r[12] is not None else None,
                        "estimated_edge_bps": float(r[13]) if r[13] is not None else None,
                        "sim_ok": bool(r[14]) if r[14] is not None else None,
                        "pnl_est": float(r[15]) if r[15] is not None else None,
                        "decision": r[16],
                        "reject_reason": r[17],
                    }
                    for r in rows
                ],
            }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"debug candidates query failed: {e}") from e

@app.get("/debug/decisions")
def debug_decisions():
    q = """
    SELECT
      COALESCE(decision, 'UNKNOWN') AS decision,
      COALESCE(reject_reason, 'none') AS reject_reason,
      COUNT(*)::bigint AS c
    FROM candidates
    WHERE created_at >= now() - interval '24 hours'
    GROUP BY 1, 2
    ORDER BY c DESC, decision ASC, reject_reason ASC
    """
    try:
        with _db_connect() as conn:
            rows = conn.execute(q).fetchall()
            total = int(sum(int(r[2]) for r in rows))
            return {
                "ok": True,
                "window": "24h",
                "total": total,
                "items": [
                    {
                        "decision": str(r[0]),
                        "reject_reason": str(r[1]),
                        "count": int(r[2]),
                    }
                    for r in rows
                ],
            }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"debug decisions query failed: {e}") from e

@app.get("/paper_report")
def paper_report():
    q = """
    WITH c AS (
      SELECT id, created_at
      FROM candidates
      WHERE created_at >= now() - interval '24 hours'
    ),
    o AS (
      SELECT candidate_id, mined_block, success, gas_used, effective_gas_price, observed_after_sec, created_at
      FROM candidates_outcomes
      WHERE created_at >= now() - interval '24 hours'
    )
    SELECT
      (SELECT count(*) FROM c) AS candidates_24h,
      (SELECT count(*) FROM o) AS outcomes_24h,
      (SELECT count(*) FROM o WHERE mined_block IS NOT NULL) AS mined_24h,
      (SELECT count(*) FROM o WHERE success IS TRUE) AS success_24h,
      (SELECT avg(observed_after_sec) FROM o) AS avg_inclusion_delay_s,
      (SELECT avg(gas_used) FROM o WHERE gas_used IS NOT NULL) AS avg_gas_used,
      (SELECT avg(effective_gas_price) FROM o WHERE effective_gas_price IS NOT NULL) AS avg_effective_gas_price
    """
    try:
        with _db_connect() as conn:
            row = conn.execute(q).fetchone()
            candidates_24h = int(row[0] or 0)
            outcomes_24h = int(row[1] or 0)
            mined_24h = int(row[2] or 0)
            success_24h = int(row[3] or 0)
            success_rate = (float(success_24h) / float(mined_24h)) if mined_24h > 0 else 0.0
            return {
                "ok": True,
                "window": "24h",
                "candidates_24h": candidates_24h,
                "outcomes_24h": outcomes_24h,
                "mined_24h": mined_24h,
                "success_24h": success_24h,
                "success_rate": success_rate,
                "avg_inclusion_delay_s": float(row[4]) if row[4] is not None else None,
                "avg_gas_used": float(row[5]) if row[5] is not None else None,
                "avg_effective_gas_price": float(row[6]) if row[6] is not None else None,
            }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"paper_report query failed: {e}") from e


@app.post("/pause")
def pause_trading():
    try:
        return _transition_state(BotState.PAUSED, actor="manual", reason="api_pause")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"failed to persist paused=true: {e}") from e


@app.post("/resume")
def resume_trading():
    try:
        return _transition_state(BotState.TRADING, actor="manual", reason="api_resume")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"failed to persist paused=false: {e}") from e


@app.post("/operator/pause")
def operator_pause():
    op_id = _new_op_id()
    try:
        _transition_state(BotState.PAUSED, actor="operator", reason=f"op:{op_id}:pause", force=True)
        return _base_operator_result(op_id=op_id, ok=True, applied=True, message="paused")
    except Exception as e:
        return _base_operator_result(op_id=op_id, ok=False, applied=False, message=f"pause_failed:{e}")


@app.post("/operator/resume")
def operator_resume():
    op_id = _new_op_id()
    try:
        _transition_state(BotState.TRADING, actor="operator", reason=f"op:{op_id}:resume", force=True)
        return _base_operator_result(op_id=op_id, ok=True, applied=True, message="resumed")
    except Exception as e:
        return _base_operator_result(op_id=op_id, ok=False, applied=False, message=f"resume_failed:{e}")


@app.post("/operator/killswitch")
def operator_killswitch(payload: KillSwitchPayload):
    op_id = _new_op_id()
    try:
        with _db_connect() as conn:
            _ensure_ops_state_table(conn)
            conn.execute(
                """
                INSERT INTO ops_state(k, v, updated_at)
                VALUES ('kill_switch', %s, now())
                ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()
                """,
                ("true" if payload.enabled else "false",),
            )
        return _base_operator_result(op_id=op_id, ok=True, applied=True, message=f"kill_switch={'on' if payload.enabled else 'off'}")
    except Exception as e:
        return _base_operator_result(op_id=op_id, ok=False, applied=False, message=f"killswitch_failed:{e}")


@app.post("/operator/mode")
def operator_mode(payload: ModePayload):
    op_id = _new_op_id()
    mode = str(payload.mode).strip().lower()
    if mode not in {"paper", "dryrun", "live"}:
        raise HTTPException(status_code=400, detail="mode must be paper|dryrun|live")
    try:
        with _db_connect() as conn:
            _ensure_ops_state_table(conn)
            conn.execute(
                """
                INSERT INTO ops_state(k, v, updated_at)
                VALUES ('mode', %s, now())
                ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()
                """,
                (mode,),
            )
        return _base_operator_result(op_id=op_id, ok=True, applied=True, message=f"mode={mode}")
    except Exception as e:
        return _base_operator_result(op_id=op_id, ok=False, applied=False, message=f"mode_failed:{e}")


@app.post("/operator/live/prepare")
def operator_live_prepare():
    token = secrets.token_hex(4).upper()
    app.state._live_confirm_token = token
    app.state._live_confirm_exp = time.time() + 60.0
    return {"ok": True, "token": token, "expires_in_s": 60}


@app.post("/operator/live/commit")
def operator_live_commit(payload: LiveCommitPayload):
    op_id = _new_op_id()
    token = str(payload.token).strip().upper()
    cur = str(getattr(app.state, "_live_confirm_token", "")).strip().upper()
    exp = float(getattr(app.state, "_live_confirm_exp", 0.0) or 0.0)
    if not cur or token != cur or time.time() > exp:
        return _base_operator_result(op_id=op_id, ok=False, applied=False, message="live_commit_invalid_or_expired_token")
    app.state._live_confirm_token = ""
    app.state._live_confirm_exp = 0.0
    try:
        with _db_connect() as conn:
            _ensure_ops_state_table(conn)
            conn.execute(
                """
                INSERT INTO ops_state(k, v, updated_at)
                VALUES ('mode', 'live', now())
                ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()
                """
            )
        return _base_operator_result(op_id=op_id, ok=True, applied=True, message="mode=live")
    except Exception as e:
        return _base_operator_result(op_id=op_id, ok=False, applied=False, message=f"live_commit_failed:{e}")


@app.post("/state/{target}")
def set_state(target: str, actor: str = "manual", reason: str = "api_state_set", force: bool = False):
    try:
        parsed = parse_bot_state(target)
        return _transition_state(parsed, actor=actor, reason=reason, force=force)
    except ValueError as e:
        msg = str(e)
        code = 409 if "invalid state transition" in msg or "blocked by BOT_STATE_LOCKDOWN" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"failed to set state={target}: {e}") from e


@app.post("/chain/select")
async def select_chain(name: str):
    try:
        data = await _reload_chain_runtime(name)
        return {"ok": True, **data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"failed to select chain '{name}': {e}") from e

# ---- Debug endpoints ----
@app.post("/debug/bump")
def bump():
    mempool_unique_tx_total.labels(**canonical_metric_labels()).inc()
    return {"ok": True}

@app.post("/debug/private-submit/ok")
def debug_private_submit_ok(endpoint: str = "demo"):
    chain = os.getenv("CHAIN","any")
    private_submit_attempts.labels(relay=endpoint, chain=chain, reason="selected").inc()
    private_submit_success.labels(relay=endpoint, chain=chain).inc()
    return {"ok": True, "endpoint": endpoint}

@app.post("/debug/private-submit/fail")
def debug_private_submit_fail(endpoint: str = "demo", kind: str = "http_500"):
    chain = os.getenv("CHAIN","any")
    private_submit_attempts.labels(relay=endpoint, chain=chain, reason="selected").inc()
    private_submit_errors.labels(relay=endpoint, chain=chain, code=kind).inc()
    return {"ok": False, "endpoint": endpoint, "kind": kind}

# ---- Stubbed stealth smoke ----
@app.post("/_smoke/stealth")
async def smoke_stealth():
    import bot.exec.orderflow as of
    class _R:
        def __init__(self, ok=True, relay="mev_blocker"):
            self.ok = ok; self.tx_hash = "0xSMOKE"; self.relay = relay
            self.error=None; self.gas_used=120_000; self.gas_price_gwei=25.0
    class OK:
        def __init__(self, name, chain): self.name=name; self.chain=chain
        async def submit_raw(self, tx_hex, metadata): return _R(True, self.name)
        def is_retryable(self, e): return False
        def classify_reason(self, e): return "none"
    class Flaky:
        def __init__(self, name, chain): self.name=name; self.chain=chain; self.calls=0
        async def submit_raw(self, tx_hex, metadata):
            self.calls += 1
            return _R(ok=self.calls>1, relay=self.name)
        def is_retryable(self, e): return True
        def classify_reason(self, e): return "temporary"

    of.FlashbotsClient  = lambda chain, url: Flaky("flashbots_protect", chain)
    of.MevBlockerClient = lambda chain, url: OK("mev_blocker", chain)
    of.CowClient        = lambda chain, url: OK("cow_protocol", chain)

    strat = StealthStrategy()
    params = {
        "chain": os.getenv("CHAIN","sepolia"),
        "token_in":"USDC","token_out":"TOKENX",
        "amount_in":1_000_000,"desired_output":100_000,"max_input":1_200_000,
        "router":"0xRouterV3","sender":"0xSender","recipient":"0xRecipient",
        "pool_fee":3000,"size_usd":8000.0,"eth_usd":2500.0,"detected_snipers":1
    }
    results=[]
    for _ in range(3):
        r = await strat.execute_stealth_swap(params)
        results.append({"success": r.success, "relay": r.notes.get("relay"), "gas_ratio": r.notes.get("gas_cost_ratio")})
    return {"ok": all(x["success"] for x in results), "results": results}


from fastapi.responses import JSONResponse

@app.post("/_smoke/tick")
def _smoke_tick():
    try:
        # bump the key series so Grafana/Prom show non-zero
        from bot.core.telemetry import (
            stealth_decisions_total,
            orchestrator_decisions_total,
            relay_attempts_total,
            relay_success_total,
        )
        chain = os.getenv("CHAIN", "polygon")
        stealth_decisions_total.labels(decision="go").inc()
        orchestrator_decisions_total.labels(mode="stealth", reason="ok").inc()
        relay_attempts_total.labels(relay="mev_blocker", chain=chain).inc()
        relay_success_total.labels(relay="mev_blocker", chain=chain).inc()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/_smoke/stealth2")
async def _smoke_stealth2():
    try:
        # light up stealth + private orderflow metrics without importing full strategy/relays
        from bot.core.telemetry import (
            stealth_trigger_flags_total,
            stealth_flags_count,
            private_submit_attempts,
            private_submit_success,
            stealth_decisions_total,
        )
        chain = os.getenv("CHAIN", "polygon")

        # pretend 5 flags fired
        for f in ("high_slippage","new_token","low_liquidity","trending","active_snipers"):
            stealth_trigger_flags_total.labels(flag=f).inc()
        stealth_flags_count.set(5)

        # pretend we routed to mev_blocker and it worked
        private_submit_attempts.labels(relay="mev_blocker", chain=chain, reason="selected").inc()
        private_submit_success.labels(relay="mev_blocker", chain=chain).inc()

        # decide GO once
        stealth_decisions_total.labels(decision="go").inc()
        return {"ok": True, "stubbed": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
