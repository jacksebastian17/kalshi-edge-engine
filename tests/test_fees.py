"""Tests for Kalshi fee calculations."""

from kalshi_edge_engine.engine.fees import (
    estimate_fee_cents,
    estimate_maker_fee_cents,
    estimate_taker_fee_cents,
)


def test_taker_fee_75_cents():
    """Taker fee at P=0.75, C=1: ceil(0.07*0.75*0.25*100) = ceil(1.3125) = 2 cents."""
    price_cents = 75.0
    contracts = 1
    expected_fee = 2.0  # ceil(1.3125)

    result = estimate_taker_fee_cents(price_cents, contracts)
    assert result == expected_fee


def test_taker_fee_50_cents():
    """Taker fee at P=0.50, C=1: ceil(0.07*0.5*0.5*100) = ceil(1.75) = 2 cents."""
    price_cents = 50.0
    contracts = 1
    expected_fee = 2.0  # ceil(1.75)

    result = estimate_taker_fee_cents(price_cents, contracts)
    assert result == expected_fee


def test_taker_fee_1_cent():
    """Taker fee at P=0.01, C=1: ceil(0.07*0.01*0.99*100) = ceil(0.0693) = 1 cent."""
    price_cents = 1.0
    contracts = 1
    expected_fee = 1.0  # ceil(0.0693)

    result = estimate_taker_fee_cents(price_cents, contracts)
    assert result == expected_fee


def test_maker_fee_75_cents():
    """Maker fee at P=0.75, C=1: ceil(0.0175*0.75*0.25*100) = ceil(0.328125) = 1 cent."""
    price_cents = 75.0
    contracts = 1
    expected_fee = 1.0  # ceil(0.328125)

    result = estimate_maker_fee_cents(price_cents, contracts)
    assert result == expected_fee


def test_maker_fee_50_cents():
    """Maker fee at P=0.50, C=1: ceil(0.0175*0.5*0.5*100) = ceil(0.4375) = 1 cent."""
    price_cents = 50.0
    contracts = 1
    expected_fee = 1.0  # ceil(0.4375)

    result = estimate_maker_fee_cents(price_cents, contracts)
    assert result == expected_fee


def test_maker_fee_1_cent():
    """Maker fee at P=0.01, C=1: ceil(0.0175*0.01*0.99*100) = ceil(0.0173) = 1 cent."""
    price_cents = 1.0
    contracts = 1
    expected_fee = 1.0  # ceil(0.0173)

    result = estimate_maker_fee_cents(price_cents, contracts)
    assert result == expected_fee


def test_fee_with_multiple_contracts():
    """Fee scales linearly with contract count."""
    price_cents = 50.0
    contracts = 10

    # Single contract taker fee: ceil(0.07*0.5*0.5*100) = 2 cents
    # 10 contracts: ceil(0.07*10*0.5*0.5*100) = ceil(17.5) = 18 cents
    expected_fee = 18.0

    result = estimate_taker_fee_cents(price_cents, contracts)
    assert result == expected_fee


def test_fee_minimum_is_one_cent():
    """Fee should always be at least 1 cent even for tiny prices."""
    # Very small price (0.1 cents = $0.001)
    price_cents = 0.1
    contracts = 1

    taker_fee = estimate_taker_fee_cents(price_cents, contracts)
    maker_fee = estimate_maker_fee_cents(price_cents, contracts)

    assert taker_fee >= 1.0
    assert maker_fee >= 1.0


def test_estimate_fee_cents_direct():
    """Test the base function with custom rates."""
    price_cents = 50.0
    contracts = 1
    custom_rate = 0.05  # 5%

    # fee = ceil(0.05 * 1 * 0.5 * 0.5 * 100) = ceil(1.25) = 2 cents
    expected_fee = 2.0

    result = estimate_fee_cents(price_cents, contracts, custom_rate)
    assert result == expected_fee


def test_fee_at_extreme_prices():
    """Test fees at extreme prices (near 0 and near 100)."""
    # Near 0: P=0.02 => P*(1-P) = 0.02*0.98 = 0.0196
    # Taker: ceil(0.07 * 0.0196 * 100) = ceil(0.1372) = 1 cent
    result_low = estimate_taker_fee_cents(2.0, 1)
    assert result_low == 1.0

    # Near 100: P=0.98 => P*(1-P) = 0.98*0.02 = 0.0196
    # Taker: ceil(0.07 * 0.0196 * 100) = ceil(0.1372) = 1 cent
    result_high = estimate_taker_fee_cents(98.0, 1)
    assert result_high == 1.0


def test_fee_symmetry():
    """Fee should be symmetric around P=0.5 since formula uses P*(1-P)."""
    price_low = 30.0  # P=0.3
    price_high = 70.0  # P=0.7
    contracts = 1

    # P*(1-P) is the same for P=0.3 and P=0.7
    # 0.3 * 0.7 = 0.21
    fee_low = estimate_taker_fee_cents(price_low, contracts)
    fee_high = estimate_taker_fee_cents(price_high, contracts)

    assert fee_low == fee_high


def test_fee_zero_at_boundary_prices():
    """Fee should be exactly 0 at price 0 and price 100 (P*(1-P) = 0)."""
    # Price = 0 cents => P = 0.0 => P*(1-P) = 0
    taker_fee_0 = estimate_taker_fee_cents(0.0, 1)
    maker_fee_0 = estimate_maker_fee_cents(0.0, 1)
    assert taker_fee_0 == 0.0
    assert maker_fee_0 == 0.0

    # Price = 100 cents => P = 1.0 => P*(1-P) = 0
    taker_fee_100 = estimate_taker_fee_cents(100.0, 1)
    maker_fee_100 = estimate_maker_fee_cents(100.0, 1)
    assert taker_fee_100 == 0.0
    assert maker_fee_100 == 0.0


def test_taker_fee_exact_values():
    """Verify exact taker fee values at key price points."""
    # P=0.50, C=1: 0.07 * 1 * 0.5 * 0.5 = 0.0175 dollars = 1.75 cents
    # ceil(1.75) = 2 cents
    assert estimate_taker_fee_cents(50.0, 1) == 2.0

    # P=0.75, C=1: 0.07 * 1 * 0.75 * 0.25 = 0.013125 dollars = 1.3125 cents
    # ceil(1.3125) = 2 cents
    assert estimate_taker_fee_cents(75.0, 1) == 2.0
