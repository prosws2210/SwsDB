"""Names users choose, and how they map onto engine-level names and ids."""

from __future__ import annotations

import re
import uuid

NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]|_(?!_)){0,62}$")  # no "__": it separates tenant and collection
ID_NAMESPACE = uuid.UUID("6d3a3c0e-5b1f-4f0e-9a51-73776462736a")  # fixed forever: changing it orphans every point
DEFAULT_TENANT = "default"


class NameError_(ValueError):
    pass


def check(kind: str, name: str) -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise NameError_(f"invalid {kind} name {name!r}: use 1-63 lowercase letters, digits, '-' or '_' "
                         "(starting with a letter or digit, no '__')")
    return name


def engine_name(tenant: str, collection: str) -> str:
    """The Qdrant collection and graph key behind a swsdb collection."""
    return f"sw_{check('tenant', tenant)}__{check('collection', collection)}"


def parse_engine_name(name: str, tenant: str) -> str | None:
    prefix = f"sw_{tenant}__"
    return name[len(prefix):] if name.startswith(prefix) else None


def point_id(value: str | int) -> str | int:
    """Qdrant accepts unsigned integers and UUIDs. Other strings map to a stable UUID (the original is kept in the
    payload under `_id`, so results show the id the user gave)."""
    if isinstance(value, bool):
        raise ValueError("point id must be a string or a non-negative integer")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("integer point ids must be non-negative")
        return value
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("point id must be a non-empty string (up to 512 characters) or a non-negative integer")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid5(ID_NAMESPACE, value))
