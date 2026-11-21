from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.engine.orderbook import (
    canonical_book,
    displayed_quote_size,
    parse_orderbook,
    quote_liquidation,
)
from kalshi_edge_engine.engine.paper_sim import (
    OrderRequest,
    RealisticOrderSimulator,
    compute_simulator_diagnostics,
)
from kalshi_edge_engine.engine.signal import score_signal
from kalshi_edge_engine.replay import Mode, b1_orderbook
from kalshi_edge_engine.replay.execution import Intent, Queue, strict_liquidity_rejection


def fixed(*yes):
    return {"orderbook_fp": {"yes_dollars": list(yes), "no_dollars": [["0.55", "400"]]}}


def intent(key="i", ticker="A", limit=41):
    order = OrderRequest(key, ticker, "YES", limit, 1, 0, 5, 1, 60)
    return Intent({}, {"simulated_volume_per_poll": 50}, order, None)


@pytest.mark.parametrize("quantity", ["0", "0.25", "0.75", "1", "1.5", "100", "112263.52"])
def test_reported_depth_and_full_sale(quantity):
    b = canonical_book(fixed(["0.40", quantity]))
    n = Decimal(quantity)
    top = b.top("YES")
    if n == 0:
        assert top is None and b.ask("NO") is None
    else:
        assert top.price == 40 and top.quantity == n and top.capacity == int(n)
        assert b.ask("NO") == 60
    assert quote_liquidation(b, "YES", int(n) + 1, 1) is None
    if int(n):
        assert quote_liquidation(b, "YES", int(n), 1).bid_levels == ((40, int(n)),)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        [],
        "",
        "_1",
        " 1 ",
        "x",
        "NaN",
        Decimal("sNaN"),
        float("inf"),
        "-Infinity",
        "-0.01",
        "1e200",
        "0.000000001",
    ],
)
def test_invalid_quantities_do_not_create_liquidity(bad):
    b = canonical_book(fixed(["0.40", bad], ["0.39", "2"]))
    assert b.top("YES").price == 39 and "invalid_number" in b.issues


@pytest.mark.parametrize("bad", ["0", "1", "-0.0001", "1.0001", "0.00001", "NaN", False])
def test_invalid_prices(bad):
    assert canonical_book(fixed([bad, "1"])).top("YES") is None


def test_fixed_point_boundaries_precision_and_legacy_shapes():
    b = canonical_book(fixed(["0.9999", "9007199254740993.01000001"], ["0.0001", "1"]))
    assert b.yes[0].price == Decimal("99.99")
    assert b.yes[0].quantity == Decimal("9007199254740993.01000001")
    assert b.yes[0].capacity == 9007199254740993
    assert displayed_quote_size(b, "YES", 99.99) == "9007199254740993.01000001"
    assert b.yes[-1].price == b.ask("NO") == Decimal("0.01")
    legacy = {"yes": [[40.1, 2.75]], "no": [[50, 3]]}
    assert canonical_book(legacy) == canonical_book({"orderbook": legacy})
    assert canonical_book(legacy) == canonical_book(
        {"orderbook_fp": {"yes_dollars": [["0.4010", "2.75"]], "no_dollars": [["0.50", "3"]]}}
    )
    assert "missing_no" in canonical_book({"yes": []}).issues
    assert "invalid_array_no" in canonical_book({"yes": [], "no": None}).issues


def test_duplicates_and_subcontract_top_are_conservative():
    raw = fixed(["0.41", "0.75"], ["0.40", "7"], ["0.40", "1.75"], ["0.39", "0.75"])
    b = canonical_book(raw)
    assert b.issues == ("duplicate_price",)
    assert parse_orderbook(b).bid_yes == (41, 0)
    assert displayed_quote_size(b, "YES", 40) == 1.75
    q = quote_liquidation(b, "YES", 1, 3)
    assert q.best_bid_cents == 41 and q.bid_levels == ((40, 1),)
    assert quote_liquidation(b, "YES", 2, 3) is None
    assert canonical_book(fixed(["0.40", "6"], ["0.400", "0"])).yes == ()


def test_toronto_scanner_entry_liquidation_and_replay():
    raw = fixed(["0.3500", "3810.00"], ["0.4000", "112263.52"])
    i = intent(limit=35)
    old = Queue().observe(i, raw, Mode.B2)
    assert old["market_best_bid"] == 35 and old["poll_class"] == "at_displayed_quote"
    assert b1_orderbook.quote_liquidation(raw, "YES", 1, 0).best_bid_cents == 40
    signal = score_signal("A", raw)
    live = compute_simulator_diagnostics(i.order, raw, 0, None, 50)
    replay = Queue().observe(i, raw)
    exit_quote = quote_liquidation(raw, "YES", 112263, 0)
    assert (
        signal.bid_yes
        == live["market_best_bid"]
        == replay["market_best_bid"]
        == exit_quote.best_bid_cents
        == 40
    )
    assert (
        signal.liq_yes
        == live["best_size_at_bid"]
        == replay["consistency"]["best_capacity_contracts"]
        == 112263
    )
    assert Decimal(live["reported_best_size"]) == Decimal("112263.52")
    assert replay["poll_class"] == "moved_through" and replay["eligible_path"] != "queue_cleared"
    assert quote_liquidation(raw, "YES", 112264, 0).bid_levels == ((40, 112263), (35, 1))


