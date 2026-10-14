def format_amount(amount: int, currency: str) -> str:
    """A payout amount for a person to read, for example `125.00 USD`."""
    # assumes a two-decimal currency. Multi-currency is out of scope.
    return f"{amount / 100:.2f} {currency.upper()}"
