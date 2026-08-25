# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Container-level access to SolidWorks part files.

A ``.SLDPRT`` is a bag of named byte blobs. Two envelopes are publicly
documented (see ``docs/EXISTANT.md``):

``cfb``
    Classic OLE2 / Compound File Binary. Streams are addressed by path,
    e.g. ``Contents/Config-0-Partition``. Requires :mod:`olefile`.

``block``
    Used by newer SolidWorks saves. A flat sequence of raw-DEFLATE blocks,
    each carrying its own section name in a nibble-swapped preamble. The
    same file also contains *cache cells* and a *tail directory* that reuse
    the block marker but are not payloads — we classify and skip them.

Nothing in this module interprets a payload. Naming a blob is the whole job;
deciding what is inside it belongs to :mod:`sldprt.detect` and later phases.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

OLE2_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
BLOCK_MARKER = bytes.fromhex("140006000800")

#: Fixed 16-byte magic that introduces a wrapped semantic payload, both in
#: ``__ZLB`` compound streams and in the sections of a Partition stream.
SECTION_MAGIC = bytes.fromhex("231dd571da8148a2a85898b21b89ef99")

#: Minimum bytes a block frame occupies before its variable-length parts.
BLOCK_HEADER_SIZE = 26


def nibble_swap(data: bytes, *, key: int = 4) -> bytes:
    """Rotate every byte left by ``key`` bits — the stream-name codec.

    Section names in the block container are stored rotated by a per-file
    key held at byte 7 of the file header (established by the openswx
    project, ``docs/refs/openswx-blussyya-notes.md``). Measured on this
    corpus: every modern file declares key 4 — the nibble swap this
    function has always done, which is the one self-inverse rotation. The
    general form is kept so a file with another key decodes rather than
    yielding garbage names; callers pass the header byte down.
    """
    shift = key & 7
    if shift == 0:
        return bytes(data)
    return bytes(((b << shift) & 0xFF) | (b >> (8 - shift)) for b in data)


@dataclass
class Blob:
    """One named byte string recovered from a container."""

    name: str
    data: bytes
    #: ``"cfb"`` or ``"block"``.
    origin: str
    #: Byte offset in the file the blob was recovered from, when meaningful.
    offset: int | None = None
    #: Container-level facts worth reporting but not worth a field each.
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.data)


class ContainerError(Exception):
    """The file is not a container shape we recognise."""


def detect_container(data: bytes) -> str:
    """Return ``"cfb"``, ``"block"`` or ``"unknown"`` for a whole file."""
    if data.startswith(OLE2_MAGIC):
        return "cfb"
    if BLOCK_MARKER in data[:4096]:
        return "block"
    # Marker may sit past a large preamble of cache cells; scan wider.
    if BLOCK_MARKER in data:
        return "block"
    return "unknown"


# --------------------------------------------------------------------------
# CFB / OLE2
# --------------------------------------------------------------------------


def read_cfb(path: Path, wanted: Callable[[str], bool] | None = None) -> list[Blob]:
    """List every stream of a compound file, in directory order.

    *wanted* filters by name **before** the bytes are read: a caller after
    one stream should not pay for the other thirty. A part's geometry
    lives in one or two streams out of dozens.
    """
    try:
        import olefile
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ContainerError(
            "reading a CFB .SLDPRT needs olefile (pip install olefile)"
        ) from exc

    blobs: list[Blob] = []
    with olefile.OleFileIO(str(path)) as ole:
        for parts in ole.listdir(streams=True, storages=False):
            name = "/".join(parts)
            if wanted is not None and not wanted(name):
                continue
            with ole.openstream(parts) as stream:
                blobs.append(Blob(name=name, data=stream.read(), origin="cfb"))
    return blobs


# --------------------------------------------------------------------------
# Block container
# --------------------------------------------------------------------------


@dataclass
class MarkerSite:
    """A place in the file where the 6-byte block marker occurs."""

    offset: int
    #: ``"block"``, ``"cache_cell"``, ``"tail_entry"`` or ``"unknown"``.
    kind: str
    name: str | None = None
    end: int | None = None
    detail: dict[str, object] = field(default_factory=dict)


