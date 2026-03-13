"""
Integration tests for Azure Capacity Checker — end-to-end against real Azure.

These tests create real Azure resources (Capacity Reservation Groups and
Capacity Reservations) and delete them.  They require:
  - Active Azure credentials (az login or service principal env vars)
  - A valid subscription ID (set via AZURE_SUBSCRIPTION_ID env var or
    auto-discovered from Azure CLI)
  - Contributor RBAC on the subscription or probe resource group

Run with:
    python -m pytest test_integration.py -v -s --timeout=300

The -s flag shows live output (useful for watching 30-90s probes).
Skip these in CI unless you have a dedicated test subscription.

Test inputs:
  - Standard_D4as_v4 / southcentralus / quantity=2
  - Standard_D8as_v7 / uksouth / zone=1 / quantity=1
"""
import os

import pytest
from httpx import ASGITransport, AsyncClient

from main import app

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUBSCRIPTION_ID = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
SUB_PARAM = f"&subscription_id={SUBSCRIPTION_ID}" if SUBSCRIPTION_ID else ""
SUB_QUERY = {"subscription_id": SUBSCRIPTION_ID} if SUBSCRIPTION_ID else {}

# Test inputs
VM1 = "Standard_D4as_v4"
REGION1 = "southcentralus"
QTY1 = 2

VM2 = "Standard_D8as_v7"
REGION2 = "uksouth"
ZONE2 = "1"
QTY2 = 1


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
# Health check — validates credentials and subscription are configured
# ---------------------------------------------------------------------------

