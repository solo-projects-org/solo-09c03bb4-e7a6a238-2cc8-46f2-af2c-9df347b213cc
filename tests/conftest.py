"""Test bootstrap: keep the module-level app from creating ./data/lights.db."""

import os
import tempfile

os.environ.setdefault(
    "LIGHTS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="lights-tests-"), "collect.db")
)
