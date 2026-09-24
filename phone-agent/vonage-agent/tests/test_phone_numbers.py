import pytest

from phone_numbers import NumberError, check_destination, mask, normalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("090-1234-5678", "+819012345678"),
        ("+81 90 1234 5678", "+819012345678"),
        ("0081901234567 8", "+819012345678"),
        ("819012345678", "+819012345678"),
        ("011-222-3333", "+81112223333"),  # Sapporo must not be blocked
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected
    assert check_destination(raw) == expected


@pytest.mark.parametrize("raw", ["110", "119", "0570-000-000", "0990-123-456", "abc"])
def test_blocked_or_invalid(raw):
    with pytest.raises(NumberError):
        check_destination(raw)


def test_country_and_allowlist():
    with pytest.raises(NumberError):
        check_destination("+12015550123")
    assert check_destination("+12015550123", ("1", "81")) == "+12015550123"
    with pytest.raises(NumberError):
        check_destination("09012345678", allowlist=("819000000000",))
    assert check_destination("09012345678", allowlist=("819012345678",)) == "+819012345678"


def test_mask():
    assert mask("+819012345678") == "…5678"
    assert mask("") == "-"
