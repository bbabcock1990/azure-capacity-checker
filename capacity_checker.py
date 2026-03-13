"""
Azure Capacity Checker — ODCR probe logic with prerequisite checks.

Strategy: attempt to create an On-Demand Capacity Reservation (ODCR)
for the target VM size / region / zone. If Azure accepts the reservation
the capacity exists; if it rejects with a capacity-related error it does
not.  Either way the ephemeral resources are deleted immediately.

In addition to the ODCR probe, the checker validates prerequisites:
  - SKU availability / restrictions in the target region
  - vCPU quota headroom for the VM family
  - Capacity Reservation support for the VM SKU

These signals are combined into a confidence score (0–100).
"""
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import ResourceManagementClient

logger = logging.getLogger(__name__)

# ── Disclaimer ─────────────────────────────────────────────────────────────────
DISCLAIMER = (
    "IMPORTANT: This capacity check provides a point-in-time signal only. "
    "Azure capacity is dynamic and can change at any moment. This result "
    "does NOT guarantee that capacity will be available when you attempt to "
    "deploy. Use this API for directional guidance only — not as a "
    "deployment guarantee."
)

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


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass
class QuotaCheckResult:
    """Result of a vCPU quota check for a VM family in a region."""
    family: str
    region: str
    current_usage: int
    limit: int
    vcpus_needed: int
    sufficient: bool
    message: str


@dataclass
class SkuCheckResult:
    """Result of a SKU availability check in a region."""
    vm_size: str
    region: str
    available: bool
    capacity_reservation_supported: bool
    restrictions: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class CapacityCheckResult:
    """Result of an ODCR capacity probe for a VM size in a region."""
    vm_size: str
    region: str
    zone: Optional[str]
    available: bool
    message: str
    error_code: Optional[str] = field(default=None)


