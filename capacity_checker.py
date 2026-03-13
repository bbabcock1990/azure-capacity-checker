"""
Azure Capacity Checker — ODCR probe logic.

Strategy: attempt to create an On-Demand Capacity Reservation (ODCR)
for the target VM size / region / zone. If Azure accepts the reservation
the capacity exists; if it rejects with a capacity-related error it does
not.  Either way the ephemeral resources are deleted immediately.
"""
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import ResourceManagementClient

logger = logging.getLogger(__name__)

# Azure error codes that indicate capacity is unavailable (not a config error)
CAPACITY_ERROR_CODES: set[str] = {
    "CapacityNotAvailable",
    "AllocationFailed",
    "OverconstrainedAllocationRequest",
    "OverconstrainedZonalAllocationRequest",
    "ZonalAllocationFailed",
    "SkuNotAvailable",
}

# Fallback: substrings in the error message that suggest capacity problems
CAPACITY_ERROR_SUBSTRINGS: list[str] = [
    "capacity",
    "allocation failed",
    "insufficient",
]

DISCLAIMER: str = (
    "Capacity checks are point-in-time signals only. Azure capacity is dynamic "
    "and can change at any moment. Results do NOT guarantee that capacity will "
    "be available when you deploy. Use this API for directional guidance only."
)


@dataclass
class CapacityCheckResult:
    vm_size: str
    region: str
    zone: Optional[str]
    available: bool
    message: str
    error_code: Optional[str] = field(default=None)


@dataclass
class SkuCheckResult:
    vm_size: str
    region: str
    available: bool
    capacity_reservation_supported: bool
    restrictions: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class QuotaCheckResult:
    family: str
    region: str
    current_usage: int
    limit: int
    vcpus_needed: int
    sufficient: bool
    message: str = ""


@dataclass
class FullCheckResult:
    vm_size: str
    region: str
    zone: Optional[str]
    sku_check: SkuCheckResult
    quota_check: QuotaCheckResult
    capacity_check: Optional[CapacityCheckResult]
    confidence_score: int
    signal_level: str
    summary: str
    disclaimer: str = DISCLAIMER


