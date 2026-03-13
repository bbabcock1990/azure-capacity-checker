"""
Azure Capacity Checker — Azure Function App entry-point.

This module wraps the FastAPI application as an Azure Function using the
ASGI integration.  The same FastAPI app (main.py) runs in all modes:
  - Local uvicorn: uvicorn main:app --reload  (or python run.py)
  - Local func:    func start  (uses this file)
  - Azure Function: deployed via this file

Deploy as a Python v2 Azure Function App.
"""
import azure.functions as func

from main import app

# Wrap the FastAPI ASGI app as an Azure Function HTTP trigger.
# All routes defined in main.py are automatically available.
app_function = func.AsgiFunctionApp(app=app, http_auth_level=func.AuthLevel.ANONYMOUS)
