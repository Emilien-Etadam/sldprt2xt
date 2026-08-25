# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""The Parasolid transmit embedded in a part file.

SolidWorks stores its B-rep as a Parasolid *transmit*, compressed inside
``Contents/Config-N-Partition``. What it stores is the transmit **body** only:
there is no ``**ABCDEF…**PARASOLID`` text header like a ``.x_b`` written by
Parasolid itself, so the blob starts straight at the ``PS`` magic and no other
Parasolid consumer will accept it as-is.

This module reads that body's own header, reads the header of a ground-truth
``.x_t`` for comparison, and can put the missing text header back so the
extracted body becomes a file other tools will open.

It does **not** decode the node stream. That is phase 3.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from .carve import carve
from .container import Container

#: An embedded transmit body starts here. The two NUL bytes are the high half
#: of the big-endian length that follows, not part of the magic.
BINARY_MAGIC = b"PS"

#: The printable preamble opening a standalone text or binary transmit file.
TEXT_PREAMBLE = (
    b"**ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    b"**************************"
)
TEXT_PREAMBLE_2 = (
    b"**PARASOLID !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~0123456789"
    b"**************************"
)
END_OF_HEADER = b"**END_OF_HEADER"

#: Header lines are padded to this width with asterisks.
_HEADER_WIDTH = 80

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")

#: Field names shorter than this are too common as coincidences to trust.
_MIN_NAME = 3


class TransmitError(Exception):
    """The bytes are not a Parasolid transmit we recognise."""


@dataclass
class Transmit:
    """One Parasolid transmit, embedded or standalone."""

    #: ``"binary"`` or ``"text"``.
    form: str
    #: Free-text banner, e.g. ``": TRANSMIT FILE (partition) created by …"``.
    description: str
    #: Full schema string of the body, e.g. ``"SCH_3701250_37102_13006"``.
    schema: str
    #: Offset in ``data`` where the node stream begins.
    body_offset: int
    data: bytes = field(repr=False)

    @property
    def guise(self) -> str:
        """``partition``, ``deltas`` or ``transmit`` — what kind of dump this is."""
        match = re.search(r"TRANSMIT FILE(?: \((\w+)\))?", self.description)
        if match is None:
            return "unknown"
        return match.group(1) or "transmit"

    @property
    def modeller_version(self) -> str | None:
        """The Parasolid build that wrote it, e.g. ``"3701250"`` for V37.1.250."""
        match = re.search(r"modeller version (\d+)", self.description)
        return match.group(1) if match else None

    @property
    def schema_key(self) -> str:
        """The schema as a file header spells it — the body schema less its last part.

        The body carries ``SCH_3701250_37102_13006``; the ``SCH=`` keyword of a
        standalone file carries ``SCH_3701250_37102``. The trailing group is
        the base schema the delta-schemas resolve against.
        """
        return self.schema.rsplit("_", 1)[0]

    @property
    def base_schema(self) -> str | None:
        parts = self.schema.rsplit("_", 1)
        return parts[1] if len(parts) == 2 else None

    @property
    def version_schema(self) -> str:
        """The schema of the release that wrote the file — ``"37102"`` say.

        What :func:`sldprt.xt.schema.resolve_schemas` looks up as the
        optional half of the pair; the required half is ``base_schema``.
        """
        return self.schema_key.split("_")[-1]


def read_binary_transmit(data: bytes) -> Transmit:
    """Parse the header of an embedded binary transmit.

    Layout, all lengths big-endian::

        +0   'PS'
        +2   u32   description length
        +6   description
        +..  u32   schema length
        +..  schema
        +..  node stream
    """
    if not data.startswith(BINARY_MAGIC):
        raise TransmitError(f"no PS magic (starts {data[:8].hex(' ')})")
    if len(data) < 10:
        raise TransmitError("too short to hold a header")

    desc_len = struct.unpack_from(">I", data, 2)[0]
    if desc_len > len(data):
        raise TransmitError(f"description length {desc_len} exceeds the blob")
    at = 6 + desc_len
    description = data[6:at].decode("latin-1")

    if at + 4 > len(data):
        raise TransmitError("truncated before the schema length")
    schema_len = struct.unpack_from(">I", data, at)[0]
    at += 4
    if at + schema_len > len(data):
        raise TransmitError(f"schema length {schema_len} exceeds the blob")
    schema = data[at : at + schema_len].decode("latin-1")

    return Transmit(
        form="binary",
        description=description,
        schema=schema,
        body_offset=at + schema_len,
        data=data,
    )