class AzureCapacityChecker:
    """
    Checks real-time Azure VM capacity via the ODCR (On-Demand Capacity
    Reservations) API.

    A single instance can be reused across requests — it lazily creates
    SDK clients and caches the credential.
    """

    def __init__(self, subscription_id: str, probe_resource_group: str):
        if not subscription_id:
            raise ValueError("subscription_id must not be empty")
        self.subscription_id = subscription_id
        self.probe_resource_group = probe_resource_group
        self._credential: Optional[DefaultAzureCredential] = None
        self._compute_client: Optional[ComputeManagementClient] = None
        self._resource_client: Optional[ResourceManagementClient] = None

    # ------------------------------------------------------------------
    # SDK client helpers
    # ------------------------------------------------------------------

    @property
    def credential(self) -> DefaultAzureCredential:
        if self._credential is None:
            self._credential = DefaultAzureCredential()
        return self._credential

    @property
    def compute_client(self) -> ComputeManagementClient:
        if self._compute_client is None:
            self._compute_client = ComputeManagementClient(
                self.credential, self.subscription_id
            )
        return self._compute_client

    @property
    def resource_client(self) -> ResourceManagementClient:
        if self._resource_client is None:
            self._resource_client = ResourceManagementClient(
                self.credential, self.subscription_id
            )
        return self._resource_client

    # ------------------------------------------------------------------
    # Resource-group bootstrap
    # ------------------------------------------------------------------

    def ensure_resource_group(self, region: str) -> str:
        """Create the probe resource group if it does not already exist."""
        logger.info("Ensuring resource group '%s' exists", self.probe_resource_group)
        try:
            self.resource_client.resource_groups.create_or_update(
                self.probe_resource_group,
                {"location": region},
            )
        except HttpResponseError as exc:
            # The RG already exists in a different region — that's fine.
            # A resource group can host ODCR resources in any region
            # regardless of its own location metadata.
            if exc.error and exc.error.code == "InvalidResourceGroupLocation":
                logger.info(
                    "Resource group '%s' already exists in a different location; reusing it.",
                    self.probe_resource_group,
                )
            else:
                raise
        return self.probe_resource_group

    # ------------------------------------------------------------------
    # Capacity probe
    # ------------------------------------------------------------------

    def check_capacity(
        self,
        vm_size: str,
        region: str,
        zone: Optional[str] = None,
    ) -> CapacityCheckResult:
        """
        Probe capacity by attempting to create an ODCR Capacity Reservation.

        Flow:
          1. Create a CapacityReservationGroup (CRG) in the target region.
          2. Attempt to create a CapacityReservation (CR) for 1 VM of the
             requested size.  This is the actual capacity probe — Azure will
             reject it if no capacity is available.
          3. Record the result.
          4. Always clean up both the CR and CRG in a ``finally`` block.

        Returns a :class:`CapacityCheckResult` regardless of outcome.
        Raises on unexpected errors (wrong VM size name, auth failure, etc.).
        """
        probe_id = uuid.uuid4().hex[:10]
        crg_name = f"cap-probe-crg-{probe_id}"
        cr_name = f"cap-probe-cr-{probe_id}"
        rg_name = self.probe_resource_group

        crg_created = False
        cr_created = False

        try:
            self.ensure_resource_group(region)

            # ── 1. Create Capacity Reservation Group ──────────────────────
            crg_body: dict = {"location": region}
            if zone:
                crg_body["zones"] = [zone]

            logger.info("Creating CRG '%s' in region '%s'", crg_name, region)
            self.compute_client.capacity_reservation_groups.create_or_update(
                rg_name, crg_name, crg_body
            )
            crg_created = True

            # ── 2. Try to create Capacity Reservation (the capacity probe) ─
            cr_body: dict = {
                "location": region,
                "sku": {"name": vm_size, "capacity": 1},
            }
            if zone:
                cr_body["zones"] = [zone]

            logger.info(
                "Probing capacity: vm_size='%s' region='%s' zone='%s'",
                vm_size,
                region,
                zone,
            )
            try:
                poller = self.compute_client.capacity_reservations.begin_create_or_update(
                    rg_name, crg_name, cr_name, cr_body
                )
                poller.result()  # Block until the long-running operation resolves
                cr_created = True

                logger.info("AVAILABLE — %s in %s (zone=%s)", vm_size, region, zone)
                return CapacityCheckResult(
                    vm_size=vm_size,
                    region=region,
                    zone=zone,
                    available=True,
                    message=(
                        f"Capacity is available for {vm_size} in {region}"
                        + (f" (zone {zone})" if zone else "")
                    ),
                )

            except HttpResponseError as exc:
                error_code = self._extract_error_code(exc)
                logger.info(
                    "Capacity reservation failed — code='%s' status=%s",
                    error_code,
                    exc.status_code,
                )

                if self._is_capacity_error(exc, error_code):
                    return CapacityCheckResult(
                        vm_size=vm_size,
                        region=region,
                        zone=zone,
                        available=False,
                        message=(
                            f"Capacity is NOT available for {vm_size} in {region}"
                            + (f" (zone {zone})" if zone else "")
                        ),
                        error_code=error_code,
                    )

                # Re-raise unexpected errors (bad VM SKU name, auth issue, …)
                raise

        finally:
            self._cleanup(rg_name, crg_name, cr_name, cr_created, crg_created)

    def check_sku(self, vm_size: str, region: str) -> SkuCheckResult:
        """Check whether the VM SKU is available and supports capacity reservations."""
        try:
            skus = self.compute_client.resource_skus.list(filter=f"location eq '{region}'")
            for sku in skus:
                if sku.name == vm_size and sku.resource_type == "virtualMachines":
                    restrictions = []
                    cr_supported = False
                    available = True

                    if sku.restrictions:
                        for r in sku.restrictions:
                            if r.type and r.type.lower() == "location":
                                available = False
                                restrictions.append(f"Location restricted: {r.reason_code}")
                            elif r.type and r.type.lower() == "zone":
                                restrictions.append(f"Zone restricted: {r.reason_code}")

                    for cap in (sku.capabilities or []):
                        if cap.name == "CapacityReservationSupported" and cap.value == "True":
                            cr_supported = True
                            break

                    msg = "SKU available" if available else f"SKU restricted: {', '.join(restrictions)}"
                    return SkuCheckResult(
                        vm_size=vm_size, region=region,
                        available=available, capacity_reservation_supported=cr_supported,
                        restrictions=restrictions, message=msg,
                    )

            return SkuCheckResult(
                vm_size=vm_size, region=region,
                available=False, capacity_reservation_supported=False,
                message=f"SKU '{vm_size}' not found in region '{region}'",
            )
        except Exception as exc:
            logger.warning("SKU check failed: %s", exc)
            return SkuCheckResult(
                vm_size=vm_size, region=region,
                available=False, capacity_reservation_supported=False,
                message=f"SKU check error: {exc}",
            )

    def check_quota(self, vm_size: str, region: str, quantity: int = 1) -> QuotaCheckResult:
        """Check vCPU quota for the VM family in the given region."""
        family = "Unknown"
        vcpus_per_vm = 0

        try:
            skus = self.compute_client.resource_skus.list(filter=f"location eq '{region}'")
            for sku in skus:
                if sku.name == vm_size and sku.resource_type == "virtualMachines":
                    for cap in (sku.capabilities or []):
                        if cap.name == "vCPUsAvailable":
                            vcpus_per_vm = int(cap.value)
                        elif cap.name == "vCPUs" and vcpus_per_vm == 0:
                            vcpus_per_vm = int(cap.value)
                    family = sku.family or "Unknown"
                    break
        except Exception as exc:
            logger.warning("Could not look up SKU info for quota check: %s", exc)

        vcpus_needed = vcpus_per_vm * quantity

        try:
            usages = self.compute_client.usage.list(region)
            for usage in usages:
                if usage.name and usage.name.value and family.lower() in usage.name.value.lower():
                    current = usage.current_value
                    limit = usage.limit
                    sufficient = (limit - current) >= vcpus_needed
                    return QuotaCheckResult(
                        family=family, region=region,
                        current_usage=current, limit=limit,
                        vcpus_needed=vcpus_needed, sufficient=sufficient,
                        message=f"Quota: {current}/{limit} vCPUs used, need {vcpus_needed}",
                    )
        except Exception as exc:
            logger.warning("Quota check failed: %s", exc)

        return QuotaCheckResult(
            family=family, region=region,
            current_usage=-1, limit=-1,
            vcpus_needed=vcpus_needed, sufficient=True,
            message="Could not verify quota — assuming sufficient",
        )

    def full_check(
        self,
        vm_size: str,
        region: str,
        zone: Optional[str] = None,
        quantity: int = 1,
    ) -> FullCheckResult:
        """Run SKU check, quota check, and ODCR capacity probe, then score."""
        sku_result = self.check_sku(vm_size, region)
        quota_result = self.check_quota(vm_size, region, quantity)

        capacity_result: Optional[CapacityCheckResult] = None
        if sku_result.available:
            try:
                capacity_result = self.check_capacity(vm_size, region, zone)
            except Exception as exc:
                logger.error("ODCR probe failed: %s", exc)
                capacity_result = CapacityCheckResult(
                    vm_size=vm_size, region=region, zone=zone,
                    available=False, message=f"Probe error: {exc}",
                )

        score = self._compute_confidence(sku_result, quota_result, capacity_result)
        level = self._score_to_level(score)
        summary = self._build_summary(
            vm_size, region, zone, sku_result, quota_result, capacity_result, score, level
        )

        return FullCheckResult(
            vm_size=vm_size, region=region, zone=zone,
            sku_check=sku_result, quota_check=quota_result,
            capacity_check=capacity_result,
            confidence_score=score, signal_level=level, summary=summary,
        )

    @staticmethod
    def _compute_confidence(
        sku: SkuCheckResult,
        quota: QuotaCheckResult,
        capacity: Optional[CapacityCheckResult],
    ) -> int:
        if not sku.available:
            return 0
        score = 20  # base for SKU available
        if sku.capacity_reservation_supported:
            score += 5
        if quota.sufficient:
            score += 15
        if capacity is not None:
            score += 60 if capacity.available else 0
        return min(score, 100)

    @staticmethod
    def _score_to_level(score: int) -> str:
        if score >= 90:
            return "High"
        if score >= 60:
            return "Medium"
        if score >= 20:
            return "Low"
        return "None"

    @staticmethod
    def _build_summary(
        vm_size: str, region: str, zone: Optional[str],
        sku: SkuCheckResult, quota: QuotaCheckResult,
        capacity: Optional[CapacityCheckResult],
        score: int, level: str,
    ) -> str:
        zone_str = f" zone {zone}" if zone else ""
        parts = [f"{vm_size} in {region}{zone_str}:"]
        parts.append(f"SKU {'available' if sku.available else 'restricted'}")
        if not sku.available:
            parts.append("— skipping further checks")
            return " ".join(parts)
        parts.append(f"| Quota {'OK' if quota.sufficient else 'INSUFFICIENT'}")
        if capacity is not None:
            parts.append(f"| ODCR {'PASS' if capacity.available else 'FAIL'}")
        else:
            parts.append("| ODCR not checked")
        parts.append(f"→ {level} confidence ({score}/100)")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_error_code(exc: HttpResponseError) -> Optional[str]:
        """Walk the OData error tree to find the most specific error code."""
        if exc.error is None:
            return None
        if exc.error.code in CAPACITY_ERROR_CODES:
            return exc.error.code
        # Check nested details
        if exc.error.details:
            for detail in exc.error.details:
                if detail.code in CAPACITY_ERROR_CODES:
                    return detail.code
        return exc.error.code

    @staticmethod
    def _is_capacity_error(exc: HttpResponseError, error_code: Optional[str]) -> bool:
        if error_code in CAPACITY_ERROR_CODES:
            return True
        error_str = str(exc).lower()
        return any(sub in error_str for sub in CAPACITY_ERROR_SUBSTRINGS)

    def _cleanup(
        self,
        rg_name: str,
        crg_name: str,
        cr_name: str,
        cr_created: bool,
        crg_created: bool,
    ) -> None:
        """Delete the CR first, then the CRG.  Swallow cleanup errors."""
        if cr_created:
            try:
                logger.info("Deleting CR '%s'", cr_name)
                self.compute_client.capacity_reservations.begin_delete(
                    rg_name, crg_name, cr_name
                ).result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not delete CR '%s': %s", cr_name, exc)

        if crg_created:
            try:
                logger.info("Deleting CRG '%s'", crg_name)
                self.compute_client.capacity_reservation_groups.delete(
                    rg_name, crg_name
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not delete CRG '%s': %s", crg_name, exc)

    def sweep_orphaned_probes(self) -> int:
        """Delete any orphaned cap-probe-* CRGs left by failed or interrupted probes."""
        cleaned = 0
        rg_name = self.probe_resource_group
        try:
            crgs = self.compute_client.capacity_reservation_groups.list_by_resource_group(rg_name)
            for crg in crgs:
                if crg.name and crg.name.startswith("cap-probe-crg-"):
                    try:
                        # Delete any CRs inside the CRG first
                        crs = self.compute_client.capacity_reservations.list_by_capacity_reservation_group(
                            rg_name, crg.name
                        )
                        for cr in crs:
                            try:
                                self.compute_client.capacity_reservations.begin_delete(
                                    rg_name, crg.name, cr.name
                                ).result()
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("Could not delete orphaned CR '%s': %s", cr.name, exc)
                        self.compute_client.capacity_reservation_groups.delete(rg_name, crg.name)
                        cleaned += 1
                        logger.info("Cleaned up orphaned CRG '%s'", crg.name)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Could not clean up CRG '%s': %s", crg.name, exc)
        except HttpResponseError as exc:
            if exc.status_code == 404:
                logger.info("Resource group '%s' not found — nothing to clean up", rg_name)
            else:
                raise
        return cleaned
