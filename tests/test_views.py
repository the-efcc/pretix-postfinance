"""
Tests for the PostFinance admin views.

Inspired by pretix's Stripe plugin test suite.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.utils.timezone import now
from pretix.base.models import Event, Order, Organizer, Team, User

from pretix_postfinance.api import PostFinanceError


@pytest.fixture
def env(client):
    """Create test environment with user, organizer, event, and order."""
    user = User.objects.create_user("dummy@dummy.dummy", "dummy")
    o = Organizer.objects.create(name="Dummy", slug="dummy")
    event = Event.objects.create(
        organizer=o,
        name="Dummy",
        slug="dummy",
        plugins="pretix_postfinance",
        date_from=now(),
        live=True,
    )
    event.settings.set("payment_postfinance_space_id", "12345")
    event.settings.set("payment_postfinance_user_id", "67890")
    event.settings.set("payment_postfinance_auth_key", "test-secret")

    event.settings.set("payment_postfinance__enabled", True)

    t = Team.objects.create(
        organizer=event.organizer,
        can_view_orders=True,
        can_change_orders=True,
        can_change_event_settings=True,
    )
    t.members.add(user)
    t.limit_events.add(event)

    order = Order.objects.create(
        code="FOOBAR",
        event=event,
        email="dummy@dummy.test",
        status=Order.STATUS_PENDING,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal("13.37"),
        sales_channel=o.sales_channels.get(identifier="web"),
    )

    client.force_login(user)

    return client, event, order


class TestTestConnectionView:
    """Tests for PostFinanceTestConnectionView."""

    @pytest.mark.django_db
    def test_connection_success(self, env, monkeypatch):
        """Test successful connection test."""
        client, event, order = env

        mock_space = MagicMock()
        mock_space.name = "Test Space"

        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinanceClient.get_space",
            lambda self: mock_space,
        )

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/test-connection/"
        response = client.post(url)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Test Space" in data["message"]

    @pytest.mark.django_db
    def test_connection_auth_error(self, env, monkeypatch):
        """Test connection test with authentication error."""
        client, event, order = env

        def get_space_error():
            raise PostFinanceError("Unauthorized", status_code=401)

        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinanceClient.get_space",
            lambda self: get_space_error(),
        )

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/test-connection/"
        response = client.post(url)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Authentication" in data["message"] or "failed" in data["message"].lower()

    @pytest.mark.django_db
    def test_connection_requires_login(self, env):
        """Test that connection test requires authentication."""
        client, event, order = env
        client.logout()

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/test-connection/"
        response = client.post(url)

        # Should redirect to login
        assert response.status_code in (302, 403)
