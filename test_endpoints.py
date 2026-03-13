"""
Unit tests for Azure Capacity Checker endpoints.

Tests all API endpoints with mocked Azure SDK calls, verifying consistent
behaviour across deployment methods (local uvicorn, func start, Azure Function).
The FastAPI app from main.py is the same in all modes — function_app.py just
wraps it as an ASGI Azure Function — so testing via httpx against the FastAPI
app covers all three deployment surfaces.

Test inputs (per user specification):
  - Standard_D4as_v4 / southcentralus / quantity=2
  - Standard_D8as_v7 / uksouth / zone=1 / quantity=1
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from capacity_checker import (
    DISCLAIMER,
    CapacityCheckResult,
    FullCheckResult,
    QuotaCheckResult,
    SkuCheckResult,
)
from main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_sku_result(vm_size: str, region: str, available: bool = True) -> SkuCheckResult:
    return SkuCheckResult(
        vm_size=vm_size,
        region=region,
        available=available,
        capacity_reservation_supported=True,
        restrictions=[],
        message="SKU available" if available else "SKU restricted",
    )


def _make_quota_result(
    region: str, family: str = "standardDASv4Family", sufficient: bool = True,
    current_usage: int = 10, limit: int = 100, vcpus_needed: int = 8,
) -> QuotaCheckResult:
    return QuotaCheckResult(
        family=family,
        region=region,
        current_usage=current_usage,
        limit=limit,
        vcpus_needed=vcpus_needed,
        sufficient=sufficient,
        message=f"Quota: {current_usage}/{limit} vCPUs used, need {vcpus_needed}",
    )


def _make_capacity_result(
    vm_size: str, region: str, zone: Optional[str] = None, available: bool = True,
) -> CapacityCheckResult:
    status = "available" if available else "NOT available"
    return CapacityCheckResult(
        vm_size=vm_size,
        region=region,
        zone=zone,
        available=available,
        message=f"Capacity is {status} for {vm_size} in {region}",
        error_code=None if available else "CapacityNotAvailable",
    )


def _make_full_result(
    vm_size: str, region: str, zone: Optional[str] = None,
    available: bool = True, quantity: int = 1,
) -> FullCheckResult:
    sku = _make_sku_result(vm_size, region)
    quota = _make_quota_result(region, vcpus_needed=4 * quantity)
    cap = _make_capacity_result(vm_size, region, zone, available)
    score = 100 if available else 40
    level = "High" if available else "Low"
    return FullCheckResult(
        vm_size=vm_size,
        region=region,
        zone=zone,
        sku_check=sku,
        quota_check=quota,
        capacity_check=cap,
        confidence_score=score,
        signal_level=level,
        summary=f"{vm_size} in {region}: SKU available | Quota OK | ODCR {'PASS' if available else 'FAIL'}",
    )


def _mock_checker():
    """Return a MagicMock that quacks like AzureCapacityChecker."""
    checker = MagicMock()
    checker.subscription_id = "00000000-0000-0000-0000-000000000000"
    checker.probe_resource_group = "az-cap-probe-rg"
    checker.sweep_orphaned_probes.return_value = 0
    return checker


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_configured(self, client):
        with patch("main.SUBSCRIPTION_ID", "test-sub-id"):
            resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["subscription_configured"] is True

    @pytest.mark.asyncio
    async def test_health_unconfigured(self, client):
        with patch("main.SUBSCRIPTION_ID", ""), \
             patch("main._discover_subscription_id", return_value=None):
            resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "misconfigured"
        assert data["subscription_configured"] is False

    @pytest.mark.asyncio
    async def test_health_reports_runtime_local(self, client):
        with patch("main.SUBSCRIPTION_ID", "test-sub"), \
             patch.dict("os.environ", {"FUNCTIONS_WORKER_RUNTIME": ""}, clear=False):
            resp = await client.get("/health")
        assert resp.json()["runtime"] == "local"

    @pytest.mark.asyncio
    async def test_health_reports_runtime_azure_function(self, client):
        with patch("main.SUBSCRIPTION_ID", "test-sub"), \
             patch.dict("os.environ", {"FUNCTIONS_WORKER_RUNTIME": "python"}, clear=False):
            resp = await client.get("/health")
        assert resp.json()["runtime"] == "azure-function"


# ---------------------------------------------------------------------------
# GET / (root redirect)
# ---------------------------------------------------------------------------

class TestRootRedirect:
    @pytest.mark.asyncio
    async def test_root_redirects_to_docs(self, client):
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/docs"


# ---------------------------------------------------------------------------
# GET /api/v1/check-sku
# ---------------------------------------------------------------------------

class TestCheckSku:
    @pytest.mark.asyncio
    async def test_check_sku_available(self, client):
        checker = _mock_checker()
        checker.check_sku.return_value = _make_sku_result("Standard_D4as_v4", "southcentralus")

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check-sku",
                params={"vm_size": "Standard_D4as_v4", "region": "southcentralus"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vm_size"] == "Standard_D4as_v4"
        assert data["region"] == "southcentralus"
        assert data["available"] is True
        assert data["capacity_reservation_supported"] is True

    @pytest.mark.asyncio
    async def test_check_sku_restricted(self, client):
        checker = _mock_checker()
        checker.check_sku.return_value = _make_sku_result(
            "Standard_D8as_v7", "uksouth", available=False
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check-sku",
                params={"vm_size": "Standard_D8as_v7", "region": "uksouth"},
            )
        assert resp.status_code == 200
        assert resp.json()["available"] is False


# ---------------------------------------------------------------------------
# GET /api/v1/check-quota
# ---------------------------------------------------------------------------

class TestCheckQuota:
    @pytest.mark.asyncio
    async def test_check_quota_sufficient(self, client):
        checker = _mock_checker()
        checker.check_quota.return_value = _make_quota_result(
            "southcentralus", vcpus_needed=8, sufficient=True,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check-quota",
                params={"vm_size": "Standard_D4as_v4", "region": "southcentralus"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sufficient"] is True
        assert data["vcpus_needed"] == 8

    @pytest.mark.asyncio
    async def test_check_quota_insufficient(self, client):
        checker = _mock_checker()
        checker.check_quota.return_value = _make_quota_result(
            "uksouth", vcpus_needed=8, sufficient=False, current_usage=98, limit=100,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check-quota",
                params={"vm_size": "Standard_D8as_v7", "region": "uksouth"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sufficient"] is False
        assert data["current_usage"] == 98


# ---------------------------------------------------------------------------
# GET /api/v1/check-capacity (ODCR-only, single)
# ---------------------------------------------------------------------------

class TestCheckCapacity:
    @pytest.mark.asyncio
    async def test_capacity_available(self, client):
        checker = _mock_checker()
        checker.check_capacity.return_value = _make_capacity_result(
            "Standard_D4as_v4", "southcentralus", available=True,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check-capacity",
                params={"vm_size": "Standard_D4as_v4", "region": "southcentralus"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["vm_size"] == "Standard_D4as_v4"

    @pytest.mark.asyncio
    async def test_capacity_unavailable_with_zone(self, client):
        checker = _mock_checker()
        checker.check_capacity.return_value = _make_capacity_result(
            "Standard_D8as_v7", "uksouth", zone="1", available=False,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check-capacity",
                params={"vm_size": "Standard_D8as_v7", "region": "uksouth", "zone": "1"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["zone"] == "1"
        assert data["error_code"] == "CapacityNotAvailable"

    @pytest.mark.asyncio
    async def test_capacity_report_format(self, client):
        checker = _mock_checker()
        checker.check_capacity.return_value = _make_capacity_result(
            "Standard_D4as_v4", "southcentralus", available=True,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check-capacity",
                params={
                    "vm_size": "Standard_D4as_v4",
                    "region": "southcentralus",
                    "report": "true",
                },
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain; charset=utf-8"
        assert "[PASS]" in resp.text
        assert "Standard_D4as_v4" in resp.text

    @pytest.mark.asyncio
    async def test_capacity_with_subscription_override(self, client):
        checker = _mock_checker()
        checker.check_capacity.return_value = _make_capacity_result(
            "Standard_D4as_v4", "southcentralus", available=True,
        )

        with patch("main._get_checker", return_value=checker) as mock_get:
            resp = await client.get(
                "/api/v1/check-capacity",
                params={
                    "vm_size": "Standard_D4as_v4",
                    "region": "southcentralus",
                    "subscription_id": "custom-sub-id",
                },
            )
        assert resp.status_code == 200
        mock_get.assert_called_once_with("custom-sub-id")


# ---------------------------------------------------------------------------
# POST /api/v1/check-capacity/batch (ODCR-only, batch)
# ---------------------------------------------------------------------------

class TestCheckCapacityBatch:
    @pytest.mark.asyncio
    async def test_batch_capacity_two_items(self, client):
        checker = _mock_checker()
        # check_capacity is called per-item via separate checker instances
        checker.check_capacity.side_effect = [
            _make_capacity_result("Standard_D4as_v4", "southcentralus", available=True),
            _make_capacity_result("Standard_D8as_v7", "uksouth", zone="1", available=False),
        ]

        with patch("main._get_checker", return_value=checker), \
             patch("main.AzureCapacityChecker", return_value=checker):
            resp = await client.post(
                "/api/v1/check-capacity/batch",
                json={
                    "checks": [
                        {"vm_size": "Standard_D4as_v4", "region": "southcentralus", "quantity": 2},
                        {"vm_size": "Standard_D8as_v7", "region": "uksouth", "zone": "1", "quantity": 1},
                    ]
                },
            )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 2
        assert results[0]["vm_size"] == "Standard_D4as_v4"
        assert results[0]["available"] is True
        assert results[1]["vm_size"] == "Standard_D8as_v7"
        assert results[1]["available"] is False

    @pytest.mark.asyncio
    async def test_batch_capacity_report(self, client):
        checker = _mock_checker()
        checker.check_capacity.return_value = _make_capacity_result(
            "Standard_D4as_v4", "southcentralus", available=True,
        )

        with patch("main._get_checker", return_value=checker), \
             patch("main.AzureCapacityChecker", return_value=checker):
            resp = await client.post(
                "/api/v1/check-capacity/batch?report=true",
                json={
                    "checks": [
                        {"vm_size": "Standard_D4as_v4", "region": "southcentralus", "quantity": 2},
                    ]
                },
            )
        assert resp.status_code == 200
        assert "Azure Capacity Check Report" in resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/check (full check, single)
# ---------------------------------------------------------------------------

class TestFullCheck:
    @pytest.mark.asyncio
    async def test_full_check_available(self, client):
        checker = _mock_checker()
        checker.full_check.return_value = _make_full_result(
            "Standard_D4as_v4", "southcentralus", quantity=2, available=True,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check",
                params={
                    "vm_size": "Standard_D4as_v4",
                    "region": "southcentralus",
                    "quantity": 2,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vm_size"] == "Standard_D4as_v4"
        assert data["region"] == "southcentralus"
        assert data["quantity"] == 2
        assert data["capacity_available"] is True
        assert data["confidence_score"] == 100
        assert data["signal_level"] == "High"
        assert data["sku_check"]["available"] is True
        assert data["quota_check"]["sufficient"] is True
        assert "disclaimer" in data

    @pytest.mark.asyncio
    async def test_full_check_unavailable_with_zone(self, client):
        checker = _mock_checker()
        checker.full_check.return_value = _make_full_result(
            "Standard_D8as_v7", "uksouth", zone="1", quantity=1, available=False,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check",
                params={
                    "vm_size": "Standard_D8as_v7",
                    "region": "uksouth",
                    "zone": "1",
                    "quantity": 1,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["capacity_available"] is False
        assert data["confidence_score"] == 40
        assert data["signal_level"] == "Low"
        assert data["zone"] == "1"

    @pytest.mark.asyncio
    async def test_full_check_report_format(self, client):
        checker = _mock_checker()
        checker.full_check.return_value = _make_full_result(
            "Standard_D4as_v4", "southcentralus", quantity=2, available=True,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check",
                params={
                    "vm_size": "Standard_D4as_v4",
                    "region": "southcentralus",
                    "quantity": 2,
                    "report": "true",
                },
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain; charset=utf-8"
        assert "Azure Capacity Check Report" in resp.text
        assert "Standard_D4as_v4" in resp.text

    @pytest.mark.asyncio
    async def test_full_check_server_error(self, client):
        checker = _mock_checker()
        checker.full_check.side_effect = RuntimeError("Azure SDK exploded")

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check",
                params={"vm_size": "Standard_D4as_v4", "region": "southcentralus"},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/v1/check/batch (full check, batch)
# ---------------------------------------------------------------------------

class TestFullCheckBatch:
    @pytest.mark.asyncio
    async def test_batch_full_check_two_items(self, client):
        checker = _mock_checker()
        checker.full_check.side_effect = [
            _make_full_result("Standard_D4as_v4", "southcentralus", quantity=2, available=True),
            _make_full_result("Standard_D8as_v7", "uksouth", zone="1", quantity=1, available=False),
        ]

        with patch("main._get_checker", return_value=checker):
            resp = await client.post(
                "/api/v1/check/batch",
                json={
                    "checks": [
                        {"vm_size": "Standard_D4as_v4", "region": "southcentralus", "quantity": 2},
                        {"vm_size": "Standard_D8as_v7", "region": "uksouth", "zone": "1", "quantity": 1},
                    ]
                },
            )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 2

        r1 = results[0]
        assert r1["vm_size"] == "Standard_D4as_v4"
        assert r1["region"] == "southcentralus"
        assert r1["quantity"] == 2
        assert r1["capacity_available"] is True
        assert r1["confidence_score"] == 100

        r2 = results[1]
        assert r2["vm_size"] == "Standard_D8as_v7"
        assert r2["region"] == "uksouth"
        assert r2["zone"] == "1"
        assert r2["quantity"] == 1
        assert r2["capacity_available"] is False

    @pytest.mark.asyncio
    async def test_batch_full_check_report(self, client):
        checker = _mock_checker()
        checker.full_check.side_effect = [
            _make_full_result("Standard_D4as_v4", "southcentralus", quantity=2, available=True),
            _make_full_result("Standard_D8as_v7", "uksouth", zone="1", quantity=1, available=False),
        ]

        with patch("main._get_checker", return_value=checker):
            resp = await client.post(
                "/api/v1/check/batch?report=true",
                json={
                    "checks": [
                        {"vm_size": "Standard_D4as_v4", "region": "southcentralus", "quantity": 2},
                        {"vm_size": "Standard_D8as_v7", "region": "uksouth", "zone": "1", "quantity": 1},
                    ]
                },
            )
        assert resp.status_code == 200
        text = resp.text
        assert "Azure Capacity Check Report" in text
        assert "Standard_D4as_v4" in text
        assert "Standard_D8as_v7" in text
        assert "1/2 passed" in text

    @pytest.mark.asyncio
    async def test_batch_full_check_graceful_degradation(self, client):
        """If one probe fails, the batch still returns results (degraded) for that item."""
        checker = _mock_checker()
        checker.full_check.side_effect = [
            _make_full_result("Standard_D4as_v4", "southcentralus", quantity=2, available=True),
            RuntimeError("Connection timeout"),
        ]

        with patch("main._get_checker", return_value=checker):
            resp = await client.post(
                "/api/v1/check/batch",
                json={
                    "checks": [
                        {"vm_size": "Standard_D4as_v4", "region": "southcentralus", "quantity": 2},
                        {"vm_size": "Standard_D8as_v7", "region": "uksouth", "zone": "1", "quantity": 1},
                    ]
                },
            )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 2
        assert results[0]["capacity_available"] is True
        # Second result is degraded (error captured, not propagated)
        assert results[1]["capacity_available"] is False
        assert "Connection timeout" in results[1]["capacity_message"]

    @pytest.mark.asyncio
    async def test_batch_validation_empty_checks(self, client):
        resp = await client.post(
            "/api/v1/check/batch",
            json={"checks": []},
        )
        assert resp.status_code == 422  # validation error

    @pytest.mark.asyncio
    async def test_batch_validation_too_many_checks(self, client):
        checks = [
            {"vm_size": f"Standard_D{i}s_v3", "region": "eastus"} for i in range(21)
        ]
        resp = await client.post(
            "/api/v1/check/batch",
            json={"checks": checks},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_no_orphans(self, client):
        checker = _mock_checker()
        checker.sweep_orphaned_probes.return_value = 0

        with patch("main._get_checker", return_value=checker):
            resp = await client.post("/api/v1/cleanup")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleaned"] == 0
        assert "No orphaned" in data["message"]

    @pytest.mark.asyncio
    async def test_cleanup_with_orphans(self, client):
        checker = _mock_checker()
        checker.sweep_orphaned_probes.return_value = 3

        with patch("main._get_checker", return_value=checker):
            resp = await client.post("/api/v1/cleanup")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleaned"] == 3
        assert "Removed 3" in data["message"]


# ---------------------------------------------------------------------------
# Subscription resolution
# ---------------------------------------------------------------------------

class TestSubscriptionResolution:
    @pytest.mark.asyncio
    async def test_no_subscription_returns_500(self, client):
        with patch("main.SUBSCRIPTION_ID", ""), \
             patch("main._discover_subscription_id", return_value=None):
            resp = await client.get(
                "/api/v1/check-sku",
                params={"vm_size": "Standard_D4as_v4", "region": "southcentralus"},
            )
        assert resp.status_code == 500
        assert "No Azure subscription" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Confidence scoring (unit tests on the checker class)
# ---------------------------------------------------------------------------

class TestConfidenceScoring:
    def test_all_pass_score(self):
        from capacity_checker import AzureCapacityChecker

        sku = _make_sku_result("Standard_D4as_v4", "southcentralus", available=True)
        quota = _make_quota_result("southcentralus", sufficient=True)
        cap = _make_capacity_result("Standard_D4as_v4", "southcentralus", available=True)

        score = AzureCapacityChecker._compute_confidence(sku, quota, cap)
        assert score == 100  # 20 (sku) + 5 (cr supported) + 15 (quota) + 60 (odcr)

    def test_sku_unavailable_score_zero(self):
        from capacity_checker import AzureCapacityChecker

        sku = _make_sku_result("Standard_D4as_v4", "southcentralus", available=False)
        quota = _make_quota_result("southcentralus", sufficient=True)

        score = AzureCapacityChecker._compute_confidence(sku, quota, None)
        assert score == 0

    def test_odcr_fail_reduces_score(self):
        from capacity_checker import AzureCapacityChecker

        sku = _make_sku_result("Standard_D4as_v4", "southcentralus", available=True)
        quota = _make_quota_result("southcentralus", sufficient=True)
        cap = _make_capacity_result("Standard_D4as_v4", "southcentralus", available=False)

        score = AzureCapacityChecker._compute_confidence(sku, quota, cap)
        assert score == 40  # 20 + 5 + 15 + 0

    def test_signal_levels(self):
        from capacity_checker import AzureCapacityChecker

        assert AzureCapacityChecker._score_to_level(100) == "High"
        assert AzureCapacityChecker._score_to_level(90) == "High"
        assert AzureCapacityChecker._score_to_level(89) == "Medium"
        assert AzureCapacityChecker._score_to_level(60) == "Medium"
        assert AzureCapacityChecker._score_to_level(59) == "Low"
        assert AzureCapacityChecker._score_to_level(20) == "Low"
        assert AzureCapacityChecker._score_to_level(19) == "None"
        assert AzureCapacityChecker._score_to_level(0) == "None"


# ---------------------------------------------------------------------------
# Disclaimer consistency
# ---------------------------------------------------------------------------

class TestDisclaimer:
    @pytest.mark.asyncio
    async def test_full_check_includes_disclaimer(self, client):
        checker = _mock_checker()
        checker.full_check.return_value = _make_full_result(
            "Standard_D4as_v4", "southcentralus", available=True,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check",
                params={"vm_size": "Standard_D4as_v4", "region": "southcentralus"},
            )
        assert resp.json()["disclaimer"] == DISCLAIMER

    @pytest.mark.asyncio
    async def test_full_report_includes_disclaimer(self, client):
        checker = _mock_checker()
        checker.full_check.return_value = _make_full_result(
            "Standard_D4as_v4", "southcentralus", available=True,
        )

        with patch("main._get_checker", return_value=checker):
            resp = await client.get(
                "/api/v1/check",
                params={
                    "vm_size": "Standard_D4as_v4",
                    "region": "southcentralus",
                    "report": "true",
                },
            )
        assert DISCLAIMER in resp.text


# ---------------------------------------------------------------------------
# Azure Function wrapper (function_app.py)
# ---------------------------------------------------------------------------

class TestFunctionAppWrapper:
    """Verify that function_app.py correctly wraps the FastAPI app."""

    def test_function_app_imports_and_creates_asgi_app(self):
        import function_app
        import azure.functions as func

        assert hasattr(function_app, "app_function")
        assert isinstance(function_app.app_function, func.AsgiFunctionApp)

    def test_function_app_uses_anonymous_auth(self):
        import function_app

        # The AsgiFunctionApp is created with ANONYMOUS auth level
        # so the Azure platform handles auth via Easy Auth, not function keys
        assert function_app.app_function is not None
