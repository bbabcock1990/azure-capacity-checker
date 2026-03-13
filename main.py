"""
Azure Capacity Checker — FastAPI application entry-point.

Runs in two modes:
  - Local:          uvicorn main:app --reload  (or python run.py)
  - Azure Function: via function_app.py ASGI wrapper

Environment variables (see .env.example / Application Settings):
    AZURE_SUBSCRIPTION_ID        — optional (auto-discovered if not set)
    AZURE_PROBE_RESOURCE_GROUP   — optional, defaults to 'az-cap-probe-rg'
    AZURE_CLIENT_ID              — optional (service-principal auth)
    AZURE_CLIENT_SECRET          — optional (service-principal auth)
    AZURE_TENANT_ID              — optional (service-principal auth)
"""
import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from capacity_checker import AzureCapacityChecker, FullCheckResult, DISCLAIMER

# Only load .env file for local development; Azure Functions injects
# Application Settings as environment variables automatically.
if not os.environ.get("FUNCTIONS_WORKER_RUNTIME"):
    load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
SUBSCRIPTION_ID: str = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
PROBE_RESOURCE_GROUP: str = os.environ.get(
    "AZURE_PROBE_RESOURCE_GROUP", "az-cap-probe-rg"
)

# Max concurrent ODCR probes when handling batch requests
BATCH_CONCURRENCY: int = int(os.environ.get("BATCH_CONCURRENCY", "3"))

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Azure Capacity Checker",
    description="""
## Overview
Real-time Azure VM capacity checking using the **On-Demand Capacity
Reservations (ODCR)** API.

## How it works
1. The API validates **prerequisites**: SKU availability, restriction status,
   Capacity Reservation support, and vCPU quota headroom — all read-only calls.
2. It then creates an ephemeral **Capacity Reservation Group** in the target
   region.
3. It attempts to create a **Capacity Reservation** for the requested quantity of
   the VM size — this is the actual capacity probe.
4. If Azure accepts the reservation → **capacity is available**.
   If Azure rejects with a capacity error → **capacity is unavailable**.
5. Both resources are immediately deleted regardless of the outcome.
6. A **confidence score** (0–100) and signal level (High/Medium/Low/None) are
   computed from all signals.

## ⚠️ Disclaimer
Capacity checks are **point-in-time signals only**. Azure capacity is dynamic
and can change at any moment. Results do NOT guarantee that capacity will be
available when you deploy. Use this API for directional guidance only.

## Latency
Each probe involves creating and deleting Azure resources, so expect
**30 – 90 seconds** per request.  Use the `/batch` endpoint to check
multiple combinations concurrently.

## Authentication
The service uses `DefaultAzureCredential`.  Set `AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET`, and `AZURE_TENANT_ID` for service-principal auth,
or rely on Azure CLI / Managed Identity in production.
""",
    version="1.0.0",
    contact={"name": "Azure Capacity Checker"},
    license_info={"name": "MIT"},
)


# ── Pydantic models ────────────────────────────────────────────────────────────


class CapacityResponse(BaseModel):
    vm_size: str = Field(..., examples=["Standard_D4s_v3"])
    region: str = Field(..., examples=["eastus"])
    zone: Optional[str] = Field(None, examples=["1"])
    available: bool = Field(..., description="True if capacity is available")
    message: str
    error_code: Optional[str] = Field(
        None, description="Azure error code when capacity is unavailable"
    )


class BatchCheckItem(BaseModel):
    vm_size: str = Field(..., examples=["Standard_D4s_v3"])
    region: str = Field(..., examples=["eastus"])
    zone: Optional[str] = Field(None, examples=["1"])
    quantity: int = Field(1, ge=1, le=100, description="Number of VM instances to probe")


class BatchCapacityRequest(BaseModel):
    checks: list[BatchCheckItem] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="List of VM size / region / zone / quantity combinations to probe",
    )


class BatchCapacityResponse(BaseModel):
    results: list[CapacityResponse]


class QuotaInfo(BaseModel):
    family: str = Field(..., description="VM family name")
    region: str = Field(..., examples=["eastus"])
    current_usage: int = Field(..., description="Current vCPU usage")
    limit: int = Field(..., description="vCPU quota limit")
    vcpus_needed: int = Field(..., description="Estimated vCPUs needed for request")
    sufficient: bool = Field(..., description="True if quota headroom is sufficient")
    message: str


