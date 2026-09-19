"""Extended coverage tests for the Kubernetes quantity helper (quantities.py)."""

from __future__ import annotations

from testkit.k8s import to_mib


def test_full_to_mib_none():
    assert to_mib(None) == 0


def test_full_to_mib_int_bytes():
    # 1 MiB in raw bytes.
    assert to_mib(1048576) == 1


def test_full_to_mib_float_bytes():
    # 3 MiB expressed as a float byte count.
    assert to_mib(3145728.0) == 3


def test_full_to_mib_2gi():
    assert to_mib("2Gi") == 2048


def test_full_to_mib_512mi():
    assert to_mib("512Mi") == 512


def test_full_to_mib_1024ki():
    assert to_mib("1024Ki") == 1


def test_full_to_mib_1ti():
    assert to_mib("1Ti") == 1024 * 1024


def test_full_to_mib_decimal_m():
    # Decimal 'M' is mapped to the binary multiplier (1M == 1Mi).
    assert to_mib("1000M") == 1000


def test_full_to_mib_bare_small_rounds_to_zero():
    assert to_mib("500") == 0


def test_full_to_mib_bare_large():
    assert to_mib("2097152") == 2


def test_full_to_mib_1_5gi():
    assert to_mib("1.5Gi") == 1536


def test_full_to_mib_empty_string():
    assert to_mib("") == 0


def test_full_to_mib_unparseable():
    assert to_mib("abc") == 0


def test_full_to_mib_unknown_suffix():
    assert to_mib("2Xy") == 0
