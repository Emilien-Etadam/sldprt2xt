# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Write a Parasolid text transmit — a ``.x_t`` other tools will open.

A ``.SLDPRT`` stores a transmit of guise *partition*; what Parasolid consumers
import is a transmit of guise *transmit*, rooted on bodies rather than on a
world. The two carry the same geometry — decoding a cube's partition and its
exported ``.x_t`` gives the same nodes, type for type — so what has to change
is the framing, not the shape. See FORMAT.md Q-17.

The tokenisation this writes is the one :mod:`sldprt.xt.text` reads, and the
two are checked against each other: a file written here, read back, must give
the nodes it was written from.
"""

from __future__ import annotations

import math
from decimal import Decimal
from functools import lru_cache

from .binary import Node
from .edit import (
    ASSEMBLY_TYPE,
    ATTRIBUTE_NODES,
    INSTANCE_TYPE,
    NOMINAL_GEOMETRY,
    PARTITION_ONLY,
    RESOLVED_GEOMETRY,
    SOLID_BODY,
    TRANSFORM_TYPE,
    Selection,
    WriteError,
    drop_attributes,
    drop_nodes,
    drop_partition_nodes,
    renumber,
    resolve_nominal,
    select_bodies,
    wrap_in_assembly,
)
from .schema import DOUBLES_PER_TYPE, FieldSpec, Schema
from .text import NULL_DOUBLE, TERMINATOR, UNCHANGED

#: Re-exported editing surface: callers assemble a transmit through this
#: module (select bodies, resolve nominal geometry, wrap, renumber, write)
#: without importing :mod:`.edit` themselves.
__all__ = [
    "ASSEMBLY_TYPE",
    "ATTRIBUTE_NODES",
    "INSTANCE_TYPE",
    "NOMINAL_GEOMETRY",
    "PARTITION_ONLY",
    "RESOLVED_GEOMETRY",
    "SOLID_BODY",
    "TRANSFORM_TYPE",
    "Selection",
    "WriteError",
    "drop_attributes",
    "drop_nodes",
    "drop_partition_nodes",
    "renumber",
    "resolve_nominal",
    "select_bodies",
    "wrap_in_assembly",
]

#: Column at which Parasolid wraps a text transmit. The wrap may fall anywhere,
#: including inside a token, because a reader unwraps before parsing.
WRAP = 80


class Tokens:
    """Collects the body text, separating tokens the way Parasolid does.

    The rule, read off an exported file: **a space goes in after a number, and
    nowhere else.** What follows it does not matter.

        239 0 12 36 CCCI7 lattice222 0 CCCI4 mesh1006 0 ... 16 BODY_RECIPE_200180

    ``36`` is a number, so the ops after it are separated; ``I`` is not, so the
    length ``7`` abuts it; ``BODY_RECIPE_2001`` ends in a digit and still takes
    no separator, because a string is read by its declared length and cannot
    run into what follows.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._after_number = False

    def _emit(self, text: str, *, number: bool) -> None:
        if not text:
            return
        if self._after_number:
            self._parts.append(" ")
        self._parts.append(text)
        self._after_number = number

    def raw(self, text: str) -> None:
        """Text that carries its own separation — a string body, a char run."""
        if not text:
            return
        self._parts.append(text)
        self._after_number = False

    def number(self, text: str) -> None:
        self._emit(text, number=True)

    def integer(self, value: int) -> None:
        self._emit(str(int(value)), number=True)

    def double(self, value: float) -> None:
        if value == NULL_DOUBLE:
            self.null()
            return
        self._emit(spell(float(value)), number=True)

    def logical(self, value) -> None:
        self._emit("T" if value else "F", number=False)

    def op(self, letter: str) -> None:
        """One edit-script operation."""
        self._emit(letter, number=False)

    def null(self) -> None:
        """The mark for an element the file does not record."""
        self._emit("?", number=False)

    def char(self, value) -> None:
        """A single character field."""
        self._emit(
            chr(value) if isinstance(value, int) else str(value)[:1], number=False
        )

    def short_string(self, text: str) -> None:
        self._emit(str(len(text)), number=True)
        self.raw(" " + text)

    def text(self) -> str:
        return "".join(self._parts).lstrip()


