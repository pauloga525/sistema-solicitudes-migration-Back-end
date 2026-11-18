import re

_SPECIAL = re.compile(r"[.*+?^${}()|\[\]\\]")


def escape_regexp(value: str) -> str:
    """Escapa caracteres especiales de regex (idéntico a common/utils/escape-regexp.ts)."""
    return _SPECIAL.sub(lambda m: "\\" + m.group(0), value)
