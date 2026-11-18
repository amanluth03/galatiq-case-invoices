"""Tests for the README's mock payment function used by the pay stage."""
from decimal import Decimal

from invoice_agent.utils.payments import mock_payment


def test_mock_payment_prints_and_succeeds(capsys):
    """mock_payment prints the README message and reports success."""
    assert mock_payment("Widgets Inc.", Decimal("5000.00")) == {"status": "success"}
    assert capsys.readouterr().out == "Paid 5000.00 to Widgets Inc.\n"