class SkuInfo(BaseModel):
    vm_size: str = Field(..., examples=["Standard_D4s_v3"])
    region: str = Field(..., examples=["eastus"])
    available: bool = Field(..., description="True if the SKU is available (no restrictions)")
    capacity_reservation_supported: bool = Field(
        ..., description="True if the SKU supports On-Demand Capacity Reservations"
    )
    restrictions: list[str] = Field(
        default_factory=list, description="List of restriction reasons, if any"
    )
    message: str


class FullCapacityResponse(BaseModel):
    vm_size: str = Field(..., examples=["Standard_D4s_v3"])
    region: str = Field(..., examples=["eastus"])
    zone: Optional[str] = Field(None, examples=["1"])
    quantity: int = Field(1, description="Number of VM instances tested")
    # Prerequisite checks
    sku_check: SkuInfo
    quota_check: QuotaInfo
    # ODCR probe result
    capacity_available: bool = Field(
        ..., description="True if the ODCR probe confirmed capacity"
    )
    capacity_message: str
    capacity_error_code: Optional[str] = None
    # Aggregate signal
    confidence_score: int = Field(
        ..., ge=0, le=100,
        description="Confidence score (0–100) combining all signals"
    )
    signal_level: str = Field(
        ..., description="Signal level: High (90-100), Medium (60-89), Low (20-59), None (0-19)"
    )
    summary: str = Field(..., description="Human-readable summary of all results")
    disclaimer: str = Field(
        default=DISCLAIMER,
        description="Point-in-time disclaimer — capacity is NOT guaranteed"
    )


class BatchFullCheckItem(BaseModel):
    vm_size: str = Field(..., examples=["Standard_D4s_v3"])
    region: str = Field(..., examples=["eastus"])
    zone: Optional[str] = Field(None, examples=["1"])
    quantity: int = Field(1, ge=1, le=100, description="Number of VM instances to test")


class BatchFullCheckRequest(BaseModel):
    checks: list[BatchFullCheckItem] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="List of VM size / region / zone / quantity combinations to check",
    )


class BatchFullCheckResponse(BaseModel):
    results: list[FullCapacityResponse]


# ── Shared checker factory ─────────────────────────────────────────────────────

_discovered_subscription_id: Optional[str] = None


