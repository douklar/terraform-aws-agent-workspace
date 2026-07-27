"""Shared test setup for the Lambda handlers.

The handlers read config and build boto3 clients at import time, so the
environment must be set before any test module imports them.
"""

import os
import sys

# MANAGER_TAG_VALUE has no default in the handlers, so supply one here.
os.environ.setdefault("MANAGER_TAG_VALUE", "workspace-scheduler-test")
# boto3.client(...) needs a region even though no test makes a real API call.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

# ``lambda/`` is a Python keyword, so it cannot be imported as a package.
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
