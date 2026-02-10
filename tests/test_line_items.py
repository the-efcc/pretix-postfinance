from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from postfinancecheckout.models import LineItemType

from pretix_postfinance.payment import PostFinancePaymentProvider


def make_position(item_name: str, price: Decimal, count: int = 1, variation: str | None = None):
    """Create a mock cart position."""
    pos = MagicMock()
    pos.item = MagicMock()
    pos.item.name = item_name
    pos.item.pk = 1
    pos.price = price
    pos.total = price * count
    pos.count = count
    if variation:
        pos.variation = MagicMock()
        pos.variation.value = variation
    else:
        pos.variation = None
    return pos


def make_fee(value: Decimal, fee_type: str = "service", has_display: bool = True):
    """Create a mock fee."""
    fee = MagicMock()
    fee.value = value
    fee.fee_type = fee_type
    if has_display:
        fee.get_fee_type_display = MagicMock(return_value=fee_type.capitalize())
    else:
        del fee.get_fee_type_display
    return fee


@pytest.mark.django_db
class TestBuildLineItemsStandardCart:
    def test_single_item(self, event):
        prov = PostFinancePaymentProvider(event)

        position = make_position("Concert Ticket", Decimal("50.00"))
        cart = {"positions": [position], "fees": [], "total": Decimal("50.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert len(line_items) == 1
        assert line_items[0].name == "Concert Ticket"
        assert line_items[0].quantity == 1.0
        assert line_items[0].amount_including_tax == 50.0
        assert line_items[0].type == LineItemType.PRODUCT

    def test_multiple_items(self, event):
        prov = PostFinancePaymentProvider(event)

        positions = [
            make_position("Concert Ticket", Decimal("50.00")),
            make_position("VIP Upgrade", Decimal("30.00")),
            make_position("Merchandise", Decimal("20.00")),
        ]
        cart = {"positions": positions, "fees": [], "total": Decimal("100.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert len(line_items) == 3
        assert line_items[0].name == "Concert Ticket"
        assert line_items[1].name == "VIP Upgrade"
        assert line_items[2].name == "Merchandise"

    def test_item_with_variation(self, event):
        prov = PostFinancePaymentProvider(event)

        position = make_position("T-Shirt", Decimal("25.00"), variation="Large")
        cart = {"positions": [position], "fees": [], "total": Decimal("25.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert len(line_items) == 1
        assert line_items[0].name == "T-Shirt - Large"

    def test_cart_with_fees(self, event):
        prov = PostFinancePaymentProvider(event)

        position = make_position("Concert Ticket", Decimal("50.00"))
        fee = make_fee(Decimal("5.00"), "service")
        cart = {"positions": [position], "fees": [fee], "total": Decimal("55.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert len(line_items) == 2
        assert line_items[0].name == "Concert Ticket"
        assert line_items[0].type == LineItemType.PRODUCT
        assert line_items[1].name == "Service"
        assert line_items[1].type == LineItemType.FEE
        assert line_items[1].amount_including_tax == 5.0

    def test_empty_positions_fallback(self, event):
        prov = PostFinancePaymentProvider(event)

        cart = {"positions": [], "fees": [], "total": Decimal("100.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert len(line_items) == 1
        assert line_items[0].name == "Order Total"
        assert line_items[0].amount_including_tax == 100.0
        assert line_items[0].unique_id == "order-total"


@pytest.mark.django_db
class TestBuildLineItemsEdgeCases:
    def test_zero_value_fee_skipped(self, event):
        prov = PostFinancePaymentProvider(event)

        position = make_position("Concert Ticket", Decimal("50.00"))
        zero_fee = make_fee(Decimal("0.00"), "waived")
        nonzero_fee = make_fee(Decimal("5.00"), "service")
        cart = {
            "positions": [position],
            "fees": [zero_fee, nonzero_fee],
            "total": Decimal("55.00"),
        }

        line_items = prov._build_line_items(cart, "CHF")

        assert len(line_items) == 2
        assert line_items[0].name == "Concert Ticket"
        assert line_items[1].name == "Service"

    def test_grouped_positions_quantity(self, event):
        prov = PostFinancePaymentProvider(event)

        position = make_position("Concert Ticket", Decimal("50.00"), count=3)
        cart = {"positions": [position], "fees": [], "total": Decimal("150.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert len(line_items) == 1
        assert line_items[0].quantity == 3.0
        assert line_items[0].amount_including_tax == 150.0

    def test_position_with_total_attribute(self, event):
        prov = PostFinancePaymentProvider(event)

        pos = MagicMock()
        pos.item = MagicMock()
        pos.item.name = "Bundle"
        pos.item.pk = 1
        pos.price = Decimal("100.00")
        pos.total = Decimal("90.00")
        pos.count = 1
        pos.variation = None

        cart = {"positions": [pos], "fees": [], "total": Decimal("90.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert line_items[0].amount_including_tax == 90.0

    def test_position_fallback_to_price(self, event):
        prov = PostFinancePaymentProvider(event)

        pos = MagicMock()
        pos.item = MagicMock()
        pos.item.name = "Ticket"
        pos.item.pk = 1
        pos.price = Decimal("75.00")
        pos.count = 1
        pos.variation = None
        del pos.total

        cart = {"positions": [pos], "fees": [], "total": Decimal("75.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert line_items[0].amount_including_tax == 75.0

    def test_fee_without_display_method(self, event):
        prov = PostFinancePaymentProvider(event)

        position = make_position("Ticket", Decimal("50.00"))
        fee = make_fee(Decimal("5.00"), "shipping", has_display=False)
        cart = {"positions": [position], "fees": [fee], "total": Decimal("55.00")}

        line_items = prov._build_line_items(cart, "CHF")

        assert len(line_items) == 2
        assert line_items[1].name == "shipping"

    def test_unique_ids_generated(self, event):
        prov = PostFinancePaymentProvider(event)

        positions = [
            make_position("Ticket A", Decimal("50.00")),
            make_position("Ticket B", Decimal("30.00")),
        ]
        fees = [
            make_fee(Decimal("5.00"), "fee1"),
            make_fee(Decimal("3.00"), "fee2"),
        ]
        cart = {"positions": positions, "fees": fees, "total": Decimal("88.00")}

        line_items = prov._build_line_items(cart, "CHF")

        unique_ids = [item.unique_id for item in line_items]
        assert len(unique_ids) == len(set(unique_ids))
        assert unique_ids[0].startswith("position-")
        assert unique_ids[1].startswith("position-")
        assert unique_ids[2].startswith("fee-")
        assert unique_ids[3].startswith("fee-")
