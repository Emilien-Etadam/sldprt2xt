# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Read a Parasolid schema file.

A schema file (``sch_NNNNN.s_t``) is a text transmit whose body is the table
of node definitions. Each node is declared as::

    <nodetype> <NAME>; <description>; <transmit> <n_fields> <variable>

followed by exactly ``n_fields`` field lines::

    <fieldname>; <type>; <transmit> <ptr_class> <n_elts>

``transmit == 0`` marks ephemeral data that never reaches a transmit file and
must be skipped when decoding. ``n_elts`` is 0 for a scalar, 1 for a
variable-length field whose count is written into the data, and ``n`` for a
fixed array.

Field type letters, per the format reference §2.1.4:

===== ===========================================
``u`` unsigned byte             ``c`` char
``l`` logical (one byte, 0/1)   ``n`` short int
``w`` Unicode char, as short    ``d`` int
``p`` pointer index             ``f`` double
``i`` interval, 2 doubles       ``v`` vector, 3 doubles
``b`` box, 6 doubles            ``h`` intersection point, 3 doubles
===== ===========================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

#: How many doubles each real-valued field type occupies.
DOUBLES_PER_TYPE = {"f": 1, "i": 2, "v": 3, "b": 6, "h": 3}

#: Field types that are a single small integer or byte.
SCALAR_TYPES = {"u", "c", "l", "n", "w", "d", "p"}

KNOWN_TYPES = SCALAR_TYPES | set(DOUBLES_PER_TYPE)

_NODE_LINE = re.compile(r"^(\d+)\s+(\S+);\s*(.*?);\s*(\d+)\s+(\d+)\s+(\d+)\s*$")
_FIELD_LINE = re.compile(r"^(.+?);\s*(\w+);\s*(\d+)\s+(\d+)\s+(\d+)\s*$")

_END_OF_HEADER = "**END_OF_HEADER"


