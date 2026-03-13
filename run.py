"""
Development launcher — starts uvicorn with the .venv directory excluded
from the file watcher to prevent spurious reloads.

Usage:
    python run.py
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_excludes=[".venv", "__pycache__", "*.pyc"],
    )
