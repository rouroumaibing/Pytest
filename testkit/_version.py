"""Single source of truth for the package version.

``pyproject.toml`` reads this attribute dynamically (PEP 621 ``dynamic =
["version"]``) so the version is defined in exactly one place.
"""

__version__ = "0.1.0"
