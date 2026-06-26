"""In-app refresh of provisioned-throughput region availability.

The deployed app keeps its region-availability data fresh **by itself** — it
queries the live Azure Models API (ARM REST) using its own managed identity /
AAD credential, on a background thread, once every 24 hours. No storage account,
no scheduled job, and no GitHub dependency are required: a fresh ``azd up`` in any
subscription gets daily-refreshed data as long as the app's identity can read the
Cognitive Services model catalog (e.g. the **Reader** role at subscription scope).

The result is a payload identical in shape to ``region_data.json`` — a dict with
``generated_utc`` / ``source`` / ``deployment_type_skus`` / ``models`` — so it can
be handed straight to :func:`ptu_core.set_live_region_data`.

Everything here is best-effort and never raises: on any failure (no credentials,
missing RBAC, throttling) the app simply keeps showing the bundled snapshot and
retries on the next cycle. The developer-time ``scripts/refresh_regions.py`` (which
uses the ``az`` CLI) remains the way to regenerate the committed snapshot.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# Azure provisioned SKU name -> the deployment-type label used throughout the app.
# Plain "Provisioned" is the legacy SKU and is intentionally ignored. Kept in sync
# with scripts/refresh_regions.py.
_SKU_TO_DEPLOYMENT_TYPE = {
    "GlobalProvisionedManaged": "Global",
    "DataZoneProvisionedManaged": "Data Zone",
    "ProvisionedManaged": "Regional",
}

# Azure model catalog names that differ from this tool's MODEL_PRESETS keys.
_MODEL_NAME_ALIASES = {
    "Llama-3.3-70B-Instruct": "Llama-3.3-70B",
}

_ARM = "https://management.azure.com"
# Recent stable Models API version; overridable for forward-compat.
_MODELS_API_VERSION = os.environ.get("AZURE_MODELS_API_VERSION", "2026-05-01")
_SUBSCRIPTIONS_API_VERSION = "2022-12-01"
_LOCATIONS_API_VERSION = "2022-12-01"

_DEFAULT_CACHE_PATH = os.environ.get(
    "REGION_DATA_CACHE_PATH",
    os.path.join(tempfile.gettempdir(), "ptu_region_data_cache.json"),
)

_state_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None


def _preset_name(model_name: str) -> str:
    return _MODEL_NAME_ALIASES.get(model_name, model_name)


def _arm_get(url: str, token: str) -> dict:
    """GET an ARM REST URL with a bearer token and return parsed JSON."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted ARM URL)
        return json.loads(resp.read().decode("utf-8"))


def _arm_get_all(url: str, token: str) -> list:
    """GET an ARM list endpoint, following ``nextLink`` pagination."""
    items: list = []
    next_url: str | None = url
    while next_url:
        body = _arm_get(next_url, token)
        items.extend(body.get("value") or [])
        next_url = body.get("nextLink")
    return items


def _resolve_subscription_id(token: str) -> str | None:
    """Pick the subscription to query: env override, else the first accessible one."""
    explicit = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if explicit:
        return explicit
    subs = _arm_get_all(
        f"{_ARM}/subscriptions?api-version={_SUBSCRIPTIONS_API_VERSION}", token
    )
    for sub in subs:
        if sub.get("state") in (None, "Enabled") and sub.get("subscriptionId"):
            return sub["subscriptionId"]
    return None


def _list_physical_regions(subscription_id: str, token: str) -> list[str]:
    """Return all physical Azure regions for the subscription."""
    locations = _arm_get_all(
        f"{_ARM}/subscriptions/{subscription_id}/locations"
        f"?api-version={_LOCATIONS_API_VERSION}",
        token,
    )
    names = [
        loc["name"]
        for loc in locations
        if (loc.get("metadata") or {}).get("regionType") == "Physical"
    ]
    return sorted(set(names))


