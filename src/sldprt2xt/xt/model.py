# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Turn a decoded node list into a navigable B-rep model.

The node stream is flat: every relation is an index into it. This module
resolves those indices into a graph — bodies own shells, shells own faces,
faces own loops, loops own rings of halfedges — and pulls out the attributes
that carry colour.

Nothing here touches OpenCASCADE. Keeping the traversal separate means the
topology can be inspected, counted and tested without a geometry kernel, and
:mod:`sldprt.occ` has only the translation left to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .binary import Node

#: Parasolid marks a region solid or void with this field. ``V`` is the void
#: surrounding the part; only solid regions become solids.
VOID = "V"

#: Attribute holding a face or body colour, as three doubles in 0..1.
#: SolidWorks writes it with a generation suffix, hence the prefix match.
COLOUR_ATTRIBUTE = "SDL/TYSA_COLOUR"


@dataclass
class Model:
    """A resolved B-rep, indexed by node."""

    nodes: list[Node]
    by_index: dict[int, Node] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.by_index:
            self.by_index = {n.index: n for n in self.nodes}

    def get(self, index: int) -> Node | None:
        """Resolve a pointer. Index 0 and dangling indices both mean nothing."""
        return self.by_index.get(index) if index else None

    def of_type(self, name: str) -> list[Node]:
        return [n for n in self.nodes if n.name == name]

    # -- chains -----------------------------------------------------------

    def chain(self, start: int, link: str = "next") -> list[Node]:
        """Follow a ``next`` chain from ``start``, stopping at a repeat.

        Parasolid links siblings in a list that is sometimes circular. Guarding
        on nodes already seen is what keeps a circular one from spinning.
        """
        out: list[Node] = []
        seen: set[int] = set()
        node = self.get(start)
        while node is not None and node.index not in seen:
            seen.add(node.index)
            out.append(node)
            nxt = node.values.get(link)
            node = self.get(nxt) if isinstance(nxt, int) else None
        return out

    def ring(self, start: int) -> list[Node]:
        """Follow a halfedge ring through ``forward`` until it closes."""
        return self.chain(start, "forward")

    # -- topology ---------------------------------------------------------

    @property
    def bodies(self) -> list[Node]:
        return self.of_type("BODY")

    def shells_of(self, body: Node) -> list[Node]:
        return self.chain(body.values.get("shell", 0))

    def regions_of(self, body: Node) -> list[Node]:
        return self.chain(body.values.get("region", 0))

    def faces_of(self, shell: Node) -> list[Node]:
        return self.chain(shell.values.get("face", 0))

    def loops_of(self, face: Node) -> list[Node]:
        return self.chain(face.values.get("loop", 0))

    def halfedges_of(self, loop: Node) -> list[Node]:
        return self.ring(loop.values.get("halfedge", 0))

    def surface_of(self, face: Node) -> Node | None:
        return self.get(face.values.get("surface", 0))

    def curve_of(self, edge: Node) -> Node | None:
        return self.get(edge.values.get("curve", 0))

    def vertex_point(self, vertex: Node | None) -> list[float] | None:
        if vertex is None:
            return None
        point = self.get(vertex.values.get("point", 0))
        return list(point.values["pvec"]) if point else None

    def is_solid_region(self, region: Node) -> bool:
        kind = region.values.get("type")
        text = chr(kind) if isinstance(kind, int) else str(kind)
        return text != VOID

    def is_solid_body(self, body: Node) -> bool:
        """Whether the body encloses matter, as opposed to being a sheet.

        A body with a region that is not void bounds a volume, so anything
        rebuilt from it must come out a closed solid. A sheet body — a surface
        SolidWorks keeps as construction geometry — legitimately does not.
        """
        return any(self.is_solid_region(r) for r in self.regions_of(body))

    # -- attributes -------------------------------------------------------

    @staticmethod
    def characters(values) -> str:
        """A character run as text, from either reader.

        The binary reader hands back byte values and the text one the
        characters themselves. Accepting only the first silently named every
        attribute of a ``.x_t`` the empty string — which made every colour in
        an exported file invisible — and made reading one back raise.
        """
        if not isinstance(values, list):
            values = [values]
        return "".join(
            chr(c) if isinstance(c, int) else str(c) for c in values if c not in (0, "")
        )

    def _definition_name(self, definition: Node | None) -> str:
        """The name of an attribute definition, from either form of transmit.

        A character run comes back as byte values from the binary reader and
        as the characters themselves from the text one. Accepting only the
        first silently named every attribute of a ``.x_t`` the empty string —
        which made every colour in an exported file invisible, and made
        SolidWorks look as though it kept no named attributes at all.
        """
        if definition is None:
            return ""
        identifier = self.get(definition.values.get("identifier", 0))
        if identifier is None:
            return ""
        return self.characters(identifier.values.get("string", []))

    def attributes_named(self, prefix: str) -> list[Node]:
        """Every ATTRIBUTE whose definition's name starts with ``prefix``."""
        wanted = {
            definition.index
            for definition in self.of_type("ATTRIB_DEF")
            if self._definition_name(definition).startswith(prefix)
        }
        return [
            node
            for node in self.of_type("ATTRIBUTE")
            if node.values.get("definition") in wanted
        ]

    def attribute_reals(self, attribute: Node) -> list[float]:
        """The doubles an attribute carries, across all its value nodes."""
        out: list[float] = []
        fields = attribute.values.get("fields", [])
        for index in fields if isinstance(fields, list) else [fields]:
            node = self.get(index) if isinstance(index, int) else None
            if node is not None and node.name == "REAL_VALUES":
                values = node.values.get("values", [])
                out.extend(values if isinstance(values, list) else [values])
        return out

    def colours(self) -> dict[int, tuple[float, float, float]]:
        """Colour per owning entity index, as RGB in 0..1.

        The owner is whatever the attribute is attached to — a body for a
        whole-part colour, a face for an override. Callers decide which they
        care about by looking up the owner's node type.
        """
        found: dict[int, tuple[float, float, float]] = {}
        for attribute in self.attributes_named(COLOUR_ATTRIBUTE):
            values = self.attribute_reals(attribute)
            if len(values) < 3:
                continue
            owner = attribute.values.get("owner")
            if isinstance(owner, int) and owner:
                found[owner] = (values[0], values[1], values[2])
        return found


def build(nodes: list[Node]) -> Model:
    return Model(nodes=nodes)
