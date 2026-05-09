"""Shared pytest fixtures for miso-gallery tests.

Ensures required environment variables are set before any test module
imports the app (which requires SECRET_KEY at import time).
"""

import os

# Set a deterministic SECRET_KEY so app.py can import successfully
# without requiring it to be pre-set in the environment.
os.environ.setdefault("SECRET_KEY", "test-secret-for-ci")
