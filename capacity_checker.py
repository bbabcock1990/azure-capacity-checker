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


@dataclass
class CapacityCheckResult:
    vm_size: str
    region: str
    zone: Optional[str]
    available: bool
    message: str
    error_code: Optional[str] = field(default=None)


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
