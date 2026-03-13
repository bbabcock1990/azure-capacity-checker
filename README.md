# Azure Capacity Checker

A lightweight REST API that checks **real-time Azure VM capacity** using the [On-Demand Capacity Reservations (ODCR)](https://learn.microsoft.com/en-us/azure/virtual-machines/capacity-reservation-overview) API, with built-in **prerequisite validation** (SKU availability, quota headroom) and a **confidence score**.

> ⚠️ **Disclaimer:** Capacity checks provide a **point-in-time signal only**. Azure capacity is dynamic and can change at any moment. Results do **NOT** guarantee that capacity will be available when you deploy. Use this API for directional guidance only — not as a deployment guarantee.

## How it works

```
Client → GET /api/v1/check?vm_size=Standard_D4s_v3&region=eastus
           │
           ▼
   1. Check SKU availability (read-only)
      └─ Exists? Restrictions? ODCR supported?
           │
           ▼
   2. Check vCPU quota headroom (read-only)
      └─ Current usage vs limit for VM family
           │
           ▼
   3. ODCR Probe (create/delete ephemeral resources)
      └─ Create CRG → Create CR → Record result → Delete both
           │
           ▼
   4. Calculate confidence score (0–100)
      └─ SKU: 20pts | ODCR-supported: 5pts | Quota: 15pts | ODCR: 60pts
           │
           ▼
   Return JSON with prerequisites + probe + score + disclaimer
```

> **Latency note:** Each probe creates and deletes real Azure resources, so expect **30 – 90 seconds** per request.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.11+ |
| Azure CLI | For local authentication (`az login`) |
| Azure subscription | Needed to create ODCR resources |
| RBAC role | `Contributor` (or a custom role) on the subscription / resource group |

### Required Azure RBAC actions

```
Microsoft.Compute/capacityReservationGroups/write
Microsoft.Compute/capacityReservationGroups/delete
Microsoft.Compute/capacityReservationGroups/capacityReservations/write
Microsoft.Compute/capacityReservationGroups/capacityReservations/delete
Microsoft.Compute/skus/read                   # SKU availability check
Microsoft.Compute/locations/usages/read       # Quota check
Microsoft.Resources/resourceGroups/write      # only if auto-creating the RG
```

---

## Authentication

The API uses [DefaultAzureCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential), which tries multiple auth methods in order. Choose the one that fits your scenario:

### Option 1: Azure CLI (recommended for local dev)

```bash
# Log in to your own tenant
az login

# Verify which subscription is active
az account show --query "{subscription: id, tenant: tenantId}" -o table

# If needed, switch to a different subscription
az account set --subscription "YOUR-SUBSCRIPTION-ID"
```

### Option 2: Service principal (recommended for CI/CD and production)

Set these environment variables in `.env`:

```bash
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret
```

### Option 3: Managed Identity (recommended for Azure-hosted deployments)

If running on Azure (VM, App Service, Function App), assign a managed identity and grant it the required RBAC role.

**System-assigned identity:** No environment variables needed — `DefaultAzureCredential` picks it up automatically.

**User-assigned identity:** Set `AZURE_MANAGED_IDENTITY_CLIENT_ID` to the Client ID of the identity in your Function App's Application Settings (or `.env` for local development). This is required because `DefaultAzureCredential` cannot auto-discover user-assigned identities.

```bash
# Find the client ID of your user-assigned identity
az identity show --name YOUR-IDENTITY-NAME --resource-group YOUR-RG --query clientId -o tsv
```

### Multi-tenant / multi-subscription usage

Your Azure CLI login must be in the **same tenant** that owns the subscription. If you need to check capacity in a subscription belonging to a different tenant, log in to that tenant first:

```bash
az login --tenant "TARGET-TENANT-ID"
```

You can also override the subscription on a per-request basis without changing your `.env` file — see [Targeting a specific subscription](#targeting-a-specific-subscription) below.

---

## Quick start — local development

```bash
# 1. Clone / copy the project
cd azure-capacity-checker

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Configure environment variables
copy .env.example .env
# Edit .env and set AZURE_SUBSCRIPTION_ID
# Or skip this — the API auto-discovers your subscription from Azure CLI

# 5. Authenticate
az login

# 6. Start the API
uvicorn main:app --reload --port 8000
```

Open the interactive Swagger docs at **http://localhost:8000/docs**.

---

## Quick start — Azure Function App

The same codebase runs as an Azure Function App with zero code changes. The `function_app.py` wrapper maps the FastAPI ASGI app to an HTTP trigger.

### Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local) v4+
- Python 3.11+
- An Azure subscription with a Function App (Premium or Dedicated plan recommended)

### Local testing with Azure Functions Core Tools

```bash
cd azure-capacity-checker

# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 2. Install dependencies (includes azure-functions)
pip install -r requirements.txt

# 3. Configure local.settings.json
#    Set AZURE_SUBSCRIPTION_ID or leave blank for auto-discovery

# 4. Start the Function App locally (must run inside the activated venv)
func start
```

The API is available at **http://localhost:7071/api/v1/check** (and all other routes).

> **Note:** `func start` must run inside the activated virtual environment so it picks up the installed dependencies. The `routePrefix` is set to `""` in `host.json` so routes match between local uvicorn and Azure Function modes.

### Deploy to Azure

```bash
# 1. Create a Function App (Premium plan recommended for 10-min timeout)
az functionapp create \
  --resource-group YOUR-RG \
  --consumption-plan-location eastus \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --name YOUR-FUNCTION-APP-NAME \
  --storage-account YOUR-STORAGE-ACCOUNT \
  --os-type Linux

# 2. Enable system-assigned managed identity
az functionapp identity assign \
  --resource-group YOUR-RG \
  --name YOUR-FUNCTION-APP-NAME

# 3. Grant the managed identity Contributor on the probe resource group ONLY
#    (least-privilege — do NOT grant on the full subscription)
PRINCIPAL_ID=$(az functionapp identity show \
  --resource-group YOUR-RG \
  --name YOUR-FUNCTION-APP-NAME \
  --query principalId -o tsv)

az role assignment create \
  --assignee $PRINCIPAL_ID \
  --role Contributor \
  --scope /subscriptions/YOUR-SUB-ID/resourceGroups/az-cap-probe-rg

# 4. Configure application settings
az functionapp config appsettings set \
  --resource-group YOUR-RG \
  --name YOUR-FUNCTION-APP-NAME \
  --settings \
    AZURE_SUBSCRIPTION_ID=YOUR-SUB-ID \
    AZURE_PROBE_RESOURCE_GROUP=az-cap-probe-rg \
    BATCH_CONCURRENCY=3

# 5. Deploy the code
func azure functionapp publish YOUR-FUNCTION-APP-NAME

# 6. Enable Entra ID authentication (REQUIRED — see next section)
```

> ⚠️ **IMPORTANT:** The Function App is **not secure** until you complete the
> [Securing the API with Entra ID](#securing-the-api-with-microsoft-entra-id-required)
> setup below. Do not skip this step.

---

### Securing the API with Microsoft Entra ID (REQUIRED)

The Azure Function App **must** be protected with Microsoft Entra ID (formerly Azure AD) authentication. This ensures that only authorized users in your organization can call the API. Unauthenticated requests are rejected by the platform before they reach your code.

#### Step 1: Register an App and Expose an API

```bash
# Create the app registration
az ad app create \
  --display-name "Azure Capacity Checker API" \
  --sign-in-audience AzureADMyOrg

# Note the appId from the output — you'll need it below
APP_ID=<appId-from-output>

# Create a service principal for the app
az ad sp create --id $APP_ID

# Set the Application ID URI (required for token requests)
az ad app update --id $APP_ID --identifier-uris "api://$APP_ID"

# Add a user_impersonation scope so clients can request tokens
az ad app update --id $APP_ID --set api='{"oauth2PermissionScopes":[{"adminConsentDescription":"Access Azure Capacity Checker API","adminConsentDisplayName":"Access API","id":"00000000-0000-0000-0000-000000000001","isEnabled":true,"type":"User","userConsentDescription":"Access Azure Capacity Checker API","userConsentDisplayName":"Access API","value":"user_impersonation"}]}'
```

Or via the Azure Portal:
1. Go to **Microsoft Entra ID → App registrations → New registration**
2. Name: `Azure Capacity Checker API`
3. Supported account types: **Accounts in this organizational directory only**
4. Click **Register**
5. Note the **Application (client) ID** and **Directory (tenant) ID**
6. Go to **Expose an API** → **Set** the Application ID URI (accept the default `api://YOUR-APP-ID`)
7. Click **+ Add a scope** → Scope name: `user_impersonation`, Who can consent: **Admins and users**, fill display names, click **Add scope**
8. Under **Authorized client applications**, click **+ Add a client application** → Client ID: `04b07795-8ddb-461a-bbee-02f9e1bf7b46` (Azure CLI), check the `user_impersonation` scope, click **Add**

#### Step 2: Enable Authentication on the Function App

The recommended approach is via the Azure Portal, which avoids common pitfalls with CLI-based configuration:

1. Go to your **Function App → Authentication**
2. Click **Add identity provider**
3. Select **Microsoft**
4. **App registration type**: Provide the details of an existing app registration
5. **Application (client) ID**: your app's client ID
6. **Issuer URL**: `https://sts.windows.net/YOUR-TENANT-ID/`
7. **Allowed token audiences**: add both `api://YOUR-APP-ID` and `YOUR-APP-ID`
8. **Client application requirement**: **Allow requests from any application** (required for Azure CLI access)
9. **Identity requirement**: **Allow requests from any identity**
10. **Unauthenticated requests**: Return HTTP 401
11. Click **Add**

> **Important:** The issuer URL must be `https://sts.windows.net/YOUR-TENANT-ID/` (without `/v2.0` suffix). Azure CLI issues v1 tokens, and a v2.0 issuer will cause `IDX10214: Audience validation failed` errors.
>
> **Important:** "Client application requirement" must be set to **Allow requests from any application**. The default "Allow requests only from this application itself" blocks Azure CLI and other clients from calling the API (they have a different `appid` in their tokens).

#### Step 3: Restrict access to specific users/groups (recommended)

By default, any user in your Entra ID tenant can authenticate. To restrict access to specific users or groups:

1. Go to **Microsoft Entra ID → Enterprise applications**
2. Find `Azure Capacity Checker API`
3. Go to **Properties** → set **Assignment required?** to **Yes**
4. Go to **Users and groups** → **Add user/group**
5. Select the users or security groups that should have access

#### Step 4: First-time consent and token acquisition

The first time a user calls the API via Azure CLI, they must consent to the app. Run this once:

```bash
az logout
az login --tenant "YOUR-TENANT-ID" --scope "api://YOUR-APP-ID/.default"
```

After consenting, acquire tokens normally:

**Using Azure CLI (simplest for testing):**
```bash
# Get a token for the API
TOKEN=$(az account get-access-token \
  --resource "api://YOUR-APP-ID" \
  --query accessToken -o tsv)

# Call the API with the token
curl -H "Authorization: Bearer $TOKEN" \
  "https://YOUR-FUNCTION-APP-NAME.azurewebsites.net/api/v1/check?vm_size=Standard_D4s_v3&region=eastus"
```

**Using PowerShell:**
```powershell
$token = az account get-access-token --resource "api://YOUR-APP-ID" --query accessToken -o tsv

Invoke-RestMethod `
  -Uri "https://YOUR-FUNCTION-APP-NAME.azurewebsites.net/api/v1/check?vm_size=Standard_D4s_v3&region=eastus" `
  -Headers @{ Authorization = "Bearer $token" }
```

> **Note:** Always use `https://` when calling the Azure-hosted API. HTTP requests bypass Easy Auth and will be rejected.

**From application code (service-to-service):**
Use the [client credentials flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow) with a client secret or certificate. Create a client secret in the app registration and use it to acquire a token for the `api://YOUR-APP-ID` audience.

#### Authentication summary

| Layer | What it does |
|---|---|
| **Entra ID (Easy Auth)** | Platform-level — rejects unauthenticated requests with 401 before they reach your code |
| **Assignment required** | Restricts which users/groups in your tenant can authenticate |
| **Managed Identity RBAC** | Scoped to the probe resource group only — limits what the Function App can do in Azure, even if compromised |

#### Authentication flow

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────────┐
│  Client (CLI,   │     │   Microsoft      │     │  Azure Function App     │
│  PowerShell,    │     │   Entra ID       │     │  + Easy Auth            │
│  custom app)    │     │   (Token Issuer) │     │                         │
└────────┬────────┘     └─────────┬────────┘     └────────────┬────────────┘
         │                       │                            │
    1. Request token             │                            │
    (az account get-access-token)│                            │
         │                       │                            │
         └──────────────────────►│                            │
                          2. Validate user,                   │
                          issue JWT token                     │
                          (aud: api://APP-ID)                 │
         ┌───────────────────────┘                            │
         │                                                    │
    3. Call API with                                          │
    Authorization: Bearer <token>                             │
         │                                                    │
         └───────────────────────────────────────────────────►│
                                              4. Easy Auth (platform layer)
                                                 validates JWT:
                                                 ─ Issuer matches tenant?
                                                 ─ Audience in allowed list?
                                                 ─ Token not expired?
                                                 ─ User assigned? (if required)
                                                          │
                                                 ┌────────┴────────┐
                                                 │                 │
                                            Valid │            Invalid │
                                                 │                 │
                                                 ▼                 ▼
                                          5. Request         Return 401/403
                                          forwarded to      (never reaches
                                          FastAPI app        your code)
                                                 │
                                                 ▼
                                          6. FastAPI uses
                                          Managed Identity
                                          to call Azure APIs
                                          (SKU, Quota, ODCR)
```

**Key points:**
- Easy Auth runs at the **platform level** — invalid requests are rejected before they reach your Python code
- The API itself never handles tokens, validates JWTs, or manages authentication logic
- Two separate identities are in play: the **user's Entra ID token** (for calling the API) and the **Function App's managed identity** (for calling Azure Resource Manager APIs)

> **Note:** Entra ID authentication is enforced at the Azure platform level.
> Local development (`uvicorn` / `python run.py`) does **not** require
> authentication — it runs unauthenticated for convenience. If you need to
> test auth locally, use the [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
> with Easy Auth emulation, or add a custom middleware to `main.py`.

### Azure Function considerations

| Topic | Detail |
|---|---|
| **Timeout** | ODCR probes take 30–90s each. Consumption plan has a 5-min max; **Premium or Dedicated plan** recommended for batch requests (10-min timeout configured in `host.json`). |
| **Authentication** | **REQUIRED.** Microsoft Entra ID (Easy Auth) must be enabled. See [Securing the API with Entra ID](#securing-the-api-with-microsoft-entra-id-required) above. |
| **Managed Identity** | System-assigned or user-assigned. For user-assigned, set `AZURE_MANAGED_IDENTITY_CLIENT_ID`. Scope RBAC to the **probe resource group only** — not the subscription. |
| **Cold starts** | Azure SDK imports take ~2-3s. Use Premium plan with "always ready" instances to avoid cold start latency. |
| **Concurrency** | Each Function instance is a single Python worker. `BATCH_CONCURRENCY=3` is safe. Avoid very large batch sizes on Consumption plan. |
| **Auto-discovery** | Azure CLI is not available in Azure Functions. Set `AZURE_SUBSCRIPTION_ID` in Application Settings, or the API will use the Managed Identity to discover subscriptions. |
| **Cleanup safety** | If a Function times out mid-probe, orphaned resources may remain. Consider adding a **timer-triggered Function** that calls `POST /api/v1/cleanup` every 5–10 minutes. |

### Project structure (with Azure Function files)

```
azure-capacity-checker/
├── main.py                 # FastAPI application (shared by both modes)
├── capacity_checker.py     # ODCR probe + prerequisite checks
├── function_app.py         # Azure Function ASGI wrapper
├── host.json               # Azure Function host configuration
├── local.settings.json     # Azure Function local settings (not deployed)
├── requirements.txt        # All dependencies (includes azure-functions)
├── requirements-azure.txt  # Alternate requirements file (same packages)
├── run.py                  # Local dev launcher (uvicorn)
├── .env.example            # Environment variable template
├── test_endpoints.py       # Unit tests (mocked, no Azure calls)
├── test_integration.py     # Integration tests (real Azure calls)
└── README.md
```

---

## Targeting a specific subscription

You can set the default subscription in `.env`:

```bash
AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000
```

Or override it **per-request** by adding `subscription_id` as a query parameter to any endpoint:

```
GET /api/v1/check?vm_size=Standard_D4s_v3&region=eastus&subscription_id=YOUR-SUB-ID
```

This lets multiple users or teams share the same API server while checking capacity against their own subscriptions.

---

## API reference

All capacity endpoints support these **common query parameters**:

| Parameter | Type | Required | Description |
|---|---|---|---|
| `subscription_id` | string | ❌ | Override the default Azure subscription |
| `report` | bool | ❌ | When `true`, returns a concise plain-text report instead of JSON |

### `GET /health`

Returns service health and configuration status.

```json
{
  "status": "healthy",
  "runtime": "local",
  "subscription_configured": true,
  "subscription_source": "environment",
  "probe_resource_group": "az-cap-probe-rg"
}
```

---

### `GET /api/v1/check` — full capacity check ⭐ recommended

Runs all prerequisite checks + ODCR probe and returns a confidence score.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `vm_size` | string | ✅ | Azure VM SKU, e.g. `Standard_D4s_v3` |
| `region` | string | ✅ | Azure region slug, e.g. `eastus` |
| `zone` | string | ❌ | Availability zone (`1`, `2`, or `3`). Omit for regional check. |
| `quantity` | int | ❌ | Number of VM instances to test (default: 1, max: 100) |

**Example — curl**
```bash
curl "http://localhost:8000/api/v1/check?vm_size=Standard_D4s_v3&region=eastus&quantity=5"
```

**Example — PowerShell**
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/check?vm_size=Standard_D4s_v3&region=eastus&quantity=5"
```

**Example — plain-text report**
```bash
curl "http://localhost:8000/api/v1/check?vm_size=Standard_D4s_v3&region=eastus&report=true"
```

**Example response — capacity available (high confidence)**
```json
{
  "vm_size": "Standard_D4s_v3",
  "region": "eastus",
  "zone": null,
  "quantity": 1,
  "sku_check": {
    "vm_size": "Standard_D4s_v3",
    "region": "eastus",
    "available": true,
    "capacity_reservation_supported": true,
    "restrictions": [],
    "message": "SKU available"
  },
  "quota_check": {
    "family": "standardDSv3Family",
    "region": "eastus",
    "current_usage": 12,
    "limit": 100,
    "vcpus_needed": 4,
    "sufficient": true,
    "message": "Quota: 12/100 vCPUs used, need 4"
  },
  "capacity_available": true,
  "capacity_message": "Capacity is available for Standard_D4s_v3 in eastus",
  "capacity_error_code": null,
  "confidence_score": 100,
  "signal_level": "High",
  "summary": "Standard_D4s_v3 in eastus: SKU available | Quota OK | ODCR PASS → High confidence (100/100)",
  "disclaimer": "Capacity checks are point-in-time signals only. Azure capacity is dynamic and can change at any moment. Results do NOT guarantee that capacity will be available when you deploy. Use this API for directional guidance only."
}
```

---

### `POST /api/v1/check/batch` — full capacity check (batch) ⭐

Check multiple VM SKUs / regions / zones / quantities in a single call. Probes run concurrently.

**Example — PowerShell**
```powershell
$body = @{
    checks = @(
        @{ vm_size = "Standard_D4s_v3"; region = "eastus"; quantity = 5 },
        @{ vm_size = "Standard_D8s_v5"; region = "westus2"; zone = "1"; quantity = 2 },
        @{ vm_size = "Standard_E16as_v5"; region = "centralus"; quantity = 1 }
    )
} | ConvertTo-Json -Depth 3

# JSON response
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/check/batch" -Method POST -Body $body -ContentType "application/json"

# Plain-text report
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/check/batch?report=true" -Method POST -Body $body -ContentType "application/json"
```

**Example — curl**
```bash
curl -X POST "http://localhost:8000/api/v1/check/batch" \
  -H "Content-Type: application/json" \
  -d '{
    "checks": [
      { "vm_size": "Standard_D4s_v3", "region": "eastus", "quantity": 5 },
      { "vm_size": "Standard_D8s_v5", "region": "westus2", "zone": "1", "quantity": 2 }
    ]
  }'
```

**Request body schema**

| Field | Type | Required | Description |
|---|---|---|---|
| `checks` | array | ✅ | 1–20 items to check |
| `checks[].vm_size` | string | ✅ | Azure VM SKU name |
| `checks[].region` | string | ✅ | Azure region slug |
| `checks[].zone` | string | ❌ | Availability zone (1, 2, or 3) |
| `checks[].quantity` | int | ❌ | Number of instances (default: 1, max: 100) |

**Plain-text report output**
```
Azure Capacity Check Report
===========================

  [PASS] Standard_D4s_v3 x5 in eastus
         Signal: High (100/100)
         SKU: OK | ODCR: Supported
         Quota: Standard DSv3 Family vCPUs — 12/100 used

  [FAIL] Standard_D8s_v5 x2 in westus2 zone 1
         Signal: Low (20/100)
         SKU: OK | ODCR: Supported
         Quota: Standard DSv5 Family vCPUs — 0/10 used
         ** Quota insufficient: need 16 vCPUs
         Error: CapacityNotAvailable

  1/2 passed

  * Capacity checks are point-in-time signals only. Azure capacity is dynamic...
```

---

### `GET /api/v1/check-sku` — SKU availability only

Read-only check with no cost. Validates the SKU exists, has no restrictions, and supports Capacity Reservations. **Does not create any Azure resources.**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `vm_size` | string | ✅ | Azure VM SKU |
| `region` | string | ✅ | Azure region slug |

```bash
curl "http://localhost:8000/api/v1/check-sku?vm_size=Standard_D4s_v3&region=eastus"
```

---

### `GET /api/v1/check-quota` — vCPU quota only

Read-only check with no cost. Returns vCPU usage vs limit for the VM family. **Does not create any Azure resources.**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `vm_size` | string | ✅ | Azure VM SKU |
| `region` | string | ✅ | Azure region slug |

```bash
curl "http://localhost:8000/api/v1/check-quota?vm_size=Standard_D4s_v3&region=eastus"
```

---

### `GET /api/v1/check-capacity` — ODCR probe only (legacy)

Direct ODCR probe without prerequisite checks. Use `/api/v1/check` instead for the full experience.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `vm_size` | string | ✅ | Azure VM SKU |
| `region` | string | ✅ | Azure region slug |
| `zone` | string | ❌ | Availability zone (1, 2, or 3) |

---

### `POST /api/v1/check-capacity/batch` — ODCR probe batch (legacy)

Check up to 20 combinations. Use `/api/v1/check/batch` instead for the full experience.

---

### `POST /api/v1/cleanup` — clean up orphaned probe resources

Scans the probe resource group and deletes any leftover `cap-probe-*` Capacity Reservation Groups and their Capacity Reservations. Runs automatically after every batch request, but can also be called manually.

```bash
curl -X POST "http://localhost:8000/api/v1/cleanup?subscription_id=YOUR-SUB-ID"
```

```json
{ "cleaned": 2, "message": "Removed 2 orphaned probe resource(s)" }
```

---

## Confidence score

The confidence score (0–100) combines three signals:

| Signal | Points | Description |
|---|---|---|
| SKU available | 20 | SKU exists in region with no restrictions |
| ODCR supported | 5 | SKU supports On-Demand Capacity Reservations |
| Quota sufficient | 15 | vCPU quota headroom ≥ vCPUs needed |
| ODCR probe succeeded | 60 | Azure accepted the ephemeral Capacity Reservation |

| Score Range | Signal Level | Meaning |
|---|---|---|
| 90–100 | **High** | All signals positive — strong indication of availability |
| 60–89 | **Medium** | Most signals positive, some concerns |
| 20–59 | **Low** | Significant concerns — capacity may not be available |
| 0–19 | **None** | Capacity very unlikely to be available |

### Cross-subscription capacity signals

The ODCR probe tests **physical hardware availability** in a region. Since Azure's compute hardware pool is shared across all subscriptions, a successful probe on one subscription is a strong directional signal that the same hardware is available for other subscriptions in the same region.

**What this means in practice:** If you run this tool against your own subscription and get a High confidence score for `Standard_D4as_v4` in `southcentralus`, you can reasonably tell a customer that physical capacity looks good in that region for that VM size — as long as:

| Condition | Why it matters |
|---|---|
| Same VM size and region/zone | The hardware pool is specific to the SKU and location |
| Similar deployment size | The probe tests 1 VM's worth; a customer deploying 100 may exhaust available capacity |
| Reasonable time window | Capacity is dynamic — a probe from hours ago may be stale |
| No subscription-level restrictions | The customer's subscription must not have the SKU restricted (`NotAvailableForSubscription`) |
| Sufficient quota | The customer needs enough vCPU quota for the VM family |

**What you can say:**
> "Our probes show capacity signals are currently strong for Standard_D4as_v4 in southcentralus. This indicates physical hardware is available in the region right now. We recommend verifying quota and SKU restrictions on your own subscription before deploying."

**What you cannot say:**
> "Capacity is guaranteed for your deployment in this region."

> **Tip:** For the most accurate cross-subscription signal, focus on the **ODCR probe result** (`capacity_available`) rather than the overall confidence score. The SKU and quota checks are subscription-specific, but the ODCR probe tests the shared hardware pool.

---

## Resource group lifecycle

The API uses a single **probe resource group** to hold the ephemeral Capacity Reservation resources it creates during probes.

| Aspect | Detail |
|---|---|
| **Name** | Set via `AZURE_PROBE_RESOURCE_GROUP` env var (default: `az-cap-probe-rg`) |
| **Created automatically** | On the first ODCR probe request, `ensure_resource_group()` calls `create_or_update` to create the RG if it doesn't exist |
| **Location** | Created in the region of the first probe. Subsequent probes in other regions reuse the same RG — Azure allows Capacity Reservation resources in any region regardless of the RG's location |
| **Reused across probes** | Once created, the RG persists and is reused by all future probes. It is never deleted by the API. |
| **Should be empty** | Between probes the RG should contain no resources. The `cap-probe-crg-*` / `cap-probe-cr-*` resources are created and deleted within each probe request. |
| **Read-only endpoints** | `check-sku` and `check-quota` do **not** create or touch the resource group — only the ODCR probe endpoints do |
| **Orphan cleanup** | If a probe fails mid-cleanup, the post-batch sweep and the `POST /api/v1/cleanup` endpoint will find and remove any leftover `cap-probe-*` resources |

> **Tip:** If you pre-create the resource group (e.g. via Terraform or `az group create`), the API will detect it and skip creation. Remove the `Microsoft.Resources/resourceGroups/write` RBAC action in that case.

---

## Cleanup and orphaned resources

The API uses three layers of protection to ensure probe resources are always cleaned up:

1. **Per-probe cleanup** — every probe runs in a `try/finally` block that deletes the CR and CRG. Cleanup errors are logged but swallowed to avoid masking the probe result.
2. **Post-batch sweep** — after every batch request, the API scans the probe resource group for any remaining `cap-probe-*` CRGs and deletes them.
3. **Manual cleanup** — call `POST /api/v1/cleanup` to trigger a sweep on demand.

If you suspect orphaned resources exist (e.g. after a process crash), you can also check manually:

```bash
az capacity reservation group list \
  --resource-group az-cap-probe-rg \
  --subscription YOUR-SUB-ID \
  -o table
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AZURE_SUBSCRIPTION_ID` | — | Target Azure subscription (optional if using auto-discovery via `az login` or per-request `subscription_id` parameter) |
| `AZURE_PROBE_RESOURCE_GROUP` | `az-cap-probe-rg` | Resource group for ephemeral probe resources |
| `AZURE_MANAGED_IDENTITY_CLIENT_ID` | — | Client ID of a user-assigned managed identity. Leave empty for system-assigned. |
| `AZURE_TENANT_ID` | — | Service-principal tenant ID |
| `AZURE_CLIENT_ID` | — | Service-principal client ID |
| `AZURE_CLIENT_SECRET` | — | Service-principal secret |
| `BATCH_CONCURRENCY` | `3` | Max parallel ODCR probes for `/batch` |

---

## Troubleshooting

### `InvalidAuthenticationTokenTenant` error

Your Azure CLI login is in a different tenant than the subscription you're targeting. Fix:

```bash
az logout
az login --tenant "CORRECT-TENANT-ID"
```

Or pass `subscription_id` for a subscription in your current tenant.

### `OperationNotAllowed` / quota errors

The subscription doesn't have enough vCPU quota for the requested VM family. The API reports this in the `quota_check` section. Request a quota increase in the Azure portal under **Subscriptions → Usage + quotas**.

### `SkuNotAvailable` without capacity probe running

The SKU check found that the VM size either doesn't exist in the target region or has restrictions (e.g. `NotAvailableForSubscription`). The ODCR probe is skipped to save time. Check SKU availability with:

```bash
az vm list-skus -l eastus --resource-type virtualMachines --query "[?name=='Standard_D4s_v3']" -o table
```

### Orphaned resources after a crash

Run the cleanup endpoint:

```bash
curl -X POST "http://localhost:8000/api/v1/cleanup?subscription_id=YOUR-SUB-ID"
```

### `AuthorizationFailed` when running on Azure Function App

The managed identity does not have Contributor on the probe resource group **in the subscription being targeted**. The RBAC role assignment must be in the same subscription as the `subscription_id` you're passing. Verify with:

```bash
az role assignment list --assignee YOUR-IDENTITY-OBJECT-ID --scope /subscriptions/YOUR-SUB-ID/resourceGroups/az-cap-probe-rg -o table
```

### `IDX10214: Audience validation failed` (401)

The token's audience doesn't match what Easy Auth expects. Ensure:
1. The **Issuer URL** on the Function App's auth config is `https://sts.windows.net/YOUR-TENANT-ID/` (no `/v2.0`)
2. **Allowed token audiences** includes both `api://YOUR-APP-ID` and `YOUR-APP-ID`
3. The app registration has an **Application ID URI** set (`api://YOUR-APP-ID`) under **Expose an API**

### `403 Forbidden` with a valid token

Common causes:
- **Client application requirement** is set to "Allow requests only from this application itself" — change to **Allow requests from any application** in the Function App's Authentication settings
- **Assignment required** is enabled but the user is not assigned — add the user under **Enterprise applications → Users and groups**, or set **Assignment required** to **No**
- Using `http://` instead of `https://` — Easy Auth only works over HTTPS

### `consent_required` / `AADSTS650057` when getting a token

The app registration is missing a scope. Go to **App registrations → Expose an API** and add a `user_impersonation` scope. Also add Azure CLI (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) as an authorized client application.

### Managed identity not found on Azure Function App

If using a **user-assigned** managed identity, set `AZURE_MANAGED_IDENTITY_CLIENT_ID` in the Function App's Application Settings to the identity's Client ID. `DefaultAzureCredential` cannot auto-discover user-assigned identities.

---

## Security notes

- **Entra ID authentication is required** when deploying to Azure. See [Securing the API with Entra ID](#securing-the-api-with-microsoft-entra-id-required). Do not expose the Function App without it.
- **Managed Identity RBAC must be scoped narrowly** — grant `Contributor` on the probe resource group only, never the full subscription. This limits blast radius even if the Function App is compromised.
- The service principal / identity used **only** needs rights to create/delete Capacity Reservation resources and read SKUs/usage (and optionally create the probe resource group). Grant least-privilege access; do **not** use Owner.
- Never commit `.env` or `local.settings.json` to source control — `.env.example` is the safe template.
- The `AZURE_CLIENT_SECRET` is read from the environment at runtime; it is never logged or returned by any endpoint.
- The `subscription_id` query parameter appears in request URLs and logs. Entra ID authentication ensures only authorized users can make requests.