def _fetch_region(subscription_id: str, region: str, token: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (region, [(preset_name, deployment_type), ...]) for one region.

    Empty list when the region has no provisioned model SKUs, or when the call
    fails — a single bad region must never abort the whole refresh.
    """
    url = (
        f"{_ARM}/subscriptions/{subscription_id}/providers/Microsoft.CognitiveServices"
        f"/locations/{region}/models?api-version={_MODELS_API_VERSION}"
    )
    try:
        models = _arm_get_all(url, token)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        return region, []

    found: set[tuple[str, str]] = set()
    for entry in models:
        if entry.get("kind") not in ("OpenAI", "AIServices"):
            continue
        model = entry.get("model") or {}
        name = model.get("name")
        if not name:
            continue
        preset = _preset_name(name)
        for sku in model.get("skus") or []:
            dep = _SKU_TO_DEPLOYMENT_TYPE.get(sku.get("name"))
            if dep:
                found.add((preset, dep))
    return region, sorted(found)


def fetch_region_data(workers: int = 10) -> dict | None:
    """Query the live Azure Models API and build a ``region_data.json``-shaped payload.

    Returns the payload dict on success, or ``None`` on any failure (missing
    credentials, no subscription access, RBAC, network). Never raises.
    """
    try:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        token = credential.get_token(f"{_ARM}/.default").token

        subscription_id = _resolve_subscription_id(token)
        if not subscription_id:
            return None

        regions = _list_physical_regions(subscription_id, token)
        if not regions:
            return None

        models: dict[str, dict[str, set]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_region, subscription_id, r, token): r for r in regions
            }
            for fut in as_completed(futures):
                _region, pairs = fut.result()
                for preset, dep in pairs:
                    models.setdefault(preset, {}).setdefault(dep, set()).add(_region)

        if not models:
            return None

        serialised = {
            preset: {dep: sorted(regs) for dep, regs in sorted(types.items())}
            for preset, types in sorted(models.items())
        }
        return {
            "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "source": "Azure Models API (in-app refresh)",
            "deployment_type_skus": _SKU_TO_DEPLOYMENT_TYPE,
            "models": serialised,
        }
    except Exception:
        return None


def _age_hours(payload: dict | None) -> float | None:
    """Hours since ``payload['generated_utc']``, or ``None`` if unparseable."""
    if not payload:
        return None
    try:
        ts = _dt.datetime.fromisoformat(
            str(payload.get("generated_utc")).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 3600.0


def load_cached(cache_path: str | None = None) -> dict | None:
    """Load a previously-persisted refresh payload, or ``None`` if absent/malformed."""
    path = cache_path or _DEFAULT_CACHE_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("models"), dict):
        return data
    return None


def _save_cache(payload: dict, cache_path: str | None = None) -> None:
    """Persist a refresh payload to disk so quick restarts skip a re-query."""
    path = cache_path or _DEFAULT_CACHE_PATH
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass


def _refresh_loop(on_update, max_age_hours: float, retry_minutes: float, cache_path: str | None) -> None:
    while True:
        cached = load_cached(cache_path)
        age = _age_hours(cached)
        if cached is not None and age is not None and age < max_age_hours:
            on_update(cached)
            time.sleep(max(60.0, (max_age_hours - age) * 3600.0))
            continue

        payload = fetch_region_data()
        if payload:
            on_update(payload)
            _save_cache(payload, cache_path)
            time.sleep(max_age_hours * 3600.0)
        else:
            time.sleep(retry_minutes * 60.0)


def start_background_refresh(
    on_update,
    max_age_hours: float = 24.0,
    retry_minutes: float = 60.0,
    cache_path: str | None = None,
) -> None:
    """Start a daemon thread that keeps region data fresh via the Azure Models API.

    ``on_update`` is called with each new (or still-fresh cached) payload — pass
    :func:`ptu_core.set_live_region_data`. Idempotent: only one thread runs per
    process. On success it refreshes every ``max_age_hours``; on failure it retries
    every ``retry_minutes`` (so a just-granted RBAC role is picked up promptly).
    """
    global _refresh_thread
    with _state_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        thread = threading.Thread(
            target=_refresh_loop,
            args=(on_update, max_age_hours, retry_minutes, cache_path),
            name="region-refresh",
            daemon=True,
        )
        _refresh_thread = thread
        thread.start()