@pytest.mark.parametrize("mode", [Mode.B1_QUEUE, Mode.CANONICAL])
def test_queue_initializes_depletes_resets_and_respects_missing_prefix(mode):
    i = intent()

    def observe(q, p, n):
        return q.observe(i, fixed([p, n]), mode)

    q = Queue()
    assert observe(q, "0.41", "2")["queue_transition"] == "initialize"
    assert observe(q, "0.41", "1")["eligible_path"] is None
    assert observe(q, "0.41", "8")["queue_ahead_after_poll"] == 1
    d = observe(q, "0.41", "1")
    assert (d["eligible_path"], d["capacity"], q.ahead) == ("queue_cleared", 6, 0)
    assert observe(q, "0.42", "4")["queue_transition"] == "reset"
    assert observe(q, "0.40", "4")["poll_class"] == "price_improving"
    assert observe(q, "0.41", "8")["queue_ahead_before_decrement"] == 8
    missing = Queue(missing_history=True)
    for n in ("2", "8", "1"):
        d = observe(missing, "0.41", n)
        assert not d["queue_state_known"] and d["eligible_path"] is None
    observe(missing, "0.40", "8")
    assert observe(missing, "0.41", "8")["queue_state_known"]
    with pytest.raises(ValueError, match="another intent"):
        q.observe(intent("j", "B"), fixed(["0.41", "1"]), mode)
    assert (
        Queue().observe(intent("j", "B"), fixed(["0.41", "3"]), mode)[
            "queue_ahead_before_decrement"
        ]
        == 3
    )


async def test_fractional_live_queue_and_replay_agree_poll_by_poll():
    raw = [fixed(["0.41", s]) for s in ("1.50", "1.01", "0.99", "5.75", "1.01")]
    i = intent()
    q = Queue()
    replay = [q.observe(i, b) for b in raw]
    assert [d["traded_volume_estimate"] for d in replay] == [0, 0, 0, 0, 4]
    assert [d["queue_ahead_after_poll"] for d in replay] == [2, 2, 2, 2, 0]
    assert replay[-1]["capacity"] == 2
    client = AsyncMock()
    client.get_orderbook.side_effect = raw
    observed = []

    def observe(event):
        observed.append(event["diagnostics"])
        return True

    simulator = RealisticOrderSimulator(poll_interval_seconds=0, fill_probability_at_best=1)
    fills, _ = await simulator.simulate_order_fills(
        client, i.order, horizon_seconds=1, on_poll=observe
    )
    assert [(f.fill_size, f.fill_path_type) for f in fills] == [(1, "queue_cleared")]
    for actual, modeled in zip(observed, replay, strict=True):
        for metric in (
            "poll_class",
            "market_best_bid",
            "queue_ahead_before_decrement",
            "queue_ahead_after_poll",
            "traded_volume_estimate",
        ):
            assert actual[metric] == modeled[metric]


def test_strict_gate_detects_equal_prices_with_different_quantity_or_deeper_capacity():
    i = intent(limit=42)
    numeric = {"yes": [[41, 2.75]], "no": [[55, 10]]}
    c = Queue().observe(i, numeric, Mode.B1_QUEUE)["consistency"]
    assert c["best_price_agrees"] and c["implied_ask_agrees"] and c["best_capacity_agrees"]
    assert c["model_best_quantity"] == "2" and c["raw_best_quantity"] == "2.75"
    assert strict_liquidity_rejection(c) == "liquidity_inconsistent"
    c = Queue().observe(i, fixed(["0.41", "5"], ["0.40", "1.75"]), Mode.B1_QUEUE)["consistency"]
    assert c["displayed_size_agrees"] and c["different_capacity_levels"] == 1
    assert strict_liquidity_rejection(c) == "liquidity_inconsistent"
    c = Queue().observe(i, {"yes": [[41, 10]]})["consistency"]
    assert (
        c["implied_ask_agrees"] is None
        and strict_liquidity_rejection(c) == "unknown_or_ambiguous_depth"
    )
    c = Queue().observe(i, fixed(["0.41", "0.75"]))["consistency"]
    assert strict_liquidity_rejection(c) == "subcontract_top"


def test_strict_queue_checks_entire_history():
    i = intent()
    q = Queue()
    # The first legacy numeric quantity truncates. Following quantities and both tops agree.
    for n in (2.75, 4, 6, 1):
        result = q.observe(i, {"yes": [[41, n]], "no": [[50, 10]]}, Mode.B1_QUEUE)
    c = result["consistency"]
    assert c["displayed_size_agrees"] and c["depth_capacity_agrees"]
    assert c["raw_queue_before"] == 3 and c["model_queue_before"] == 2
    assert strict_liquidity_rejection(c) == "unknown_or_inconsistent_queue_liquidity"


def test_strict_gate_detects_fractional_opposite_depth_with_identical_whole_capacity():
    raw = {"yes": [[41, 8]], "no": [[55, 10], [54, 3.75]]}
    c = Queue().observe(intent(limit=42), raw, Mode.B1_QUEUE)["consistency"]
    assert c["displayed_size_agrees"] and c["depth_capacity_agrees"]
    assert not c["depth_quantity_agrees"] and c["different_quantity_levels"] == 1
    assert strict_liquidity_rejection(c) == "liquidity_inconsistent"
