"""Helpers shared by the Soniox platforms."""


def base_language(language: str) -> str:
    """Reduce a language tag such as `en-US` or `pt_BR` to its base code (`en`, `pt`)."""
    return language.lower().replace("_", "-").split("-", maxsplit=1)[0]
