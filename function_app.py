"""
Azure Capacity Checker — Azure Function App entry-point.

This module wraps the FastAPI application as an Azure Function using the
ASGI integration.  The same FastAPI app (main.py) runs in both modes:
  - Local: uvicorn main:app --reload
  - Azure Function: this file maps the FastAPI ASGI app to an HTTP trigger

Deploy as a Python v2 Azure Function App.
"""
import azure.functions as func

from main import app

# Wrap the FastAPI ASGI app as an Azure Function HTTP trigger.
# All routes defined in main.py are automatically available.
app_function = func.AsFunctionApp(app=app, http_auth_level=func.AuthLevel.ANONYMOUS)