class SchemaError(Exception):
    """The schema file does not have the shape we expect."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    #: One of :data:`KNOWN_TYPES`, or ``""`` when the field is a pointer whose
    #: type letter the schema omits.
    type: str
    #: False for ephemeral fields that never appear in a transmit.
    transmitted: bool
    #: Non-zero only for pointers: the class of node this may point at.
    ptr_class: int
    #: 0 scalar · 1 variable-length · n fixed array of n.
    n_elts: int

    @property
    def is_variable(self) -> bool:
        return self.n_elts == 1

    @property
    def is_array(self) -> bool:
        return self.n_elts > 1


@dataclass(frozen=True)
class NodeSpec:
    node_type: int
    name: str
    description: str
    transmitted: bool
    variable: bool
    fields: tuple[FieldSpec, ...]

    @property
    def effective_fields(self) -> tuple[FieldSpec, ...]:
        """Fields that actually appear in transmitted data.

        Per §2.1.2, a field is effective when it is transmittable *or*
        variable-length. The ``or`` matters: a variable-length field is
        carried even with ``transmit == 0``.
        """
        return tuple(f for f in self.fields if f.transmitted or f.is_variable)


@dataclass
class Schema:
    """One schema version, indexed by node type."""

    key: str
    modeller_version: str | None
    nodes: dict[int, NodeSpec]

    def __getitem__(self, node_type: int) -> NodeSpec:
        try:
            return self.nodes[node_type]
        except KeyError:
            raise SchemaError(
                f"node type {node_type} is not in schema {self.key}"
            ) from None

    def __contains__(self, node_type: int) -> bool:
        return node_type in self.nodes

    def by_name(self, name: str) -> NodeSpec:
        for node in self.nodes.values():
            if node.name == name:
                return node
        raise SchemaError(f"no node named {name!r} in schema {self.key}")

    @property
    def max_node_type(self) -> int:
        return max(self.nodes)


def parse_schema(text: str) -> Schema:
    """Parse the text of a ``.s_t`` schema file."""
    marker = text.find(_END_OF_HEADER)
    if marker == -1:
        raise SchemaError("no **END_OF_HEADER marker")
    body = text[text.find("\n", marker) + 1 :]

    lines = [ln.rstrip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln.strip()]

    key = ""
    modeller = None
    for line in lines[:6]:
        match = re.search(r"SCHEMA FILE created by modeller version (\d+)/(\d+)", line)
        if match:
            modeller, key = match.group(1), f"SCH_{match.group(2)}"
            break

    nodes: dict[int, NodeSpec] = {}
    index = 0
    while index < len(lines):
        match = _NODE_LINE.match(lines[index])
        if match is None:
            index += 1
            continue
        node_type = int(match.group(1))
        n_fields = int(match.group(5))
        index += 1

        fields: list[FieldSpec] = []
        for _ in range(n_fields):
            if index >= len(lines):
                raise SchemaError(
                    f"node {node_type} declares {n_fields} fields, "
                    f"file ends after {len(fields)}"
                )
            field_match = _FIELD_LINE.match(lines[index])
            if field_match is None:
                raise SchemaError(
                    f"node {node_type}: expected a field, got {lines[index]!r}"
                )
            fields.append(
                FieldSpec(
                    name=field_match.group(1).strip(),
                    type=field_match.group(2),
                    transmitted=field_match.group(3) == "1",
                    ptr_class=int(field_match.group(4)),
                    n_elts=int(field_match.group(5)),
                )
            )
            index += 1

        nodes[node_type] = NodeSpec(
            node_type=node_type,
            name=match.group(2),
            description=match.group(3),
            transmitted=match.group(4) == "1",
            variable=match.group(6) == "1",
            fields=tuple(fields),
        )

    if not nodes:
        raise SchemaError("no node definitions found")
    return Schema(key=key, modeller_version=modeller, nodes=nodes)


@cache
def load_schema(path: str | Path) -> Schema:
    """Read and cache a schema file from disk."""
    return parse_schema(Path(path).read_text(encoding="latin-1"))


@cache
def find_schema(root: Path, key: str) -> Path | None:
    """Locate ``sch_<key>.s_t`` under ``root``, ignoring case.

    The library mixes ``SCH_11003.s_t``, ``Sch_1022.s_t`` and
    ``sch_12006.s_t``, which matters the moment the lookup runs anywhere
    other than Windows. The walk matches the whole **name** case-blind —
    a set copied off an old share can carry ``.S_T`` extensions, which a
    case-sensitive glob would hide on Linux from the very code meant to
    tolerate their casing.

    Cached: a batch of parts saved by the same SolidWorks resolves the
    same ``(root, key)`` pair once, not twice per file over a directory
    that may live on a network share.
    """
    number = key.upper().removeprefix("SCH_")
    wanted = f"sch_{number}.s_t".lower()
    for candidate in root.rglob("*"):
        if candidate.name.lower() == wanted:
            return candidate
    return None


def resolve_schemas(
    root: Path, base_schema: str | None, schema_key: str
) -> tuple[Schema, Schema | None]:
    """The pair every decode needs: the base schema, and the file's own.

    The base is required — nothing decodes without it — so its absence
    raises :class:`SchemaError` naming the file to deposit. The version
    schema serves as a cross-check when reading and supplies the
    ``ASSEMBLY`` layouts when writing; absent, the decode still stands,
    so it comes back as ``None``. Never substitute the base for it:
    comparing a 2001 field list against a 2024 one is exactly how
    reading used to break on machines that only carry the public set.
    """
    base_path = find_schema(root, f"SCH_{base_schema}")
    if base_path is None:
        raise SchemaError(
            f"base schema sch_{base_schema}.s_t not found under {root}"
        )
    version_path = find_schema(root, schema_key.split("_")[-1])
    return load_schema(base_path), (
        load_schema(version_path) if version_path else None
    )