def read_text_transmit(data: bytes) -> Transmit:
    """Parse a standalone text transmit — a ``.x_t`` as SolidWorks exports it.

    The body opens with ``T<n> <description><m> <schema>``, where ``n`` and
    ``m`` are lengths. Header lines wrap at 80 columns, and the wrap can fall
    inside a value, so the body is unwrapped before anything is read from it.
    """
    if not data.startswith(TEXT_PREAMBLE[:20]):
        raise TransmitError("no Parasolid text preamble")
    marker = data.find(END_OF_HEADER)
    if marker == -1:
        raise TransmitError("no **END_OF_HEADER marker")

    line_end = data.find(b"\n", marker)
    body = unwrap(data[line_end + 1 :])

    match = re.match(rb"T(\d+) ", body)
    if match is None:
        raise TransmitError("body does not open with a T length record")
    desc_len = int(match.group(1))
    at = match.end()
    description = body[at : at + desc_len].decode("latin-1")
    at += desc_len

    match = re.match(rb"(\d+) ", body[at:])
    if match is None:
        raise TransmitError("no schema length after the description")
    schema_len = int(match.group(1))
    at += match.end()
    schema = body[at : at + schema_len].decode("latin-1")

    return Transmit(
        form="text",
        description=description,
        schema=schema,
        body_offset=at + schema_len,
        data=body,
    )


def unwrap(data: bytes) -> bytes:
    """Remove the 80-column line wrapping of a text transmit.

    Not cosmetic: the wrap falls wherever the column runs out, including in the
    middle of a length-prefixed name, so anything read before unwrapping is
    read wrong.
    """
    return data.replace(b"\r", b"").replace(b"\n", b"")


def read_transmit(data: bytes) -> Transmit:
    """Parse either form, whichever these bytes are."""
    if data.startswith(BINARY_MAGIC):
        return read_binary_transmit(data)
    return read_text_transmit(data)


#: How much of a transmit :func:`field_names` scans, in bytes.
#: Every node type contributes one schema splice, at its first appearance, so
#: the splices cluster near the start while the rest is node data. Scanning a
#: 32 MB transmit end to end takes minutes and finds nothing new. Raise this
#: if a comparison ever comes up short.
FIELD_NAME_SCAN_LIMIT = 1 << 20


def field_names(transmit: Transmit, *, limit: int = FIELD_NAME_SCAN_LIMIT) -> list[str]:
    """Field names of the schema the transmit embeds, in order of appearance.

    Both forms length-prefix their names — one byte in binary, decimal digits
    and a space in text. Names are accepted only when the prefix matches the
    length exactly, which is what keeps arbitrary byte runs out.

    Only the first ``limit`` bytes are scanned — see
    :data:`FIELD_NAME_SCAN_LIMIT`.
    """
    window = transmit.data[:limit]
    if transmit.form == "binary":
        found = []
        for match in re.finditer(rb"[a-z][a-z0-9_]*", window):
            name = match.group().decode()
            start = match.start()
            if len(name) >= _MIN_NAME and start > 0 and window[start - 1] == len(name):
                found.append(name)
        return found

    text = window.decode("latin-1")
    found = []
    for match in re.finditer(r"(\d+) ", text):
        digits = match.group(1)
        # The run of digits may butt up against the previous token, so the
        # length is some suffix of it, not necessarily the whole run. Try each
        # suffix longest-first and let the length check pick the right one.
        for cut in range(len(digits)):
            suffix = digits[cut:]
            if suffix.startswith("0"):
                continue
            length = int(suffix)
            candidate = text[match.end() : match.end() + length]
            if length >= _MIN_NAME and _IDENTIFIER.match(candidate):
                found.append(candidate)
                break
    return found


def _header_line(text: bytes) -> bytes:
    return text.ljust(_HEADER_WIDTH, b"*")


