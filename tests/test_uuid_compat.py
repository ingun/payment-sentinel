"""uuid_utils yedeğinin RFC 9562 UUIDv7 üretmesi ve monoton olması."""

from src._uuid_compat import uuid7


def test_uuid7_version_variant_and_monotonic():
    ids = [uuid7() for _ in range(2000)]
    assert all(u.version == 7 and u.variant == "specified in RFC 4122" for u in ids)
    assert ids == sorted(ids, key=lambda u: u.int) and len(set(ids)) == len(ids)


def test_uuid7_with_explicit_timestamp():
    u = uuid7(1_700_000_000, 500_000_000)
    assert u.int >> 80 == 1_700_000_000_500
