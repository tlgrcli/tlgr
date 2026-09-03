"""The layer escape hatch serialises correctly (§6.14).

The example is `help.getNearestDc`, a method Telethon already supports, so
the recipe can be checked against the real constructor ids without waiting
for a layer bump. If these assertions ever fail, the recipe is wrong and
every hand-written request built from it would be too.
"""

from __future__ import annotations

import doctest

from telethon.tl.functions.help import GetNearestDcRequest as RealRequest
from telethon.tl.types import NearestDc as RealType

from tlgr.core import custom_tl


def test_the_example_matches_telethons_own_constructor_ids():
    assert custom_tl.GetNearestDcRequest.CONSTRUCTOR_ID == RealRequest.CONSTRUCTOR_ID
    assert custom_tl.NearestDc.CONSTRUCTOR_ID == RealType.CONSTRUCTOR_ID


def test_subclass_of_is_the_crc_of_the_result_type():
    assert custom_tl.subclass_of("NearestDc") == RealRequest.SUBCLASS_OF_ID


def test_the_request_serialises_to_the_same_bytes():
    assert bytes(custom_tl.GetNearestDcRequest()) == bytes(RealRequest())


def test_the_example_does_not_hijack_telethons_own_parser():
    """Registering a type Telethon already knows would break every real call."""
    from telethon.tl import alltlobjects

    assert alltlobjects.tlobjects[RealType.CONSTRUCTOR_ID] is RealType


def test_docstrings_run():
    results = doctest.testmod(custom_tl)
    assert results.failed == 0