def to_standalone(transmit: Transmit, *, key: str = "extracted") -> bytes:
    """Put back the text header SolidWorks omitted, yielding a real ``.x_b``.

    Only the keywords needed to identify the file are emitted. The rest of
    PART1 is optional per the format reference, and inventing an ``MC=`` or a
    ``DATE=`` would be stating as fact something we do not know — a header
    that lies is worse than a header that is terse.

    The guise is taken from the payload's own description rather than assumed.
    A SolidWorks part stores a **partition** transmit, not a part transmit, and
    writing ``GUISE=transmit`` above a payload that announces
    ``TRANSMIT FILE (partition)`` makes the two halves of the file disagree.
    Declaring it truthfully does not make the file importable into SolidWorks —
    a partition is read by a different call than a part — but it stops the file
    from misrepresenting itself. See Q-17.
    """
    if transmit.form != "binary":
        raise TransmitError("only a binary transmit needs its header rebuilt")

    header = b"\n".join(
        [
            _header_line(TEXT_PREAMBLE),
            _header_line(TEXT_PREAMBLE_2),
            b"**PART1;",
            b"FORMAT=binary;",
            f"GUISE={transmit.guise};".encode("latin-1"),
            f"KEY={key};".encode("latin-1"),
            f"FILE={key}.x_b;".encode("latin-1"),
            b"**PART2;",
            f"SCH={transmit.schema_key};".encode("latin-1"),
            b"USFLD_SIZE=0;",
            b"**PART3;",
            _header_line(END_OF_HEADER),
            b"",
        ]
    )
    return header + transmit.data


#: The stream holding a configuration's B-rep. The configuration number is not
#: always 0 — a part saved with several configurations numbers them, and the
#: active one is whichever SolidWorks last resolved.
_PARTITION_STREAM = re.compile(r"Config-(\d+)-Partition$")


def is_partition_stream(name: str) -> bool:
    """Can this stream carry the B-rep?

    The one spelling of the partition filter: :func:`extract_transmits`
    keeps only these streams, and passed as ``wanted`` to
    ``open_container`` the same test spares reading the thirty other
    streams of a CFB part. One predicate, so the pre-read filter and the
    extractor cannot drift apart.
    """
    return "Partition" in name


def main_partition(container: Container) -> Transmit | None:
    """The partition transmit carrying the B-rep, whatever its configuration.

    ``GhostPartition`` is excluded: it holds reference and construction bodies,
    not the solid.
    """
    for stream, transmits in extract_transmits(container).items():
        if not _PARTITION_STREAM.search(stream):
            continue
        for transmit in transmits:
            if transmit.guise == "partition":
                return transmit
    return None


def extract_transmits(container: Container) -> dict[str, list[Transmit]]:
    """Every transmit embedded in the container, keyed by stream name.

    Streams are considered when their name mentions a partition. Each carved
    section that opens with the ``PS`` magic becomes one transmit; sections
    that do not are skipped rather than reported, because a partition stream
    legitimately holds non-transmit sections too.
    """
    out: dict[str, list[Transmit]] = {}
    for blob in container.blobs:
        if not is_partition_stream(blob.name):
            continue
        found = []
        for region in carve(blob.data, scan_bare=False):
            if region.data.startswith(BINARY_MAGIC):
                try:
                    found.append(read_binary_transmit(region.data))
                except TransmitError:
                    continue
        if found:
            out[blob.name] = found
    return out


#: strftime("%a %b") depends on the locale, and the header is written in
#: latin-1: a Tuesday in a Greek or Japanese locale broke the encode. The
#: names Parasolid's own files carry are the C-locale ones, spelled out.
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _transmit_date() -> str:
    """Now, as ``DATE=`` spells it — C locale whatever the process locale."""
    import time

    t = time.localtime()
    return (
        f"{_DAYS[t.tm_wday]} {_MONTHS[t.tm_mon - 1]} {t.tm_mday:02d} "
        f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} {t.tm_year}"
    )


def to_part_transmit(
    transmit: Transmit,
    nodes,
    base,
    layouts,
    current,
    *,
    key: str = "part",
    max_node_types: int | None = None,
    selection=None,
) -> str:
    """A ``.x_t`` of guise *transmit*, verified by reading it back.

    *selection* says what goes in — see
    :class:`sldprt.xt.edit.Selection`. Everything, by default.

    Raises rather than return a file whose own reader cannot follow it.
    """
    from .xt.write import verify, write_transmit

    # The maximum the source itself declares. Inventing one says something
    # about the file that the file does not say.
    if not max_node_types:
        raise TransmitError("the source did not declare a node-type maximum")
    declared = max_node_types

    text = write_transmit(
        nodes,
        base,
        layouts,
        max_node_types=declared,
        date=_transmit_date(),
        modeller_version=transmit.modeller_version or "0",
        schema_key=transmit.schema_key,
        key=key,
        selection=selection,
        current=current,
    )
    verify(text, nodes, base, current, layouts=layouts, selection=selection)
    return text