class TestHealthIntegration:
    @pytest.mark.asyncio
    async def test_health_returns_healthy(self, client):
        """Verify the service detects a valid subscription (env or auto-discovery)."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy", (
            f"Service is misconfigured — check AZURE_SUBSCRIPTION_ID or run 'az login'. "
            f"Response: {data}"
        )
        assert data["subscription_configured"] is True


# ---------------------------------------------------------------------------
# SKU check — read-only, no resources created
# ---------------------------------------------------------------------------

class TestCheckSkuIntegration:
    @pytest.mark.asyncio
    async def test_sku_check_vm1(self, client):
        """Standard_D4as_v4 should exist in southcentralus."""
        resp = await client.get(
            "/api/v1/check-sku",
            params={"vm_size": VM1, "region": REGION1, **SUB_QUERY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vm_size"] == VM1
        assert data["region"] == REGION1
        assert data["available"] is True, f"SKU {VM1} not available in {REGION1}: {data}"
        assert isinstance(data["capacity_reservation_supported"], bool)

    @pytest.mark.asyncio
    async def test_sku_check_vm2(self, client):
        """Standard_D8as_v7 should exist in uksouth."""
        resp = await client.get(
            "/api/v1/check-sku",
            params={"vm_size": VM2, "region": REGION2, **SUB_QUERY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vm_size"] == VM2
        assert data["region"] == REGION2
        assert data["available"] is True, f"SKU {VM2} not available in {REGION2}: {data}"

    @pytest.mark.asyncio
    async def test_sku_check_invalid_sku(self, client):
        """A made-up SKU should come back as unavailable (not a 500)."""
        resp = await client.get(
            "/api/v1/check-sku",
            params={"vm_size": "Standard_FAKE_v99", "region": REGION1, **SUB_QUERY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False


# ---------------------------------------------------------------------------
# Quota check — read-only, no resources created
# ---------------------------------------------------------------------------

class TestCheckQuotaIntegration:
    @pytest.mark.asyncio
    async def test_quota_check_vm1(self, client):
        """Quota check for Standard_D4as_v4 in southcentralus."""
        resp = await client.get(
            "/api/v1/check-quota",
            params={"vm_size": VM1, "region": REGION1, **SUB_QUERY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["region"] == REGION1
        assert isinstance(data["current_usage"], int)
        assert isinstance(data["limit"], int)
        assert isinstance(data["sufficient"], bool)

    @pytest.mark.asyncio
    async def test_quota_check_vm2(self, client):
        """Quota check for Standard_D8as_v7 in uksouth."""
        resp = await client.get(
            "/api/v1/check-quota",
            params={"vm_size": VM2, "region": REGION2, **SUB_QUERY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["region"] == REGION2


# ---------------------------------------------------------------------------
# ODCR capacity probe — creates and deletes real resources (30-90s)
# ---------------------------------------------------------------------------

class TestCheckCapacityIntegration:
    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_capacity_probe_vm1(self, client):
        """End-to-end ODCR probe for Standard_D4as_v4 in southcentralus."""
        resp = await client.get(
            "/api/v1/check-capacity",
            params={"vm_size": VM1, "region": REGION1, **SUB_QUERY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vm_size"] == VM1
        assert data["region"] == REGION1
        assert isinstance(data["available"], bool)
        assert data["message"]  # should have a non-empty message

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_capacity_probe_vm2_with_zone(self, client):
        """End-to-end ODCR probe for Standard_D8as_v7 in uksouth zone 1."""
        resp = await client.get(
            "/api/v1/check-capacity",
            params={"vm_size": VM2, "region": REGION2, "zone": ZONE2, **SUB_QUERY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vm_size"] == VM2
        assert data["region"] == REGION2
        assert data["zone"] == ZONE2
        assert isinstance(data["available"], bool)

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_capacity_probe_report_format(self, client):
        """ODCR probe with report=true returns plain text."""
        resp = await client.get(
            "/api/v1/check-capacity",
            params={"vm_size": VM1, "region": REGION1, "report": "true", **SUB_QUERY},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain; charset=utf-8"
        assert "Azure Capacity Check Report" in resp.text
        assert VM1 in resp.text


# ---------------------------------------------------------------------------
# Full check — SKU + quota + ODCR probe (30-90s)
# ---------------------------------------------------------------------------

class TestFullCheckIntegration:
    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_full_check_vm1(self, client):
        """Full check for Standard_D4as_v4 x2 in southcentralus."""
        resp = await client.get(
            "/api/v1/check",
            params={"vm_size": VM1, "region": REGION1, "quantity": QTY1, **SUB_QUERY},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Structure
        assert data["vm_size"] == VM1
        assert data["region"] == REGION1
        assert data["quantity"] == QTY1

        # SKU check ran
        assert "sku_check" in data
        assert data["sku_check"]["vm_size"] == VM1
        assert isinstance(data["sku_check"]["available"], bool)
        assert isinstance(data["sku_check"]["capacity_reservation_supported"], bool)

        # Quota check ran
        assert "quota_check" in data
        assert isinstance(data["quota_check"]["current_usage"], int)
        assert isinstance(data["quota_check"]["limit"], int)
        assert isinstance(data["quota_check"]["sufficient"], bool)

        # Capacity probe ran
        assert isinstance(data["capacity_available"], bool)
        assert data["capacity_message"]

        # Confidence scoring
        assert 0 <= data["confidence_score"] <= 100
        assert data["signal_level"] in ("High", "Medium", "Low", "None")
        assert data["summary"]
        assert data["disclaimer"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_full_check_vm2_with_zone(self, client):
        """Full check for Standard_D8as_v7 x1 in uksouth zone 1."""
        resp = await client.get(
            "/api/v1/check",
            params={
                "vm_size": VM2, "region": REGION2,
                "zone": ZONE2, "quantity": QTY2, **SUB_QUERY,
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["vm_size"] == VM2
        assert data["region"] == REGION2
        assert data["zone"] == ZONE2
        assert data["quantity"] == QTY2
        assert 0 <= data["confidence_score"] <= 100
        assert data["signal_level"] in ("High", "Medium", "Low", "None")

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_full_check_report_format(self, client):
        """Full check with report=true returns plain-text report."""
        resp = await client.get(
            "/api/v1/check",
            params={
                "vm_size": VM1, "region": REGION1,
                "quantity": QTY1, "report": "true", **SUB_QUERY,
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain; charset=utf-8"
        text = resp.text
        assert "Azure Capacity Check Report" in text
        assert VM1 in text
        assert "Signal:" in text


# ---------------------------------------------------------------------------
# Batch full check — two items in parallel (60-180s total)
# ---------------------------------------------------------------------------

class TestBatchFullCheckIntegration:
    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_batch_full_check_two_items(self, client):
        """Batch full check with both test inputs."""
        resp = await client.post(
            "/api/v1/check/batch",
            params=SUB_QUERY,
            json={
                "checks": [
                    {"vm_size": VM1, "region": REGION1, "quantity": QTY1},
                    {"vm_size": VM2, "region": REGION2, "zone": ZONE2, "quantity": QTY2},
                ]
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 2

        r1 = results[0]
        assert r1["vm_size"] == VM1
        assert r1["region"] == REGION1
        assert r1["quantity"] == QTY1
        assert isinstance(r1["capacity_available"], bool)
        assert 0 <= r1["confidence_score"] <= 100

        r2 = results[1]
        assert r2["vm_size"] == VM2
        assert r2["region"] == REGION2
        assert r2["zone"] == ZONE2
        assert r2["quantity"] == QTY2
        assert isinstance(r2["capacity_available"], bool)
        assert 0 <= r2["confidence_score"] <= 100

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_batch_full_check_report(self, client):
        """Batch full check with report=true."""
        resp = await client.post(
            "/api/v1/check/batch",
            params={**SUB_QUERY, "report": "true"},
            json={
                "checks": [
                    {"vm_size": VM1, "region": REGION1, "quantity": QTY1},
                    {"vm_size": VM2, "region": REGION2, "zone": ZONE2, "quantity": QTY2},
                ]
            },
        )
        assert resp.status_code == 200
        text = resp.text
        assert "Azure Capacity Check Report" in text
        assert VM1 in text
        assert VM2 in text


# ---------------------------------------------------------------------------
# Batch ODCR-only probe
# ---------------------------------------------------------------------------

class TestBatchCapacityIntegration:
    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_batch_capacity_probe(self, client):
        """Batch ODCR-only probe with both test inputs."""
        resp = await client.post(
            "/api/v1/check-capacity/batch",
            params=SUB_QUERY,
            json={
                "checks": [
                    {"vm_size": VM1, "region": REGION1, "quantity": QTY1},
                    {"vm_size": VM2, "region": REGION2, "zone": ZONE2, "quantity": QTY2},
                ]
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 2
        for r in results:
            assert isinstance(r["available"], bool)
            assert r["message"]


# ---------------------------------------------------------------------------
# Cleanup — safe to run, deletes only cap-probe-* resources
# ---------------------------------------------------------------------------

class TestCleanupIntegration:
    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_cleanup_runs_successfully(self, client):
        """Cleanup should succeed even if there are no orphans."""
        resp = await client.post("/api/v1/cleanup", params=SUB_QUERY)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["cleaned"], int)
        assert data["cleaned"] >= 0
        assert "message" in data
