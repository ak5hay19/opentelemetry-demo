"""Estimated model pricing used for comparing investigation runs."""

INPUT_USD_PER_M = 0.10
OUTPUT_USD_PER_M = 0.50


def estimate_cost(tokens_input: int, tokens_output: int) -> float:
    """Estimate USD cost from token counts; these are estimates from a third-party price source."""
    if tokens_input < 0 or tokens_output < 0:
        raise ValueError("token counts must be non-negative")
    return tokens_input * INPUT_USD_PER_M / 1_000_000 + tokens_output * OUTPUT_USD_PER_M / 1_000_000