@lru_cache(maxsize=1 << 16)
def spell(value: float) -> str:
    """A double the way Parasolid spells it: the shortest form that survives.

    An exported file writes ``1e3``, ``1e-8``, ``.005`` and
    ``1734723475976805e-31`` where Python writes ``1000.0``, ``1e-08``,
    ``0.005`` and ``1.734723475976805e-16``. Two forms are derived from the
    same digits — mantissa with an exponent, or positional — and the shorter
    wins, ties going to the one that sorts first, which is what reproduces an
    exported file exactly.

    Both forms carry the digits of ``repr``, which is already the shortest
    decimal that reads back as this double, so neither can lose precision.
    """
    if value == 0.0:
        # Zero has no sign here. It also has two of them in Python, and they
        # hash alike, so a cache keyed on the value would hand ``-0`` back for
        # ``0`` once any file had contained a negative zero — which made the
        # output depend on what had been written before it.
        return "0"

    text = repr(float(value))
    if text in ("inf", "-inf", "nan"):
        return text

    number = Decimal(text).normalize()
    sign, digits, exponent = number.as_tuple()
    mantissa = "".join(str(d) for d in digits)
    lead = "-" if sign else ""

    exponential = lead + mantissa + (f"e{exponent}" if exponent else "")

    if exponent >= 0:
        positional = lead + mantissa + "0" * exponent
    elif -exponent < len(mantissa):
        cut = len(mantissa) + exponent
        positional = lead + mantissa[:cut] + "." + mantissa[cut:]
    else:
        positional = lead + "." + "0" * (-exponent - len(mantissa)) + mantissa

    if len(exponential) != len(positional):
        return min(exponential, positional, key=len)
    # Same length, and the file settles it by the sign of the exponent:
    # 16500 is written 165e2, while 0.005 is written .005 rather than 5e-3.
    return exponential if exponent > 0 else positional


def _shape(spec: FieldSpec) -> tuple:
    """A field reduced to what a transmit actually states about it.

    A pointer field reads as type ``""`` from a schema file and as ``"p"`` from
    a splice, so comparing the objects makes the same field look like two
    different ones depending on where it came from.
    """
    return (spec.name, spec.type or "p", spec.transmitted, spec.ptr_class, spec.n_elts)


def _same(left, right) -> bool:
    return [_shape(f) for f in left] == [_shape(f) for f in right]


def _write_field_spec(tokens: Tokens, spec: FieldSpec) -> None:
    """One field definition, as the reader's ``_read_field_spec`` expects it."""
    tokens.short_string(spec.name)
    tokens.integer(spec.ptr_class)
    tokens.integer(spec.n_elts)
    if not spec.ptr_class:
        tokens.short_string(spec.type)
    if spec.n_elts == 1:
        tokens.logical(spec.transmitted)


def _write_layout(
    tokens: Tokens,
    node_type: int,
    fields: list[FieldSpec],
    base: Schema,
    name: str = "",
) -> None:
    """The schema spliced in at a node type's first appearance."""
    if node_type not in base:
        # A type the base schema has never heard of carries its own name and
        # description. The description an exported file writes is the name
        # made readable: INTERSECTION_DATA becomes "Intersection data".
        tokens.integer(len(fields))
        tokens.short_string(name)
        tokens.short_string(name.replace("_", " ").capitalize())
        for spec in fields:
            _write_field_spec(tokens, spec)
        return

    if _same(base[node_type].effective_fields, fields):
        tokens.integer(UNCHANGED)
        return

    # An edit script against the base, the way Parasolid writes one: copy the
    # fields that are still there, delete the ones that are not, insert the
    # rest. A script of nothing but inserts describes the same field list, but
    # it leaves every base field unconsumed, and a reader entitled to expect a
    # diff has no reason to accept that.
    base_fields = list(base[node_type].effective_fields)
    tokens.integer(len(fields))
    at = want = 0
    # Only until this layout's fields are all placed. Base fields left over
    # need no deletion: 'Z' ends the script and what was not reached is not
    # part of it.
    while want < len(fields):
        if (
            at < len(base_fields)
            and want < len(fields)
            and _shape(base_fields[at]) == _shape(fields[want])
        ):
            tokens.op("C")
            at += 1
            want += 1
        elif at < len(base_fields) and _shape(base_fields[at]) not in [
            _shape(f) for f in fields[want:]
        ]:
            # The base has a field this layout drops. Deleted before anything
            # is inserted in its place, which is the order Parasolid writes.
            tokens.op("D")
            at += 1
        else:
            # 'A' once the base list is spent, 'I' while it still has fields
            # to insert in front of. The reader treats the two alike; the
            # exported files do not, and matching them costs nothing.
            tokens.op("A" if at >= len(base_fields) else "I")
            _write_field_spec(tokens, fields[want])
            want += 1
    tokens.op("Z")


