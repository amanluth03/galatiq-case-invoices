"""The pay stage's payment call: the README's mock_payment, reached only after approval and the guardrail."""
from decimal import Decimal


def mock_payment(vendor: str, amount: Decimal) -> dict:
    """Pretend to pay the vendor (README behaviour): print the payment and report success."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