def _discover_subscription_id() -> Optional[str]:
    """
    Attempt to discover the default Azure subscription from the current
    credential context (Azure CLI, service principal, or managed identity).
    Result is cached after the first successful lookup.
    """
    global _discovered_subscription_id
    if _discovered_subscription_id:
        return _discovered_subscription_id

    try:
        import shutil
        import subprocess

        az_path = shutil.which("az") or shutil.which("az.cmd")
        if not az_path:
            logger.info("Azure CLI not found on PATH; skipping subscription auto-discovery")
            return None

        result = subprocess.run(
            [az_path, "account", "show", "--query", "id", "-o", "tsv"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            _discovered_subscription_id = result.stdout.strip()
            logger.info(
                "Auto-discovered subscription from Azure CLI: %s",
                _discovered_subscription_id,
            )
            return _discovered_subscription_id
    except Exception as exc:
        logger.warning("Could not auto-discover subscription: %s", exc)

    return None


def _get_checker(
    subscription_id: Optional[str] = None,
) -> AzureCapacityChecker:
    """Return a checker instance, using per-request subscription, env default, or auto-discovery."""
    sub_id = subscription_id or SUBSCRIPTION_ID or _discover_subscription_id()
    if not sub_id:
        raise HTTPException(
            status_code=500,
            detail=(
                "No Azure subscription found. Provide one of: "
                "(1) subscription_id query parameter, "
                "(2) AZURE_SUBSCRIPTION_ID environment variable, or "
                "(3) log in with 'az login' so the API can auto-discover your subscription."
            ),
        )
    return AzureCapacityChecker(sub_id, PROBE_RESOURCE_GROUP)


# ── Endpoints ──────────────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Redirect root to the interactive API docs."""
    return RedirectResponse(url="/docs")


@app.get(
    "/health",
    summary="Health check",
    tags=["System"],
)
async def health() -> JSONResponse:
    """Returns service health and basic configuration (no secrets)."""
    effective_sub = SUBSCRIPTION_ID or _discover_subscription_id()
    configured = bool(effective_sub)
    is_azure_function = bool(os.environ.get("FUNCTIONS_WORKER_RUNTIME"))
    return JSONResponse(
        status_code=200 if configured else 503,
        content={
            "status": "healthy" if configured else "misconfigured",
            "runtime": "azure-function" if is_azure_function else "local",
            "subscription_configured": configured,
            "subscription_source": (
                "environment" if SUBSCRIPTION_ID
                else "auto-discovered" if effective_sub
                else "none"
            ),
            "probe_resource_group": PROBE_RESOURCE_GROUP,
        },
    )


@app.get(
    "/api/v1/check-capacity",
    response_model=CapacityResponse,
    summary="Check VM capacity (single)",
    tags=["Capacity"],
)
async def check_capacity(
    vm_size: str = Query(
        ...,
        description="Azure VM SKU name",
        examples=["Standard_D4s_v3", "Standard_NC6s_v3", "Standard_F8s_v2"],
    ),
    region: str = Query(
        ...,
        description="Azure region slug (lowercase, no spaces)",
        examples=["eastus", "westeurope", "southeastasia"],
    ),
    zone: Optional[str] = Query(
        None,
        description=(
            "Availability zone number (1, 2, or 3).  "
            "Omit for a regional (non-zonal) capacity check."
        ),
        pattern=r"^[1-3]$",
    ),
    subscription_id: Optional[str] = Query(
        None,
        description="Azure subscription ID. Overrides AZURE_SUBSCRIPTION_ID env var.",
    ),
    report: bool = Query(
        False,
        description="When true, returns a concise plain-text report instead of JSON",
    ),
) -> CapacityResponse:
    """
    Probe whether a specific VM size has available capacity in an Azure region.

    **Note:** This call typically takes 30 – 90 seconds because it creates and
    deletes real Azure Capacity Reservation resources.
    """
    checker = _get_checker(subscription_id)
    region = region.strip().lower()

    try:
        result = await asyncio.to_thread(checker.check_capacity, vm_size, region, zone)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Unexpected error during capacity check: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    resp = CapacityResponse(
        vm_size=result.vm_size,
        region=result.region,
        zone=result.zone,
        available=result.available,
        message=result.message,
        error_code=result.error_code,
    )
    if report:
        return PlainTextResponse(_build_capacity_report([resp]))
    return resp


@app.post(
    "/api/v1/check-capacity/batch",
    response_model=BatchCapacityResponse,
    summary="Check VM capacity (batch)",
    tags=["Capacity"],
)
async def check_capacity_batch(
    body: BatchCapacityRequest,
    subscription_id: Optional[str] = Query(
        None,
        description="Azure subscription ID. Overrides AZURE_SUBSCRIPTION_ID env var.",
    ),
    report: bool = Query(
        False,
        description="When true, returns a concise plain-text report instead of JSON",
    ),
) -> BatchCapacityResponse:
    """
    Probe capacity for multiple VM size / region / zone / quantity combinations in parallel.

    Up to `BATCH_CONCURRENCY` (default 3) probes run concurrently to avoid
    throttling the Azure API.  Results are returned in the same order as the
    request items.

    Maximum 20 items per request.
    """
    checker = _get_checker(subscription_id)  # validate subscription config upfront
    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def probe(item: BatchCheckItem) -> CapacityResponse:
        async with semaphore:
            # Each probe gets its own checker instance for thread-safe
            # parallel execution — SDK clients are not shared across threads.
            probe_checker = AzureCapacityChecker(
                checker.subscription_id, checker.probe_resource_group
            )
            region = item.region.strip().lower()
            try:
                result = await asyncio.to_thread(
                    probe_checker.check_capacity, item.vm_size, region, item.zone, item.quantity
                )
                return CapacityResponse(
                    vm_size=result.vm_size,
                    region=result.region,
                    zone=result.zone,
                    available=result.available,
                    message=result.message,
                    error_code=result.error_code,
                )
            except Exception as exc:
                logger.error("Batch probe error for %s/%s: %s", item.vm_size, region, exc)
                return CapacityResponse(
                    vm_size=item.vm_size,
                    region=region,
                    zone=item.zone,
                    available=False,
                    message=f"Probe failed: {exc}",
                    error_code="ProbeError",
                )

    results = await asyncio.gather(*[probe(item) for item in body.checks])

    # Safety-net sweep: clean up any orphaned probe resources
    try:
        sweep_checker = _get_checker(subscription_id)
        await asyncio.to_thread(sweep_checker.sweep_orphaned_probes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Post-batch sweep failed: %s", exc)

    result_list = list(results)
    if report:
        return PlainTextResponse(_build_capacity_report(result_list))
    return BatchCapacityResponse(results=result_list)


@app.get(
    "/api/v1/check",
    response_model=FullCapacityResponse,
    summary="Full capacity check with prerequisites",
    tags=["Capacity"],
)
async def full_check(
    vm_size: str = Query(
        ...,
        description="Azure VM SKU name",
        examples=["Standard_D4s_v3", "Standard_NC6s_v3", "Standard_F8s_v2"],
    ),
    region: str = Query(
        ...,
        description="Azure region slug (lowercase, no spaces)",
        examples=["eastus", "westeurope", "southeastasia"],
    ),
    zone: Optional[str] = Query(
        None,
        description=(
            "Availability zone number (1, 2, or 3).  "
            "Omit for a regional (non-zonal) capacity check."
        ),
        pattern=r"^[1-3]$",
    ),
    subscription_id: Optional[str] = Query(
        None,
        description="Azure subscription ID. Overrides AZURE_SUBSCRIPTION_ID env var.",
    ),
    quantity: int = Query(
        1, ge=1, le=100,
        description="Number of VM instances to test capacity for",
    ),
    report: bool = Query(
        False,
        description="When true, returns a concise plain-text report instead of JSON",
    ),
) -> FullCapacityResponse:
    """
    Comprehensive capacity check combining prerequisite validation and ODCR probe.

    Checks performed (in order):
    1. **SKU availability** — confirms the VM size exists in the region, has no
       restrictions, and supports On-Demand Capacity Reservations.
    2. **Quota check** — validates vCPU quota headroom for the VM family.
    3. **ODCR probe** — creates/deletes an ephemeral Capacity Reservation to
       test real-time capacity availability for the requested quantity.

    Returns a **confidence score** (0–100) and **signal level** (High/Medium/Low/None)
    aggregating all signals.

    ⚠️ **Disclaimer:** This is a point-in-time signal. Capacity is dynamic and
    results do NOT guarantee availability at deployment time.
    """
    checker = _get_checker(subscription_id)
    region = region.strip().lower()

    try:
        result = await asyncio.to_thread(checker.full_check, vm_size, region, zone, quantity)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Full check failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    resp = _build_full_response(result, quantity)
    if report:
        return PlainTextResponse(_build_full_report([resp]))
    return resp


def _build_full_response(result, quantity: int = 1) -> FullCapacityResponse:
    """Map a FullCheckResult to the API response model."""
    return FullCapacityResponse(
        vm_size=result.vm_size,
        region=result.region,
        zone=result.zone,
        quantity=quantity,
        sku_check=SkuInfo(
            vm_size=result.sku_check.vm_size,
            region=result.sku_check.region,
            available=result.sku_check.available,
            capacity_reservation_supported=result.sku_check.capacity_reservation_supported,
            restrictions=result.sku_check.restrictions,
            message=result.sku_check.message,
        ),
        quota_check=QuotaInfo(
            family=result.quota_check.family,
            region=result.quota_check.region,
            current_usage=result.quota_check.current_usage,
            limit=result.quota_check.limit,
            vcpus_needed=result.quota_check.vcpus_needed,
            sufficient=result.quota_check.sufficient,
            message=result.quota_check.message,
        ),
        capacity_available=result.capacity_check.available if result.capacity_check else False,
        capacity_message=result.capacity_check.message if result.capacity_check else "Not checked",
        capacity_error_code=result.capacity_check.error_code if result.capacity_check else None,
        confidence_score=result.confidence_score,
        signal_level=result.signal_level,
        summary=result.summary,
        disclaimer=result.disclaimer,
    )


# ── Report formatting ─────────────────────────────────────────────────────────


def _format_single_report(r: CapacityResponse) -> str:
    """Format a single ODCR-only probe result as a concise report line."""
    icon = "PASS" if r.available else "FAIL"
    zone_str = f" zone {r.zone}" if r.zone else ""
    return f"  [{icon}] {r.vm_size} in {r.region}{zone_str}"


def _format_full_report(r: FullCapacityResponse) -> str:
    """Format a full check result as a concise multi-line report block."""
    icon = "PASS" if r.capacity_available else "FAIL"
    zone_str = f" zone {r.zone}" if r.zone else ""
    qty_str = f" x{r.quantity}" if r.quantity > 1 else ""
    lines = [
        f"  [{icon}] {r.vm_size}{qty_str} in {r.region}{zone_str}",
        f"         Signal: {r.signal_level} ({r.confidence_score}/100)",
        f"         SKU: {'OK' if r.sku_check.available else 'RESTRICTED'}"
        f" | ODCR: {'Supported' if r.sku_check.capacity_reservation_supported else 'Not supported'}",
        f"         Quota: {r.quota_check.family} — "
        + (f"{r.quota_check.current_usage}/{r.quota_check.limit} used"
           if r.quota_check.current_usage >= 0
           else "unknown"),
    ]
    if not r.quota_check.sufficient:
        lines.append(f"         ** Quota insufficient: need {r.quota_check.vcpus_needed} vCPUs")
    if r.capacity_error_code and r.capacity_error_code not in ("None",):
        lines.append(f"         Error: {r.capacity_error_code}")
    return "\n".join(lines)


def _build_capacity_report(results: list[CapacityResponse]) -> str:
    header = "Azure Capacity Check Report"
    sep = "=" * len(header)
    body = "\n".join(_format_single_report(r) for r in results)
    passed = sum(1 for r in results if r.available)
    footer = f"\n  {passed}/{len(results)} passed"
    disclaimer = f"\n  * {DISCLAIMER}"
    return f"{header}\n{sep}\n{body}\n{footer}\n{disclaimer}\n"


def _build_full_report(results: list[FullCapacityResponse]) -> str:
    header = "Azure Capacity Check Report"
    sep = "=" * len(header)
    body = "\n\n".join(_format_full_report(r) for r in results)
    passed = sum(1 for r in results if r.capacity_available)
    footer = f"\n  {passed}/{len(results)} passed"
    disclaimer = f"\n  * {DISCLAIMER}"
    return f"{header}\n{sep}\n\n{body}\n{footer}\n{disclaimer}\n"


@app.post(
    "/api/v1/check/batch",
    response_model=BatchFullCheckResponse,
    summary="Full capacity check — batch (multiple SKUs)",
    tags=["Capacity"],
)
async def full_check_batch(
    body: BatchFullCheckRequest,
    subscription_id: Optional[str] = Query(
        None,
        description="Azure subscription ID. Overrides AZURE_SUBSCRIPTION_ID env var.",
    ),
    report: bool = Query(
        False,
        description="When true, returns a concise plain-text report instead of JSON",
    ),
) -> BatchFullCheckResponse:
    """
    Run the full capacity check (SKU + quota + ODCR probe) for multiple
    VM size / region / zone / quantity combinations in parallel.

    Up to `BATCH_CONCURRENCY` probes run concurrently.  Max 20 items per request.

    ⚠️ **Disclaimer:** These are point-in-time signals. Capacity is dynamic and
    results do NOT guarantee availability at deployment time.
    """
    _get_checker(subscription_id)  # validate subscription config upfront
    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def probe(item: BatchFullCheckItem) -> FullCapacityResponse:
        async with semaphore:
            # Each probe gets its own checker instance for thread-safe
            # parallel execution — SDK clients are not shared across threads.
            probe_checker = _get_checker(subscription_id)
            region = item.region.strip().lower()
            try:
                result = await asyncio.to_thread(
                    probe_checker.full_check, item.vm_size, region, item.zone, item.quantity
                )
                return _build_full_response(result, item.quantity)
            except Exception as exc:
                logger.error("Batch full-check error for %s/%s: %s", item.vm_size, region, exc)
                # Return a degraded response rather than failing the whole batch
                return FullCapacityResponse(
                    vm_size=item.vm_size,
                    region=region,
                    zone=item.zone,
                    quantity=item.quantity,
                    sku_check=SkuInfo(
                        vm_size=item.vm_size, region=region,
                        available=False, capacity_reservation_supported=False, message=str(exc),
                    ),
                    quota_check=QuotaInfo(
                        family="unknown", region=region,
                        current_usage=-1, limit=-1, vcpus_needed=0,
                        sufficient=False, message=str(exc),
                    ),
                    capacity_available=False,
                    capacity_message=f"Probe failed: {exc}",
                    capacity_error_code="ProbeError",
                    confidence_score=0,
                    signal_level="None",
                    summary=f"Check failed for {item.vm_size} in {region}: {exc}",
                )

    results = await asyncio.gather(*[probe(item) for item in body.checks])

    # Safety-net sweep: clean up any orphaned probe resources
    try:
        sweep_checker = _get_checker(subscription_id)
        await asyncio.to_thread(sweep_checker.sweep_orphaned_probes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Post-batch sweep failed: %s", exc)

    result_list = list(results)
    if report:
        return PlainTextResponse(_build_full_report(result_list))
    return BatchFullCheckResponse(results=result_list)


@app.get(
    "/api/v1/check-quota",
    response_model=QuotaInfo,
    summary="Check vCPU quota only",
    tags=["Prerequisites"],
)
async def check_quota(
    vm_size: str = Query(..., description="Azure VM SKU name", examples=["Standard_D4s_v3"]),
    region: str = Query(..., description="Azure region slug", examples=["eastus"]),
    subscription_id: Optional[str] = Query(
        None,
        description="Azure subscription ID. Overrides AZURE_SUBSCRIPTION_ID env var.",
    ),
) -> QuotaInfo:
    """Check vCPU quota headroom for a VM family in a region (read-only, no cost)."""
    checker = _get_checker(subscription_id)
    region = region.strip().lower()

    try:
        result = await asyncio.to_thread(checker.check_quota, vm_size, region)
    except Exception as exc:
        logger.error("Quota check failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return QuotaInfo(
        family=result.family,
        region=result.region,
        current_usage=result.current_usage,
        limit=result.limit,
        vcpus_needed=result.vcpus_needed,
        sufficient=result.sufficient,
        message=result.message,
    )


@app.get(
    "/api/v1/check-sku",
    response_model=SkuInfo,
    summary="Check SKU availability only",
    tags=["Prerequisites"],
)
async def check_sku(
    vm_size: str = Query(..., description="Azure VM SKU name", examples=["Standard_D4s_v3"]),
    region: str = Query(..., description="Azure region slug", examples=["eastus"]),
    subscription_id: Optional[str] = Query(
        None,
        description="Azure subscription ID. Overrides AZURE_SUBSCRIPTION_ID env var.",
    ),
) -> SkuInfo:
    """
    Check if a VM SKU is available in a region, has no restrictions, and supports
    Capacity Reservations (read-only, no cost).
    """
    checker = _get_checker(subscription_id)
    region = region.strip().lower()

    try:
        result = await asyncio.to_thread(checker.check_sku_availability, vm_size, region)
    except Exception as exc:
        logger.error("SKU check failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return SkuInfo(
        vm_size=result.vm_size,
        region=result.region,
        available=result.available,
        capacity_reservation_supported=result.capacity_reservation_supported,
        restrictions=result.restrictions,
        message=result.message,
    )


@app.post(
    "/api/v1/cleanup",
    summary="Clean up orphaned probe resources",
    tags=["System"],
)
async def cleanup_probes(
    subscription_id: Optional[str] = Query(
        None,
        description="Azure subscription ID. Overrides AZURE_SUBSCRIPTION_ID env var.",
    ),
) -> JSONResponse:
    """
    Scan the probe resource group and delete any orphaned cap-probe-*
    Capacity Reservation Groups left behind by failed or interrupted probes.

    This runs automatically after every batch request, but can also be
    called manually.
    """
    checker = _get_checker(subscription_id)
    try:
        cleaned = await asyncio.to_thread(checker.sweep_orphaned_probes)
    except Exception as exc:
        logger.error("Cleanup failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(content={
        "cleaned": cleaned,
        "message": (
            f"Removed {cleaned} orphaned probe resource(s)"
            if cleaned > 0
            else "No orphaned probe resources found"
        ),
    })