def _write_value(tokens: Tokens, spec: FieldSpec, value, count: int) -> None:
    """One field's ``count`` elements."""
    if spec.type in DOUBLES_PER_TYPE:
        width = DOUBLES_PER_TYPE[spec.type]
        values = value if isinstance(value, list) else [value]
        if len(values) != count * width:
            raise WriteError(
                f"{spec.name}: {len(values)} doubles for {count} elements of "
                f"width {width}"
            )
        for element in range(count):
            chunk = values[element * width : (element + 1) * width]
            if all(v == NULL_DOUBLE for v in chunk):
                tokens.null()
                continue
            for component in chunk:
                tokens.double(component)
        return

    values = value if isinstance(value, list) else [value]
    if len(values) != count:
        raise WriteError(f"{spec.name}: {len(values)} values for {count} elements")
    if spec.type == "c" and count > 1:
        # One space in front of the run, then the characters solid — which is
        # how SolidWorks writes it: `84 255 6 562 BAAAAB`. Without the space
        # the index 5318 and a first character '7' read as 53187; with a space
        # between each, a character that *is* a space would vanish.
        tokens.char(values[0])
        tokens.raw("".join(
            chr(v) if isinstance(v, int) else str(v)[:1] for v in values[1:]
        ))
        return
    for element in values:
        if spec.type == "c":
            # A character the file does not record is written '?', not the NUL
            # byte its decoded value looks like.
            if element in (0, "\x00"):
                tokens.null()
            else:
                tokens.char(element)
        elif spec.type == "l":
            tokens.logical(element)
        else:
            tokens.integer(element)


