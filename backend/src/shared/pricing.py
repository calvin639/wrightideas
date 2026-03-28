"""
Pricing logic for Memories in Stone stone orders.

Prices in EUR (stored as cents for Stripe):
  1 stone  → €69.99
  2 stones → €89.99  (+€20.00)
  3 stones → €99.99  (+€10.00)
  4+       → €99.99 + €14.00 per extra stone beyond 3
"""

# Base prices in cents
PRICE_1 = 6999   # €69.99
PRICE_2 = 8999   # €89.99
PRICE_3 = 9999   # €99.99
PRICE_EXTRA = 1400  # €14.00 per stone beyond 3


def calculate_price_cents(quantity: int) -> int:
    """Return total price in euro cents for a given stone quantity."""
    if quantity < 1:
        raise ValueError("Quantity must be at least 1")
    if quantity == 1:
        return PRICE_1
    elif quantity == 2:
        return PRICE_2
    elif quantity == 3:
        return PRICE_3
    else:
        return PRICE_3 + (quantity - 3) * PRICE_EXTRA


def calculate_price_euros(quantity: int) -> float:
    """Return total price in euros (float) for display purposes."""
    return calculate_price_cents(quantity) / 100


def format_price(quantity: int) -> str:
    """Return a human-readable price string, e.g. '€99.99'"""
    return f"€{calculate_price_euros(quantity):.2f}"


def get_line_item_description(quantity: int) -> str:
    """Stripe line item description."""
    stone_label = "stone" if quantity == 1 else "stones"
    return f"Memories in Stone — {quantity} Black Slate {stone_label.title()}"


# Stone styles available (expand later)
STONE_STYLES = {
    "black_slate": {
        "label": "Black Slate",
        "available": True,
        "premium": False,
    },
    "marble": {
        "label": "Marble",
        "available": False,   # Coming soon
        "premium": True,
    },
    "wood": {
        "label": "Natural Wood",
        "available": False,   # Coming soon
        "premium": True,
    },
}
