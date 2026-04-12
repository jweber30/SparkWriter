"""Crostini/ChromeOS specific notification helpers."""

import os
from functools import lru_cache

@lru_cache(maxsize=1)
def is_running_in_crostini() -> bool:
    """
    Check if the application is running inside the Crostini container on ChromeOS.
    This is typically done by checking for the presence of specific environment variables
    or files that are unique to the Crostini environment.
    """
    # A common way to detect Crostini is the 'CROS_CONTAINER_VERSION' env var.
    return "CROS_CONTAINER_VERSION" in os.environ