def write_nodes(
    nodes: list[Node],
    base: Schema,
    layouts: dict[int, list[FieldSpec]],
    *,
    max_node_types: int,
) -> str:
    """The body of a text transmit: header integers, nodes, terminator.

    ``layouts`` are the field lists the stream was **decoded** with, which the
    readers hand back. Deriving them from a schema instead is not the same
    thing: a file splices its own edits in, and a type whose spliced layout
    happens to have the schema's field count but not its fields would then be
    written with the wrong fields in the right number — a file that parses for
    thousands of nodes and then stops making sense.
    """
    tokens = Tokens()
    tokens.integer(max_node_types)
    tokens.integer(0)  # user field size

    seen: set[int] = set()
    for node in nodes:
        fields = layouts.get(node.node_type)
        if fields is None:
            raise WriteError(
                f"node type {node.node_type} ({node.name}) has no decoded layout"
            )

        tokens.integer(node.node_type)
        if node.node_type not in seen:
            _write_layout(tokens, node.node_type, fields, base, node.name)
            seen.add(node.node_type)

        if fields and fields[-1].is_variable:
            last = fields[-1]
            value = node.values.get(last.name, [])
            values = value if isinstance(value, list) else [value]
            width = DOUBLES_PER_TYPE.get(last.type, 1)
            tokens.integer(len(values) // width)
        tokens.integer(node.index)

        for field_spec in fields:
            count = field_spec.n_elts or 1
            value = node.values.get(field_spec.name)
            if field_spec.is_variable:
                values = value if isinstance(value, list) else [value]
                width = DOUBLES_PER_TYPE.get(field_spec.type, 1)
                count = len(values) // width
            _write_value(tokens, field_spec, value, count)

    tokens.integer(TERMINATOR)
    tokens.integer(0)
    # The trailing space an exported file carries. With it, rewriting a
    # Parasolid ``.x_t`` reproduces it byte for byte.
    return tokens.text() + " "


def wrap(text: str, width: int = WRAP) -> str:
    """Break the body into lines, as Parasolid does.

    Lines are ``width`` columns, with one exception: **a line never ends in a
    space**. When the column runs out on a separator, the break falls one
    character earlier and the space opens the next line instead.

    That exception is not cosmetic. Parasolid's reader drops trailing blanks
    from each line, so a separator parked in the last column disappears and
    the two tokens it kept apart merge into one. The file still says the same
    thing once unwrapped, and is still refused — which is exactly what
    happened here: a rewrite that was byte-identical to a working file after
    unwrapping would not open, and the only difference was where the breaks
    fell. See F-46.

    Two independent Parasolid writers confirm the rule: neither SolidWorks'
    export nor Plasticity's has a single interior line ending in a space.
    """
    lines = []
    at = 0
    while at < len(text):
        end = min(at + width, len(text))
        if end < len(text) and text[end - 1] == " ":
            end -= 1
        lines.append(text[at:end])
        at = end
    return "\n".join(lines)


#: The header lines a standalone transmit opens with.
PREAMBLE = (
    "**ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "**************************"
)
PREAMBLE_2 = (
    "**PARASOLID !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~0123456789"
    "**************************"
)
END_OF_HEADER = "**END_OF_HEADER"


def _padded(line: str, width: int = WRAP) -> str:
    return line if len(line) >= width else line + "*" * (width - len(line))



def write_transmit(
    nodes: list[Node],
    base: Schema,
    layouts: dict[int, list[FieldSpec]],
    *,
    max_node_types: int,
    modeller_version: str,
    schema_key: str,
    key: str = "part",
    date: str = "Mon Jan  1 00:00:00 2001",
    selection: Selection | None = None,
    current: Schema | None = None,
) -> str:
    """A complete ``.x_t``: text header, banner, node stream.

    The banner carries no guise. That is what makes this a part transmit
    rather than the partition it was decoded from — see FORMAT.md Q-17 — and
    it is why the world node has to go with it.

    Pass *current* to root several bodies on an assembly, which Parasolid
    requires — see :func:`wrap_in_assembly`.
    """
    if not (modeller_version.isdigit() and len(modeller_version) >= 5):
        # A banner does not always name a modeller build, and the caller's
        # stand-in used to be "0" — which the FRU line below turned into
        # int("") and a crash, so the fallback could never survive its own
        # header. Padded to a full version, 0000000 reads as Version 00.0,
        # build 0, and real versions pass through untouched.
        digits = modeller_version if modeller_version.isdigit() else "0"
        modeller_version = digits.zfill(7)

    kept = (selection or Selection()).apply(nodes, layouts, current)
    body = write_nodes(
        renumber(kept, layouts),
        base,
        layouts,
        max_node_types=max_node_types,
    )
    banner = f": TRANSMIT FILE created by modeller version {modeller_version}"
    schema = f"{schema_key}_{base.key.rsplit('_', 1)[-1]}"
    stream = f"T{len(banner)} {banner}{len(schema)} {schema}{body}"

    header = "\n".join(
        [
            _padded(PREAMBLE),
            _padded(PREAMBLE_2),
            "**PART1;",
            "MC=AMD64;",
            "MC_MODEL=unknown;",
            "MC_ID=unknown;",
            "OS=unknown;",
            "OS_RELEASE=unknown;",
            # 3701250 is version 37.1, build 250: two digits of major, two of
            # minor, the rest the build.
            f"FRU=Parasolid Version {modeller_version[:2]}."
            f"{int(modeller_version[2:4])}, build {int(modeller_version[4:])};",
            "APPL=openSW;",
            "SITE=;",
            "USER=unknown;",
            "FORMAT=text;",
            "GUISE=transmit;",
            f"KEY={key};",
            f"FILE={key}.x_t;",
            f"DATE={date};",
            "**PART2;",
            f"SCH={schema_key};",
            "USFLD_SIZE=0;",
            "**PART3;",
            _padded(END_OF_HEADER),
            "",
        ]
    )
    return header + wrap(stream) + "\n"


def verify(
    text: str,
    nodes: list[Node],
    base: Schema,
    current: Schema,
    *,
    layouts: dict[int, list[FieldSpec]],
    selection: Selection | None = None,
) -> None:
    """Read the file back and check it says what it was written from.

    A transmit that a reader cannot follow is worse than no transmit: it looks
    like a file and opens like nothing. Rather than hand one out on the
    strength of having produced it, this reads it again and compares.

    Every node is compared field by field, references included, against the
    same ``Selection.apply`` and ``renumber`` the writer itself went through:
    the check and the writer cannot disagree about what was meant to be in
    the file, because they derive it the same way.

    ``layouts`` is what makes that possible, and is therefore required rather
    than optional. ``renumber`` needs it to know which fields are references,
    so without it the comparison could only ever reach type and rank — while
    still calling itself a verification (C1 of the audit).
    """
    from ..parasolid import read_text_transmit
    from . import text as text_reader

    transmit = read_text_transmit(text.encode("latin-1"))
    if transmit.guise != "transmit":
        raise WriteError(f"wrote guise {transmit.guise!r}, not 'transmit'")
    body = transmit.data[transmit.body_offset :].decode("latin-1")
    again = text_reader.parse_nodes(body, base, current=current)

    chosen = selection or Selection()
    expected = chosen.apply(nodes, layouts, current)
    if len(again) != len(expected):
        raise WriteError(
            f"wrote {len(expected)} nodes and read back {len(again)}"
        )
    for position, (wanted, got) in enumerate(zip(expected, again, strict=False), start=1):
        if wanted.node_type != got.node_type:
            raise WriteError(
                f"node {wanted.index} ({wanted.name}) came back as type "
                f"{got.node_type}"
            )
        if got.index != position:
            raise WriteError(
                f"node {position} came back numbered {got.index}; the file is "
                f"written with dense indices so that its root carries 1"
            )

    # Type and rank alone let a corrupted value through — a node_id off by
    # one reads back as a well-formed file that says something else. So the
    # comparison goes field by field, against the same renumbering the writer
    # applied (C1 of the audit).
    for wanted, got in zip(renumber(expected, layouts), again, strict=False):
        if wanted.values == got.values:
            continue
        for field, value in wanted.values.items():
            other = got.values.get(field)
            if not _same_value(value, other):
                raise WriteError(
                    f"node {got.index} ({wanted.name}) field {field!r}: "
                    f"wrote {value!r}, read back {other!r}"
                )
        surplus = set(got.values) - set(wanted.values)
        if surplus:
            raise WriteError(
                f"node {got.index} ({wanted.name}) read back with fields "
                f"never written: {sorted(surplus)}"
            )


def _same_value(wanted, got) -> bool:
    """Field equality across one serialisation round trip.

    Exact for everything — the writer emits doubles at full precision and
    the reader parses them back to the same bits — except that a NaN never
    equals itself and must be matched as a pair.
    """
    if isinstance(wanted, float) and isinstance(got, float):
        return wanted == got or (math.isnan(wanted) and math.isnan(got))
    if isinstance(wanted, (list, tuple)) and isinstance(got, (list, tuple)):
        return len(wanted) == len(got) and all(
            _same_value(a, b) for a, b in zip(wanted, got, strict=False)
        )
    # Le décodeur binaire livre les octets d'une chaîne en codes, le
    # lecteur texte en caractères : [83, 68, 76] et 'S','D','L' disent le
    # même « SDL ». Une seule orthographe de part et d'autre.
    if isinstance(wanted, int) and isinstance(got, str) and len(got) == 1:
        return ord(got) == wanted
    if isinstance(wanted, str) and len(wanted) == 1 and isinstance(got, int):
        return ord(wanted) == got
    # Un champ caractère non renseigné s'écrit '?' — et le binaire stocke
    # ce point d'interrogation lui-même (63) comme valeur d'inconnu, que
    # le lecteur texte rend 0. Un 63 numérique se sérialise « 63 » et
    # revient 63 : seule la voie caractère peut produire cette paire.
    if {wanted, got} == {0, 63}:
        return True
    return wanted == got
