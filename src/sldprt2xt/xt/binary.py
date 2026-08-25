# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Read the neutral-binary node stream of a Parasolid transmit.

Neutral binary is big-endian throughout, with IEEE doubles and ASCII. Two
encodings are not plain integers and account for most of the subtlety:

*pointer indices*
    Values below 32767 are one 2-byte integer, offset by one so the encoded
    form is always positive. Larger values are a negative first short followed
    by a quotient. Positive integers in the embedded-schema data use the same
    scheme.

*embedded delta-schemas*
    From Parasolid V14, a transmit carries the **difference** between its own
    schema and base schema 13006, so an older kernel can still read it. The
    difference is spliced in just after the node type, the first time each type
    appears. That is why decoding needs both schemas: the base one to diff
    against, the current one only as a cross-check.

Decoding stops being guesswork at exactly one point: a correct parse consumes
the buffer to its last byte. A stream that ends early or overruns has been
misread, whatever the node counts look like.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from .schema import DOUBLES_PER_TYPE, FieldSpec, Schema

#: Marks "this node's fields are identical to the base schema's".
UNCHANGED = 0xFF

#: NULLP. It closes the stream: node type, then an index, and nothing else —
#: no embedded-schema splice, no fields. Both forms agree: the text transmit
#: ends ``1 0`` and the binary ends ``00 01 00 01``, the latter being index 0
#: in the offset-by-one pointer encoding.
TERMINATOR = 1

#: Pointer indices split above this value.
_INDEX_MODULUS = 32767


class BinaryError(Exception):
    """The node stream does not decode."""


@dataclass
class Node:
    """One decoded node."""

    node_type: int
    name: str
    index: int
    values: dict[str, object] = dataclass_field(default_factory=dict)
    #: Byte range the node occupied, for diffing and for error reports.
    start: int = 0
    end: int = 0


