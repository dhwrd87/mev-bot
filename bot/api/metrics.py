import os
import logging
from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest, multiprocess, REGISTRY
from starlette.responses import Response
from bot.core.telemetry import ensure_prometheus_multiproc_ready

log = logging.getLogger("metrics")

def _registry():
    if ensure_prometheus_multiproc_ready():
        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)
        return reg
    return REGISTRY

async def metrics_endpoint():
    try:
        return Response(generate_latest(_registry()), media_type=CONTENT_TYPE_LATEST)
    except Exception as e:
        # Keep endpoint alive even if multiprocess shard files are corrupted.
        log.warning("metrics multiprocess collector failed, falling back to process registry: %s", e)
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
