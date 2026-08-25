# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Find and inflate compressed regions inside a container blob.

Two mechanisms are known:

*wrapped sections*
    A 16-byte magic followed by an uncompressed size, a compressed size and
    a zlib member. This is what sits inside ``__ZLB`` compound streams and
    what splits a ``Config-N-Partition`` stream into its transmit sections.

*bare zlib members*
    Everything else. We scan for zlib headers and keep whatever inflates to
    a non-trivial length. This is a discovery aid, deliberately noisy: it is
    for looking at an unfamiliar stream, not for production extraction.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .container import SECTION_MAGIC

ZLIB_HEADERS = (b"\x78\x01", b"\x78\x9c", b"\x78\xda", b"\x78\x5e")


@dataclass
class Region:
    """One inflated region of a blob."""

    #: ``"section"`` (magic-framed) or ``"zlib"`` (bare scan hit).
    kind: str
    #: Offset of the region's frame within the containing blob.
    offset: int
    data: bytes
    #: Fields read from the frame, for the record.
    header: dict[str, int]

    @property
    def size(self) -> int:
        return len(self.data)


def carve_sections(blob: bytes) -> list[Region]:
    """Inflate every magic-framed section found in ``blob``.

    A section is a length-prefixed run of independently compressed members::

        -0x04  u32 LE  section length, counted from the magic
        +0x00  16 B    SECTION_MAGIC
        +0x10  member, member, …, then a (0, 0) header to close the run

    and each member is::

        u32 LE  uncompressed size
        u32 LE  compressed size
        zlib member of exactly that many bytes

    **Members must all be inflated and concatenated.** A small part fits in
    one, which is why a single-member reading appears to work; a large one is
    split into many, and stopping at the first truncates the payload without
    any error — the exact failure this project cannot afford.
    """
    regions: list[Region] = []
    pos = blob.find(SECTION_MAGIC)
    while pos != -1:
        at = pos + len(SECTION_MAGIC)

        # The length prefix bounds the member run. Without it — a magic at the
        # very start of a blob — fall back to the end of the blob and let the
        # member headers say where to stop.
        end = len(blob)
        if pos >= 4:
            declared = struct.unpack_from("<I", blob, pos - 4)[0]
            if 0 < declared <= len(blob) - pos + 4:
                end = min(end, pos - 4 + 4 + declared)

        parts: list[bytes] = []
        members = 0
        while at + 8 <= end:
            uncomp_sz, comp_sz = struct.unpack_from("<II", blob, at)
            if uncomp_sz == 0 and comp_sz == 0:
                at += 8
                break
            if comp_sz == 0 or at + 8 + comp_sz > end:
                break
            try:
                raw = zlib.decompress(blob[at + 8 : at + 8 + comp_sz])
            except zlib.error:
                break
            if len(raw) != uncomp_sz:
                break
            parts.append(raw)
            members += 1
            at += 8 + comp_sz

        if parts:
            regions.append(
                Region(
                    kind="section",
                    offset=pos,
                    data=b"".join(parts),
                    header={
                        "members": members,
                        "uncompressed": sum(len(p) for p in parts),
                        "consumed": at - pos,
                    },
                )
            )
            pos = blob.find(SECTION_MAGIC, at)
            continue
        pos = blob.find(SECTION_MAGIC, pos + 1)
    return regions


def carve_zlib(blob: bytes, min_size: int = 64, limit: int = 64) -> list[Region]:
    """Scan for bare zlib members and inflate them.

    Returns at most ``limit`` regions of at least ``min_size`` inflated bytes.
    Overlapping hits are suppressed: a member found inside an already-inflated
    span is skipped.
    """
    regions: list[Region] = []
    consumed_until = 0
    pos = 0
    while pos < len(blob) and len(regions) < limit:
        nxt = min(
            (p for p in (blob.find(h, pos) for h in ZLIB_HEADERS) if p != -1),
            default=-1,
        )
        if nxt == -1:
            break
        if nxt < consumed_until:
            pos = nxt + 1
            continue
        obj = zlib.decompressobj()
        try:
            raw = obj.decompress(blob[nxt:])
        except zlib.error:
            raw = b""
        if len(raw) >= min_size:
            used = len(blob) - nxt - len(obj.unused_data)
            regions.append(
                Region(
                    kind="zlib",
                    offset=nxt,
                    data=raw,
                    header={
                        "actual_uncompressed": len(raw),
                        "actual_compressed": used,
                    },
                )
            )
            consumed_until = nxt + used
            pos = consumed_until
        else:
            pos = nxt + 1
    return regions


def carve(blob: bytes, *, scan_bare: bool = True) -> list[Region]:
    """Inflate framed sections, then bare zlib members outside them."""
    regions = carve_sections(blob)
    if not scan_bare:
        return regions
    covered = [
        (r.offset, r.offset + r.header.get("consumed", 0)) for r in regions
    ]
    for region in carve_zlib(blob):
        if any(start <= region.offset < end for start, end in covered):
            continue
        regions.append(region)
    return sorted(regions, key=lambda r: r.offset)
