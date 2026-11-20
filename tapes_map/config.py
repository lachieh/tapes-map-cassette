import re
from collections.abc import Mapping
from typing import Any

_DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
_DURATION_PART = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|ms|s|m|h)")
_TRUE = {"1", "t", "T", "TRUE", "true", "True"}
_FALSE = {"0", "f", "F", "FALSE", "false", "False"}


def env_name(key: str) -> str:
    return "CASSETTE_" + key.upper().replace(".", "_")


def parse_duration(raw: str) -> float:
    """Seconds in a Go duration string such as "1h", "90s" or "1h30m"."""
    if raw == "0":
        return 0.0
    parts = _DURATION_PART.findall(raw)
    if not parts or "".join(number + unit for number, unit in parts) != raw:
        raise ValueError(f"not a duration: {raw!r}")
    return sum(float(number) * _DURATION_UNITS[unit] for number, unit in parts)


def _parse(kind: str, raw: Any) -> Any:
    if kind == "string":
        return str(raw)
    if kind == "duration":
        return parse_duration(raw)
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
        raise ValueError(f"not a bool: {raw!r}")
    raise ValueError(f"unsupported config type {kind!r}")


def load_settings(config: list[dict[str, Any]], env: Mapping[str, str]) -> dict[str, Any]:
    """Every manifest [[config]] key, read from its CASSETTE_* variable or its default."""
    settings = {}
    for entry in config:
        name = env_name(entry["key"])
        raw = env.get(name, "").strip()
        if raw == "" and "default" not in entry:
            raise ValueError(f"{name} is required")
        try:
            settings[entry["key"]] = _parse(entry["type"], raw if raw else entry["default"])
        except ValueError as exc:
            raise ValueError(f"invalid {name}: {exc}") from None
    if settings["refresh.interval"] <= 0:
        raise ValueError(f"{env_name('refresh.interval')} must be positive")
    return settings