class Reader:
    """A cursor over big-endian Parasolid data."""

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.pos = offset

    def __len__(self) -> int:
        return len(self.data)

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def _take(self, count: int) -> bytes:
        end = self.pos + count
        if end > len(self.data):
            raise BinaryError(
                f"needed {count} bytes at {self.pos:#x}, only {self.remaining} left"
            )
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def byte(self) -> int:
        return self._take(1)[0]

    def short(self) -> int:
        return struct.unpack(">H", self._take(2))[0]

    def signed_short(self) -> int:
        return struct.unpack(">h", self._take(2))[0]

    def int32(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def double(self) -> float:
        return struct.unpack(">d", self._take(8))[0]

    def index(self) -> int:
        """A pointer index, in the split encoding of §3.3.3."""
        low = self.signed_short()
        quotient = 0
        if low < 0:
            quotient = self.short()
            low = -low
        return quotient * _INDEX_MODULUS + low - 1

    def positive_int(self) -> int:
        """A positive integer — same encoding as a pointer index."""
        return self.index()

    def short_string(self) -> str:
        """A length byte followed by that many characters, no terminator."""
        return self._take(self.byte()).decode("latin-1")


def _read_field_spec(reader: Reader) -> FieldSpec:
    """Read one field definition out of an embedded schema edit.

    Order and omissions per §2.1.2.2: the type letter is absent when the field
    is a pointer (its class already says what it points at), and the transmit
    flag is absent unless the field is variable-length.
    """
    name = reader.short_string()
    ptr_class = reader.short()
    n_elts = reader.positive_int()
    type_letter = "" if ptr_class else reader.short_string()
    transmitted = True if n_elts != 1 else bool(reader.byte())
    return FieldSpec(
        name=name,
        type=type_letter or "p",
        transmitted=transmitted,
        ptr_class=ptr_class,
        n_elts=n_elts,
    )


def read_node_layout(
    reader: Reader, node_type: int, base: Schema
) -> tuple[list[FieldSpec], str, str]:
    """Read the embedded schema for ``node_type``, spliced after its first use.

    Returns the effective field list to decode this node's data with, plus the
    node's name and description when the stream carried them.
    """
    if node_type in base:
        spec = base[node_type]
        name, description = spec.name, spec.description
        base_fields = list(spec.effective_fields)
    else:
        # Not in the base schema at all: the stream spells the node out.
        count = reader.byte()
        name = reader.short_string()
        description = reader.short_string()
        return [_read_field_spec(reader) for _ in range(count)], name, description

    flag = reader.byte()
    if flag == UNCHANGED:
        return base_fields, name, description

    # Otherwise `flag` was the current schema's effective field count, and an
    # edit script follows: Copy, Delete, Insert, Append, terminated by Z.
    expected = flag
    fields: list[FieldSpec] = []
    at = 0
    while True:
        op = reader._take(1)
        if op == b"Z":
            break
        if op == b"C":
            if at >= len(base_fields):
                raise BinaryError(f"copy past the end of base node {node_type}")
            fields.append(base_fields[at])
            at += 1
        elif op == b"D":
            at += 1
        elif op in (b"I", b"A"):
            fields.append(_read_field_spec(reader))
        else:
            raise BinaryError(
                f"node {node_type}: unknown schema edit {op!r} at {reader.pos:#x}"
            )

    if len(fields) != expected:
        raise BinaryError(
            f"node {node_type}: edit script produced {len(fields)} fields, "
            f"header declared {expected}"
        )
    return fields, name, description


def _read_value(reader: Reader, spec: FieldSpec, count: int):
    """Read one field's value, honouring its element count."""
    if spec.type in DOUBLES_PER_TYPE:
        width = DOUBLES_PER_TYPE[spec.type]
        if count == 1 and width == 1:
            return reader.double()
        return [reader.double() for _ in range(count * width)]

    readers = {
        "u": reader.byte,
        "c": reader.byte,
        "l": reader.byte,
        "n": reader.short,
        "w": reader.short,
        "d": reader.int32,
        "p": reader.index,
    }
    if spec.type not in readers:
        raise BinaryError(f"unknown field type {spec.type!r} on {spec.name}")
    read = readers[spec.type]
    return read() if count == 1 else [read() for _ in range(count)]


def parse_nodes(
    data: bytes,
    body_offset: int,
    base: Schema,
    *,
    current: Schema | None = None,
    header: dict | None = None,
) -> tuple[list[Node], dict[int, list[FieldSpec]]]:
    """Decode a whole node stream.

    ``body_offset`` is where the transmit's header ends. ``base`` is the schema
    the embedded deltas are expressed against — schema 13006 for every file
    seen so far, and the transmit's own schema string names it. ``current`` is
    optional and used only to cross-check the field counts the stream declares.

    Raises if the stream does not decode to exactly the end of the buffer.
    """
    reader = Reader(data, body_offset)
    max_node_types = reader.short()
    user_field_size = reader.int32()
    if header is not None:
        header["max_node_types"] = max_node_types

    layouts: dict[int, list[FieldSpec]] = {}
    names: dict[int, str] = {}
    nodes: list[Node] = []

    # A node stream closes on a TERMINATOR record. Without tracking whether
    # one was reached, the loop simply runs out of bytes, and a file cut at a
    # node boundary reads as a whole one: cube's partition truncated after its
    # 96th node decoded 96 of 190 and raised nothing. The check below this loop
    # catches only the opposite case, bytes left over.
    terminated = False
    while reader.remaining > 0:
        start = reader.pos
        node_type = reader.short()
        if node_type == TERMINATOR:
            reader.index()
            terminated = True
            break
        if node_type > max_node_types:
            raise BinaryError(
                f"node type {node_type} at {start:#x} exceeds the declared "
                f"maximum {max_node_types}"
            )

        if node_type not in layouts:
            fields, name, _ = read_node_layout(reader, node_type, base)
            layouts[node_type] = fields
            names[node_type] = name
            if current is not None and node_type in current:
                declared = len(current[node_type].effective_fields)
                if declared != len(fields):
                    raise BinaryError(
                        f"node {node_type} ({name}): decoded {len(fields)} fields "
                        f"but schema {current.key} declares {declared}"
                    )

        fields = layouts[node_type]

        # Whether a node is variable-length is a property of its *layout*, not
        # of the base schema: a type absent from the base schema has no entry
        # to consult, and an edit script can change which field comes last.
        # A node is variable exactly when its final field is.
        variable_count = reader.int32() if fields and fields[-1].is_variable else None
        index = reader.index()

        values: dict[str, object] = {}
        for field_spec in fields:
            count = field_spec.n_elts or 1
            if field_spec.is_variable:
                if variable_count is None:
                    raise BinaryError(
                        f"node {node_type} has a variable field "
                        f"{field_spec.name!r} but no count was read"
                    )
                count = variable_count
            values[field_spec.name] = _read_value(reader, field_spec, count)

        nodes.append(
            Node(
                node_type=node_type,
                name=names[node_type],
                index=index,
                values=values,
                start=start,
                end=reader.pos,
            )
        )

    if not terminated:
        raise BinaryError(
            f"stream ends after {len(nodes)} nodes without a TERMINATOR at "
            f"{reader.pos:#x}: it is truncated"
        )
    if reader.remaining:
        raise BinaryError(
            f"stream did not consume its buffer: {reader.remaining} bytes left "
            f"at {reader.pos:#x} after {len(nodes)} nodes"
        )
    if user_field_size:
        raise BinaryError(f"user fields are not supported (size {user_field_size})")
    return nodes, layouts


def count_by_name(nodes: list[Node]) -> dict[str, int]:
    """How many nodes of each type, keyed by schema name."""
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node.name] = counts.get(node.name, 0) + 1
    return counts