@dataclass
class FullCheckResult:
    """Combined result of all prerequisite checks + ODCR probe."""
    vm_size: str
    region: str
    zone: Optional[str]
    # Individual check results
    sku_check: Optional[SkuCheckResult]
    quota_check: Optional[QuotaCheckResult]
    capacity_check: Optional[CapacityCheckResult]
    # Aggregate signal
    confidence_score: int  # 0–100
    signal_level: str  # "High", "Medium", "Low", "None"
    summary: str
    disclaimer: str = field(default=DISCLAIMER)


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
        self._credential = None
        self._compute_client: Optional[ComputeManagementClient] = None
        self._resource_client: Optional[ResourceManagementClient] = None

    # ------------------------------------------------------------------
    # SDK client helpers
    # ------------------------------------------------------------------

    @property
    def credential(self):
        if self._credential is None:
            # Allow cross-tenant token acquisition so a user logged into
            # any tenant can access their own subscriptions.
            self._credential = DefaultAzureCredential(
                additionally_allowed_tenants=["*"]
            )
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
    # SKU availability check
    # ------------------------------------------------------------------

    def check_sku_availability(
        self, vm_size: str, region: str
    ) -> SkuCheckResult:
        """
        Check whether a VM SKU is available in a region and supports
        capacity reservations.

        Uses the Resource SKUs API (read-only, no cost).
        """
        logger.info("Checking SKU availability: %s in %s", vm_size, region)
        try:
            skus = self.compute_client.resource_skus.list(
                filter=f"location eq '{region}'"
            )
            for sku in skus:
                if (
                    sku.resource_type == "virtualMachines"
                    and sku.name.lower() == vm_size.lower()
                ):
                    # Check restrictions
                    restrictions = []
                    for r in (sku.restrictions or []):
                        reason = r.reason_code if r.reason_code else "Unknown"
                        restrictions.append(reason)

                    # Check capacity reservation support
                    cr_supported = False
                    for cap in (sku.capabilities or []):
                        if (
                            cap.name == "CapacityReservationSupported"
                            and cap.value and cap.value.lower() == "true"
                        ):
                            cr_supported = True
                            break

                    available = len(restrictions) == 0
                    msg_parts = []
                    if not available:
                        msg_parts.append(
                            f"SKU {vm_size} has restrictions in {region}: "
                            + ", ".join(restrictions)
                        )
                    if not cr_supported:
                        msg_parts.append(
                            f"SKU {vm_size} does not support Capacity Reservations"
                        )
                    if available and cr_supported:
                        msg_parts.append(
                            f"SKU {vm_size} is available in {region} and "
                            "supports Capacity Reservations"
                        )

                    return SkuCheckResult(
                        vm_size=vm_size,
                        region=region,
                        available=available,
                        capacity_reservation_supported=cr_supported,
                        restrictions=restrictions,
                        message="; ".join(msg_parts),
                    )

            return SkuCheckResult(
                vm_size=vm_size,
                region=region,
                available=False,
                capacity_reservation_supported=False,
                message=f"SKU {vm_size} not found in region {region}",
            )

        except HttpResponseError as exc:
            logger.warning("SKU availability check failed: %s", exc)
            return SkuCheckResult(
                vm_size=vm_size,
                region=region,
                available=False,
                capacity_reservation_supported=False,
                message=f"SKU check failed: {exc.message}",
            )

    # ------------------------------------------------------------------
    # Quota check
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_vm_family(vm_size: str) -> str:
        """
        Extract the VM family name from a VM size string for quota matching.

        Azure quota names use patterns like "Standard DSv3 Family vCPUs".
        We build a normalized key from the SKU to match against quota names.

        Examples:
          Standard_D4s_v3  → "dsv3"
          Standard_D8s_v5  → "dsv5"
          Standard_NC6s_v3 → "ncsv3"
          Standard_E16as_v5 → "easv5"
          Standard_NC96ads_A100_v4 → "ncadsv4"  (close enough for matching)
        """
        # Strip "Standard_" prefix
        name = re.sub(r"^Standard_", "", vm_size, flags=re.IGNORECASE)
        # Remove the instance-count digits that appear right after the family
        # letter prefix.  E.g. "D4s_v3" → "Ds_v3", "NC96ads_A100_v4" → "NCads_A100_v4"
        # Pattern: one or more uppercase letters, then digits, then rest
        name = re.sub(r"^([A-Za-z]+)\d+", r"\1", name)
        # Remove sub-model identifiers like "_A100"
        name = re.sub(r"_[A-Z]\d+", "", name)
        # Remove underscores and lowercase for comparison
        name = name.replace("_", "").lower()
        return name

    def check_quota(
        self, vm_size: str, region: str, quantity: int = 1
    ) -> QuotaCheckResult:
        """
        Check vCPU quota headroom for the VM family in a region.

        Uses the Compute Usage API (read-only, no cost).
        """
        logger.info("Checking quota for %s in %s", vm_size, region)
        family_key = self._extract_vm_family(vm_size)

        try:
            usages = self.compute_client.usage.list(region)
            best_match = None
            best_match_len = 999  # Prefer the shortest (most specific) match

            for usage in usages:
                usage_name = (usage.name.value or "").lower().replace(" ", "")
                usage_local = (usage.name.localized_value or "").lower().replace(" ", "")

                # Strip common prefixes so boundary check works correctly
                for prefix in ("standard", "standardfamily"):
                    if usage_name.startswith(prefix):
                        usage_name_stripped = usage_name[len(prefix):]
                        break
                else:
                    usage_name_stripped = usage_name

                for prefix in ("standard", "standardfamily"):
                    if usage_local.startswith(prefix):
                        usage_local_stripped = usage_local[len(prefix):]
                        break
                else:
                    usage_local_stripped = usage_local

                # Check if family_key matches as a distinct segment.
                # E.g. "dsv3" should match "dsv3familyvcpus" but NOT
                # "dadsv7familyvcpus".
                for candidate in (usage_name_stripped, usage_local_stripped,
                                  usage_name, usage_local):
                    if family_key not in candidate:
                        continue
                    idx = candidate.find(family_key)
                    if idx > 0 and candidate[idx - 1].isalpha():
                        continue
                    # Good match — prefer shorter names (more specific)
                    if len(candidate) < best_match_len:
                        best_match = usage
                        best_match_len = len(candidate)

            if best_match:
                usage = best_match
                current = usage.current_value
                limit = usage.limit
                headroom = limit - current
                # Estimate vCPUs needed from VM size name
                vcpu_match = re.search(r"(\d+)", vm_size)
                vcpus_needed = int(vcpu_match.group(1)) * quantity if vcpu_match else quantity

                sufficient = headroom >= vcpus_needed
                msg = (
                    f"Quota for {usage.name.localized_value}: "
                    f"{current}/{limit} used, {headroom} available, "
                    f"{vcpus_needed} needed — "
                    + ("SUFFICIENT" if sufficient else "INSUFFICIENT")
                )
                return QuotaCheckResult(
                    family=usage.name.localized_value or usage.name.value or family_key,
                    region=region,
                    current_usage=current,
                    limit=limit,
                    vcpus_needed=vcpus_needed,
                    sufficient=sufficient,
                    message=msg,
                )

            # Also check regional vCPU total
            return QuotaCheckResult(
                family=family_key,
                region=region,
                current_usage=-1,
                limit=-1,
                vcpus_needed=quantity,
                sufficient=True,  # Assume OK if we can't find the family
                message=(
                    f"Could not find specific quota for family '{family_key}' "
                    f"in {region}. Regional quota may still apply."
                ),
            )

        except HttpResponseError as exc:
            logger.warning("Quota check failed: %s", exc)
            return QuotaCheckResult(
                family=family_key,
                region=region,
                current_usage=-1,
                limit=-1,
                vcpus_needed=quantity,
                sufficient=True,  # Don't block on quota check failure
                message=f"Quota check failed: {exc.message}",
            )

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_confidence(
        sku_check: Optional[SkuCheckResult],
        quota_check: Optional[QuotaCheckResult],
        capacity_check: Optional[CapacityCheckResult],
    ) -> tuple[int, str]:
        """
        Calculate a 0–100 confidence score and textual signal level.

        Scoring breakdown:
          - SKU available + supports ODCR:  20 points
          - Quota sufficient:               20 points
          - ODCR probe succeeded:           60 points

        Signal levels:
          - 90–100: High   — all signals positive
          - 60–89:  Medium — most signals positive, some concerns
          - 20–59:  Low    — significant concerns
          - 0–19:   None   — capacity very unlikely
        """
        score = 0

        # SKU availability (20 pts)
        if sku_check:
            if sku_check.available and sku_check.capacity_reservation_supported:
                score += 20
            elif sku_check.available:
                score += 10  # Available but no ODCR support
            # else: 0

        # Quota headroom (20 pts)
        if quota_check:
            if quota_check.sufficient:
                score += 20
            elif quota_check.current_usage == -1:
                score += 10  # Unknown — partial credit
            # else: 0

        # ODCR probe (60 pts)
        if capacity_check:
            if capacity_check.available:
                score += 60
            # else: 0

        # Determine signal level
        if score >= 90:
            level = "High"
        elif score >= 60:
            level = "Medium"
        elif score >= 20:
            level = "Low"
        else:
            level = "None"

        return score, level

    # ------------------------------------------------------------------
    # Full check (orchestrates all signals)
    # ------------------------------------------------------------------

    def full_check(
        self,
        vm_size: str,
        region: str,
        zone: Optional[str] = None,
        quantity: int = 1,
    ) -> FullCheckResult:
        """
        Run all prerequisite checks and the ODCR probe, returning a
        combined result with confidence score and disclaimer.

        Order: SKU check → Quota check → ODCR probe → score.
        If a prerequisite fails fatally, the ODCR probe is skipped.
        """
        # 1. SKU availability
        sku_result = self.check_sku_availability(vm_size, region)

        # 2. Quota check
        quota_result = self.check_quota(vm_size, region, quantity)

        # 3. ODCR probe — skip if SKU is not found at all
        capacity_result: Optional[CapacityCheckResult] = None
        if sku_result.available:
            try:
                capacity_result = self.check_capacity(vm_size, region, zone, quantity)
            except Exception as exc:
                logger.error("ODCR probe failed unexpectedly: %s", exc)
                capacity_result = CapacityCheckResult(
                    vm_size=vm_size,
                    region=region,
                    zone=zone,
                    available=False,
                    message=f"ODCR probe error: {exc}",
                    error_code="ProbeError",
                )
        else:
            capacity_result = CapacityCheckResult(
                vm_size=vm_size,
                region=region,
                zone=zone,
                available=False,
                message=(
                    f"ODCR probe skipped — {sku_result.message}"
                ),
                error_code="SkuNotAvailable",
            )

        # 4. Calculate confidence
        score, level = self.calculate_confidence(
            sku_result, quota_result, capacity_result
        )

        # 5. Build summary
        qty_str = f"{quantity}x " if quantity > 1 else ""
        summary_parts = []
        if capacity_result and capacity_result.available:
            summary_parts.append(
                f"Capacity IS available for {qty_str}{vm_size} in {region}"
                + (f" (zone {zone})" if zone else "")
            )
        else:
            summary_parts.append(
                f"Capacity is NOT available for {qty_str}{vm_size} in {region}"
                + (f" (zone {zone})" if zone else "")
            )
        summary_parts.append(f"Confidence: {score}/100 ({level})")
        if not quota_result.sufficient:
            summary_parts.append("⚠ Insufficient vCPU quota")
        if not sku_result.capacity_reservation_supported:
            summary_parts.append("⚠ SKU does not support Capacity Reservations")

        return FullCheckResult(
            vm_size=vm_size,
            region=region,
            zone=zone,
            sku_check=sku_result,
            quota_check=quota_result,
            capacity_check=capacity_result,
            confidence_score=score,
            signal_level=level,
            summary=". ".join(summary_parts) + ".",
        )

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
        quantity: int = 1,
    ) -> CapacityCheckResult:
        """
        Probe capacity by attempting to create an ODCR Capacity Reservation.

        Flow:
          1. Create a CapacityReservationGroup (CRG) in the target region.
          2. Attempt to create a CapacityReservation (CR) for ``quantity`` VMs
             of the requested size.  This is the actual capacity probe — Azure
             will reject it if no capacity is available.
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
                "sku": {"name": vm_size, "capacity": quantity},
            }
            if zone:
                cr_body["zones"] = [zone]

            logger.info(
                "Probing capacity: vm_size='%s' region='%s' zone='%s' qty=%d",
                vm_size,
                region,
                zone,
                quantity,
            )
            try:
                poller = self.compute_client.capacity_reservations.begin_create_or_update(
                    rg_name, crg_name, cr_name, cr_body
                )
                poller.result()  # Block until the long-running operation resolves
                cr_created = True

                logger.info("AVAILABLE — %s x%d in %s (zone=%s)", vm_size, quantity, region, zone)
                return CapacityCheckResult(
                    vm_size=vm_size,
                    region=region,
                    zone=zone,
                    available=True,
                    message=(
                        f"Capacity is available for {quantity}x {vm_size} in {region}"
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
                            f"Capacity is NOT available for {quantity}x {vm_size} in {region}"
                            + (f" (zone {zone})" if zone else "")
                        ),
                        error_code=error_code,
                    )

                # Re-raise unexpected errors (bad VM SKU name, auth issue, …)
                raise

        finally:
            self._cleanup(rg_name, crg_name, cr_name, cr_created, crg_created)

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
        """
        Delete the CR first, then the CRG.  Attempts cleanup regardless of
        whether we *think* the resource was created, because a partially-
        completed long-running operation may have created the resource even
        though we never set the flag.
        """
        # Always try to delete the CR — it may exist even if cr_created is False
        # (e.g. the LRO failed after Azure accepted the create request).
        for attempt in range(1, 4):
            try:
                logger.info(
                    "Deleting CR '%s' (attempt %d/3, cr_created=%s)",
                    cr_name, attempt, cr_created,
                )
                poller = self.compute_client.capacity_reservations.begin_delete(
                    rg_name, crg_name, cr_name
                )
                poller.result()  # Wait for deletion to complete
                logger.info("CR '%s' deleted successfully", cr_name)
                break
            except HttpResponseError as exc:
                if exc.status_code == 404:
                    logger.info("CR '%s' does not exist (already deleted or never created)", cr_name)
                    break
                logger.warning(
                    "Could not delete CR '%s' (attempt %d/3): %s", cr_name, attempt, exc
                )
                if attempt == 3:
                    logger.error("FAILED to delete CR '%s' after 3 attempts", cr_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not delete CR '%s' (attempt %d/3): %s", cr_name, attempt, exc
                )
                if attempt == 3:
                    logger.error("FAILED to delete CR '%s' after 3 attempts", cr_name)

        # Now delete the CRG (only possible when all CRs are gone)
        if crg_created:
            for attempt in range(1, 4):
                try:
                    logger.info(
                        "Deleting CRG '%s' (attempt %d/3)", crg_name, attempt
                    )
                    self.compute_client.capacity_reservation_groups.delete(
                        rg_name, crg_name
                    )
                    logger.info("CRG '%s' deleted successfully", crg_name)
                    break
                except HttpResponseError as exc:
                    if exc.status_code == 404:
                        logger.info("CRG '%s' does not exist (already deleted)", crg_name)
                        break
                    logger.warning(
                        "Could not delete CRG '%s' (attempt %d/3): %s",
                        crg_name, attempt, exc,
                    )
                    if attempt == 3:
                        logger.error("FAILED to delete CRG '%s' after 3 attempts", crg_name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not delete CRG '%s' (attempt %d/3): %s",
                        crg_name, attempt, exc,
                    )
                    if attempt == 3:
                        logger.error("FAILED to delete CRG '%s' after 3 attempts", crg_name)

    def sweep_orphaned_probes(self) -> int:
        """
        Scan the probe resource group for any orphaned cap-probe-* CRGs
        and delete them.  Returns the number of resources cleaned up.

        This is a safety net for probes that failed to clean up after
        themselves (e.g. due to timeouts or process crashes).
        """
        rg_name = self.probe_resource_group
        cleaned = 0

        try:
            crgs = list(
                self.compute_client.capacity_reservation_groups.list_by_resource_group(
                    rg_name
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list CRGs for sweep: %s", exc)
            return 0

        for crg in crgs:
            if not crg.name.startswith("cap-probe-crg-"):
                continue

            logger.info("Sweep: found orphaned CRG '%s'", crg.name)

            # Delete any CRs inside first
            try:
                crs = list(
                    self.compute_client.capacity_reservations.list_by_capacity_reservation_group(
                        rg_name, crg.name
                    )
                )
                for cr in crs:
                    try:
                        logger.info("Sweep: deleting orphaned CR '%s'", cr.name)
                        self.compute_client.capacity_reservations.begin_delete(
                            rg_name, crg.name, cr.name
                        ).result()
                        cleaned += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Sweep: could not delete CR '%s': %s", cr.name, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sweep: could not list CRs in '%s': %s", crg.name, exc)

            # Now delete the CRG
            try:
                self.compute_client.capacity_reservation_groups.delete(rg_name, crg.name)
                logger.info("Sweep: deleted orphaned CRG '%s'", crg.name)
                cleaned += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sweep: could not delete CRG '%s': %s", crg.name, exc)

        return cleaned