def _u32le(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _printable(name: str) -> bool:
    return bool(name) and all(0x20 <= ord(c) < 0x7F for c in name)


def _try_block(data: bytes, off: int, *, key: int = 4) -> MarkerSite | None:
    """Parse a compressed block at ``off``; return None if it does not validate.

    Frame (all little-endian unless noted)::

        +0   marker    bytes[6]
        +6   type_id   u32
        +10  crc32     u32     CRC-32 of the *decompressed* payload
        +14  comp_sz   u32
        +18  uncomp_sz u32
        +22  pre_sz    u32
        +26  preamble  bytes[pre_sz]    nibble-swapped section name
             payload   bytes[comp_sz]   raw DEFLATE (wbits = -15)
    """
    if off + BLOCK_HEADER_SIZE > len(data):
        return None
    type_id = _u32le(data, off + 6)
    crc = _u32le(data, off + 10)
    comp_sz = _u32le(data, off + 14)
    uncomp_sz = _u32le(data, off + 18)
    pre_sz = _u32le(data, off + 22)

    # Cheap rejects before we attempt an inflate.
    if pre_sz > 4096 or comp_sz > len(data) or uncomp_sz > (1 << 31):
        return None
    body = off + BLOCK_HEADER_SIZE
    end = body + pre_sz + comp_sz
    if end > len(data):
        return None

    payload = data[body + pre_sz : end]
    try:
        # Bounded inflate: a hostile stream may hold far more than the
        # header declares, and plain ``zlib.decompress`` would allocate
        # it all before the size check below could reject it. Capped one
        # byte above the declaration, an overrun shows as a length
        # mismatch without the allocation.
        inflater = zlib.decompressobj(-15)
        raw = inflater.decompress(payload, uncomp_sz + 1)
    except zlib.error:
        return None
    if len(raw) != uncomp_sz or not inflater.eof or zlib.crc32(raw) != crc:
        return None

    name = nibble_swap(data[body : body + pre_sz], key=key).decode("latin-1")
    return MarkerSite(
        offset=off,
        kind="block",
        name=name if _printable(name) else None,
        end=end,
        detail={
            "type_id": type_id,
            "comp_sz": comp_sz,
            "uncomp_sz": uncomp_sz,
            "raw": raw,
        },
    )


#: Where the nibble-swapped name sits inside an index entry, relative to the
#: marker. The short form packs it straight after the header; the long form
#: inserts a 14-byte descriptor first. Both occur within a single file — short
#: in the header region, long in the tail directory.
NAME_OFFSETS = (BLOCK_HEADER_SIZE, 40)


def _try_index_entry(data: bytes, off: int, *, key: int = 4) -> MarkerSite | None:
    """Parse an index entry — a named reference to a section, not a payload.

    Shared frame::

        +0   marker    bytes[6]
        +6   type_id   u32 LE
        +10  u32 LE  -+
        +14  u32 LE   |- three size-shaped fields, see below
        +18  u32 LE  -+
        +22  name_len  u32 LE
        +26  name, or a 14-byte descriptor and then the name at +40
             name      bytes[name_len]   nibble-swapped

    The three size fields come in two flavours. In a **cache cell** they are
    redundant scalings of one logical length ``L``: ``2*L``, ``L//2``, ``L``.
    In a **directory entry** they are a checksum, a compressed size and an
    uncompressed size for the section the entry names.

    Both flavours occur at both name offsets, so the name is located by trying
    each and keeping the one that decodes to printable text — rather than by
    assuming a layout this file generation may not use.
    """
    if off + BLOCK_HEADER_SIZE > len(data):
        return None
    type_id = _u32le(data, off + 6)
    first = _u32le(data, off + 10)
    second = _u32le(data, off + 14)
    third = _u32le(data, off + 18)
    name_len = _u32le(data, off + 22)
    if not 0 < name_len < 500:
        return None

    for name_at in NAME_OFFSETS:
        end = off + name_at + name_len
        if end > len(data):
            continue
        name = nibble_swap(data[off + name_at : end], key=key).decode("latin-1")
        if not _printable(name):
            continue
        is_cache = third != 0 and first == 2 * third and second == third // 2
        detail: dict[str, object] = {"name_at": name_at, "type_id": type_id}
        if is_cache:
            detail["L"] = third
        else:
            detail.update(checksum=first, comp_sz=second, uncomp_sz=third)
        return MarkerSite(
            offset=off,
            kind="cache_cell" if is_cache else "dir_entry",
            name=name,
            end=end,
            detail=detail,
        )
    return None


def name_key(data: bytes) -> int:
    """The stream-name rotation key a file declares, or the corpus default.

    A real modern header carries ``00 00 00 <key>`` at bytes 4..7 (measured:
    key 4 on all 24 modern files of the corpus; openswx documents the byte
    as a per-file key). Anything else — a CFB file, a synthetic container
    starting straight at a block marker — falls back to the measured
    default rather than reading a header that is not there.
    """
    if len(data) > 7 and data[4:7] == b"\x00\x00\x00" and data[7]:
        return data[7]
    return 4


def scan_markers(data: bytes) -> list[MarkerSite]:
    """Classify every occurrence of the block marker in ``data``.

    Blocks are parsed and validated (inflate + CRC); index entries are
    recognised structurally. Everything else is reported as ``unknown`` rather
    than silently dropped — an unknown marker is a finding, not noise.
    """
    key = name_key(data)
    sites: list[MarkerSite] = []
    off = data.find(BLOCK_MARKER)
    while off != -1:
        site = (
            _try_block(data, off, key=key)
            or _try_index_entry(data, off, key=key)
            or MarkerSite(offset=off, kind="unknown")
        )
        sites.append(site)
        # A validated block may contain the marker byte pattern in its
        # compressed payload; resume past it. Otherwise step one marker on.
        resume = site.end if site.kind == "block" else off + len(BLOCK_MARKER)
        off = data.find(BLOCK_MARKER, resume)
    return sites


def read_block_container(data: bytes) -> tuple[list[Blob], list[MarkerSite]]:
    """Recover every validated block payload, plus the full marker census."""
    sites = scan_markers(data)
    blobs = [
        Blob(
            name=s.name or f"<unnamed@{s.offset:#x}>",
            data=s.detail["raw"],  # type: ignore[index]
            origin="block",
            offset=s.offset,
            meta={"type_id": s.detail.get("type_id")},
        )
        for s in sites
        if s.kind == "block"
    ]
    return blobs, sites


# --------------------------------------------------------------------------
# Front door
# --------------------------------------------------------------------------


@dataclass
class Container:
    path: Path
    kind: str
    blobs: list[Blob]
    markers: list[MarkerSite] = field(default_factory=list)
    file_size: int = 0

    def get(self, name: str) -> Blob | None:
        for blob in self.blobs:
            if blob.name == name:
                return blob
        return None


def open_container(
    path: str | Path, wanted: Callable[[str], bool] | None = None
) -> Container:
    """Open a ``.SLDPRT`` and return its named blobs, whatever the envelope.

    *wanted* filters streams by name — meaningful for the CFB envelope,
    where each stream is read separately. The block envelope is parsed in
    one pass and ignores it.

    The OLE2 magic fits in the first eight bytes, so a compound file is
    recognised without loading it whole. The block marker can sit
    anywhere, and that envelope is read in one piece regardless.
    """
    path = Path(path)
    with path.open("rb") as handle:
        head = handle.read(len(OLE2_MAGIC))
    if head.startswith(OLE2_MAGIC):
        return Container(
            path, "cfb", read_cfb(path, wanted), file_size=path.stat().st_size
        )

    data = path.read_bytes()
    kind = detect_container(data)
    if kind == "block":
        blobs, markers = read_block_container(data)
        return Container(path, kind, blobs, markers, file_size=len(data))
    raise ContainerError(
        f"{path.name}: neither OLE2 magic nor a block marker found "
        f"(first bytes: {data[:16].hex(' ')})"
    )
