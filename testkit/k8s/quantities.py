"""Kubernetes resource-quantity normalization.

Converts the various quantity spellings used in Kubernetes resource specs
(``2Gi``, ``2048Mi``, ``1024Ki``, ``500``, raw byte counts) into a single
integer unit (MiB). The function is intentionally small and dependency-free.
"""

from __future__ import annotations

import re

# Suffix -> multiplier that yields MiB.
# Binary suffixes are exact; decimal (M/G/T) are treated the Kubernetes way
# (1M == 1Mi for memory quantities, so we map them to the binary multiplier).
_SUFFIX_TO_MIB = {
    "Ki": 1 / 1024,
    "Mi": 1,
    "Gi": 1024,
    "Ti": 1024 * 1024,
    "K": 1 / 1024,
    "M": 1,
    "G": 1024,
    "T": 1024 * 1024,
    "Pi": 1024 * 1024 * 1024,
    "Ei": 1024 * 1024 * 1024 * 1024,
}

# A quantity string: optional number, optional suffix.
_QUANTITY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*$")


def to_mib(value: object) -> int:
    """Normalize a Kubernetes quantity to integer MiB.

    Parameters
    ----------
    value:
        A quantity string (``"2Gi"``, ``"2048Mi"``, ``"1024Ki"``), a raw byte
        integer (already in bytes), or ``None``.

    Returns
    -------
    int
        The quantity expressed in mebibytes, rounded down. ``None`` yields ``0``.
    """
    if value is None:
        return 0

    # Raw integer / float already denotes bytes (Kubernetes bare integers are
    # byte counts). Convert bytes -> MiB.
    if isinstance(value, (int, float)):
        return int(value // (1024 * 1024))

    text = str(value).strip()
    if not text:
        return 0

    match = _QUANTITY_RE.match(text)
    if match is None:
        # Unparseable: treat as 0 rather than raising, matching the
        # "returns 0 for None" defensive intent for unknown inputs.
        return 0

    number_str, suffix = match.group(1), match.group(2)
    number = float(number_str)
    if suffix == "":
        # Bare number == bytes.
        return int(number // (1024 * 1024))
    multiplier = _SUFFIX_TO_MIB.get(suffix)
    if multiplier is None:
        return 0
    return int(number * multiplier)
