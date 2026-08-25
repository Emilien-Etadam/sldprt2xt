# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Reshape a decoded node graph, from what a partition holds to what a part transmit carries.

A ``.SLDPRT`` stores a Parasolid *partition*: a world, its bodies, and the
bookkeeping SolidWorks keeps for its own rebuilds. A part transmit carries
bodies under an assembly and nothing else. Getting from one to the other is
half a dozen edits to the graph, and they are all the same shape — walk the
nodes, rebuild them with some pointers changed, following the layouts to know
which fields *are* pointers.

That shape is :func:`rebuild`. Everything here goes through it, so the rule
that a pointer field is whatever the layout calls a pointer is stated once.

Every edit here is answerable to :mod:`sldprt.xt.write`'s ``verify``: what
these functions produce is what the writer writes, and reading the file back
must give it again.
"""

from __future__ import annotations

from dataclasses import dataclass

from .binary import Node
from .schema import DOUBLES_PER_TYPE, FieldSpec, Schema
from .text import NULL_DOUBLE

#: Nodes a part transmit does not carry. ``WORLD`` roots a partition, and a
#: part transmit has no world: it starts at its bodies.
PARTITION_ONLY = {"WORLD"}

#: Everything an attribute is made of. A part file carries far more of these
#: than its export does — SolidWorks keeps its own bookkeeping in them — so
#: being able to leave them out turns "the file is refused" into a question
#: with two halves: the geometry, or what is hung off it.
ATTRIBUTE_NODES = {
    "ATTRIB_DEF",
    "ATT_DEF_ID",
    "ATTRIBUTE",
    "INT_VALUES",
    "CHAR_VALUES",
    "REAL_VALUES",
    "UNICODE_VALUES",
}


class WriteError(Exception):
    """The nodes could not be written — reshaped or serialised."""


def pointers(node: Node, layouts: dict[int, list[FieldSpec]]):
    """The pointer fields of *node*, as the layout it was decoded with names them.

    Deriving this from a schema instead would be a different question: a file
    splices its own edits in, and it is those that say what the node holds.
    """
    return [spec for spec in layouts.get(node.node_type, []) if spec.ptr_class]


def rebuild(node: Node, values: dict) -> Node:
    """*node* with new values, everything else carried across."""
    return Node(
        node_type=node.node_type,
        name=node.name,
        index=node.index,
        values=values,
        start=node.start,
        end=node.end,
    )


def remap(
    nodes: list[Node],
    layouts: dict[int, list[FieldSpec]],
    change,
    *,
    skip=lambda node, spec: False,
) -> list[Node]:
    """Every node, with ``change(index, node, spec)`` applied to every pointer.

    *change* is handed one target index at a time and returns what it should
    become; a list-valued field is mapped element by element. *skip* excludes
    a field from the walk — a list block's entries are compacted rather than
    remapped, and zeroing them first would hide from the compaction the very
    entries it has to remove.
    """
    out = []
    for node in nodes:
        values = dict(node.values)
        for spec in pointers(node, layouts):
            if skip(node, spec):
                continue
            value = values.get(spec.name)
            if isinstance(value, list):
                values[spec.name] = [
                    change(v, node, spec) if isinstance(v, int) else v for v in value
                ]
            elif isinstance(value, int):
                values[spec.name] = change(value, node, spec)
        out.append(rebuild(node, values))
    return out


def drop_nodes(
    nodes: list[Node],
    names: set[str],
    layouts: dict[int, list[FieldSpec]],
    *,
    indices: set[int] | None = None,
) -> list[Node]:
    """Remove nodes of these kinds — and these indices — unlinking what pointed at them.

    Only pointer fields are cleared, which the layouts say. Clearing every
    integer that happens to equal a dropped index instead destroys the file:
    a partition's world sits at index 1, and a body's ``state``,
    ``body_type`` and ``nom_geom_state`` are all 1 as well. Zeroing those
    turns a solid into a body of no type at all, and Parasolid refuses the
    result with nothing more than "the data in this file may be invalid".
    """
    dropped = {n.index for n in nodes if n.name in names} | (indices or set())
    if not dropped:
        return nodes

    survivors = [
        node
        for node in nodes
        if node.name not in names and node.index not in dropped
    ]
    return remap(
        survivors, layouts, lambda index, *_: 0 if index in dropped else index
    )


def drop_partition_nodes(
    nodes: list[Node], layouts: dict[int, list[FieldSpec]]
) -> list[Node]:
    """Remove what only a partition carries.

    A part transmit is rooted on its bodies; a partition is rooted on a world
    that a part transmit has no place for.

    The world's transforms go with it. ``loim``'s partition holds nine, every
    one owned by the world; SolidWorks' ``.x_t`` of that part holds
    seventy-two, every one owned by an instance and not one owned by anything
    else. Dropping the world alone left those nine behind with an owner of
    zero — a transform belonging to nothing, which is not a thing a part
    transmit contains. See FORMAT.md F-49.
    """
    world = {node.index for node in nodes if node.name in PARTITION_ONLY}
    return drop_nodes(
        nodes,
        PARTITION_ONLY,
        layouts,
        indices={
            node.index
            for node in nodes
            if node.name == "TRANSFORM" and node.values.get("owner") in world
        },
    )


#: The value of ``BODY.body_type`` for a solid. Anything else is a sheet, a
#: wire or a general body — geometry a part file legitimately holds and that
#: SolidWorks does not put in its own ``.x_t``.
SOLID_BODY = 1


def select_bodies(
    nodes: list[Node],
    layouts: dict[int, list[FieldSpec]],
    keep: set[int] | None = None,
    *,
    solids_only: bool = False,
) -> list[Node]:
    """Keep the named bodies and everything they reach, drop the rest.

    *keep* names bodies by index; ``None`` keeps them all. ``solids_only``
    additionally drops every body whose ``body_type`` is not
    :data:`SOLID_BODY` — which is what SolidWorks itself exports: the fermoir
    holds twelve bodies and its ``.x_t`` carries the eight solids.

    Reachability is followed through the pointer fields the layouts name, so
    a body takes its shells, faces, curves and attributes with it. The
    surviving bodies are re-chained through ``next`` and ``previous`` in the
    order they appeared; a chain that still pointed at a dropped body would
    name a node the file no longer holds.

    **Traversal stops at any other body.** A body's subgraph does reach its
    neighbours — geometry shared between two bodies is owned by a
    ``GEOMETRIC_OWNER`` that names both — so walking straight through would
    drag a neighbour's faces and curves in behind it, without the body that
    owns them. Every pointer that led out is then cleared, because the file
    must not name a node it does not carry: what this returns is
    self-contained or it is nothing. See FORMAT.md F-48.
    """
    bodies = [n for n in nodes if n.name == "BODY"]
    survivors = [
        body
        for body in bodies
        if (keep is None or body.index in keep)
        and (not solids_only or body.values.get("body_type") == SOLID_BODY)
    ]
    if len(survivors) == len(bodies):
        return nodes
    if not survivors:
        raise WriteError("no body left to transmit")

    by_index = {node.index: node for node in nodes}
    dropped_bodies = {b.index for b in bodies} - {b.index for b in survivors}
    wanted = {body.index for body in survivors}
    frontier = list(wanted)
    while frontier:
        node = by_index.get(frontier.pop())
        if node is None:
            continue
        for spec in pointers(node, layouts):
            # A body's chain is rewired below; following it here would drag
            # every other body back in through the back door.
            if node.name == "BODY" and spec.name in ("next", "previous"):
                continue
            value = node.values.get(spec.name)
            for target in value if isinstance(value, list) else [value]:
                if not isinstance(target, int) or not target:
                    continue
                if target in dropped_bodies or target in wanted:
                    continue
                wanted.add(target)
                frontier.append(target)

    chain = {body.index: i for i, body in enumerate(survivors)}
    kept = remap(
        [node for node in nodes if node.index in wanted],
        layouts,
        lambda index, *_: index if index in wanted or not index else 0,
    )
    for at, node in enumerate(kept):
        if node.name != "BODY":
            continue
        position = chain[node.index]
        kept[at] = rebuild(
            node,
            dict(
                node.values,
                previous=survivors[position - 1].index if position else 0,
                next=(
                    survivors[position + 1].index
                    if position + 1 < len(survivors)
                    else 0
                ),
            ),
        )
    return kept


#: What ``BODY.nom_geom_state`` reads when a body carries nominal geometry.
#: A partition may hold such a body; SolidWorks' own ``.x_t`` never does.
NOMINAL_GEOMETRY = 2
RESOLVED_GEOMETRY = 1


def resolve_nominal(nodes: list[Node]) -> list[Node]:
    """Declare every body's geometry resolved, as an export does.

    ``loim`` holds one solid whose ``nom_geom_state`` is
    :data:`NOMINAL_GEOMETRY`. SolidWorks' ``.x_t`` of that same part carries
    the same body — 61 faces, matched by face count — with the state written
    as :data:`RESOLVED_GEOMETRY`. Every body of every ground-truth export in
    the corpus reads 1.

    This changes a declaration, not geometry: the faces, surfaces and curves
    written are the ones the file holds either way. What it asserts is that
    they are the final shapes rather than stand-ins — which is what the
    modeller asserts when it exports the same body.
    """
    changed = []
    for node in nodes:
        if node.name != "BODY" or node.values.get("nom_geom_state") != NOMINAL_GEOMETRY:
            changed.append(node)
            continue
        values = dict(node.values)
        values["nom_geom_state"] = RESOLVED_GEOMETRY
        changed.append(
            Node(
                node_type=node.node_type,
                name=node.name,
                index=node.index,
                values=values,
                start=node.start,
                end=node.end,
            )
        )
    return changed


#: Node types of Parasolid's part structure, absent from a SolidWorks
#: partition because a partition holds bodies directly.
ASSEMBLY_TYPE = 10
INSTANCE_TYPE = 11
TRANSFORM_TYPE = 100

#: ``ASSEMBLY.type`` and ``INSTANCE.type`` for a solid part.
PART_TYPE_SOLID = 1


def _blank(spec: FieldSpec):
    """The value of a field that is set to nothing.

    Zero for scalars and for reals alike: a transform's translation is three
    zeros, not three nulls. The one field that is genuinely unset —
    ``perspective_vector`` — is handled by its caller.
    """
    count = (spec.n_elts or 1) * DOUBLES_PER_TYPE.get(spec.type, 1)
    if spec.type in DOUBLES_PER_TYPE:
        return [0.0] * count if count > 1 or spec.type != "f" else 0.0
    return [0] * count if count > 1 else 0


def _synthesise(node_type: int, index: int, fields: list[FieldSpec], **values) -> Node:
    """One node of *node_type*, every field written, only *values* set."""
    filled = {spec.name: _blank(spec) for spec in fields}
    filled.update(values)
    return Node(
        node_type=node_type,
        name={
            ASSEMBLY_TYPE: "ASSEMBLY",
            INSTANCE_TYPE: "INSTANCE",
            TRANSFORM_TYPE: "TRANSFORM",
        }[node_type],
        index=index,
        values=filled,
    )


def wrap_in_assembly(
    nodes: list[Node],
    layouts: dict[int, list[FieldSpec]],
    current: Schema | None,
    *,
    least: int = 1,
) -> list[Node]:
    """Root the bodies on an assembly, the way SolidWorks does.

    A part transmit carrying two or more bodies chained through ``next`` is
    **refused**: the fermoir's twelve bodies so chained do not import, and one
    of them alone does. SolidWorks' own ``.x_t`` of that part answers the
    question — an ``ASSEMBLY`` at index 1, one ``INSTANCE`` per body, one
    identity ``TRANSFORM`` per instance. See FORMAT.md F-47.

    **One body gets one too**, which is why *least* defaults to 1. A lone body
    is not always refused — SolidWorks' own ``cube.x_t`` is one body with no
    assembly and imports — but a body *cut out of* a larger part can be, and
    the same body wrapped is accepted. Wrapping unconditionally is measured
    safe: the cube written this way still imports. See Q-18. Pass ``least=2``
    to leave a single body untouched.

    The layouts of those three types come from the *current* schema, which is
    what the modeller writing the file would use, and they match SolidWorks'
    splices field for field.
    """
    bodies = [n for n in nodes if n.name == "BODY"]
    if len(bodies) < least:
        return nodes

    for node_type in (ASSEMBLY_TYPE, INSTANCE_TYPE, TRANSFORM_TYPE):
        if node_type in layouts:
            continue
        # Sans schéma de version, ces trois dispositions sont introuvables :
        # la partition n'a aucun nœud de ces types, donc rien à épisser, et
        # le schéma de base ne peut pas s'y substituer — d'un schéma à
        # l'autre ASSEMBLY passe de 17 à 21 champs et TRANSFORM de 9 à 10.
        # Renvoyer les corps non enveloppés donnerait un fichier que
        # Parasolid refuse (F-47) sans que rien ne l'ait dit : c'est le
        # silence qu'on refuse ici, pas l'échec.
        if current is None:
            raise WriteError(
                f"{len(bodies)} corps à enraciner sur un ASSEMBLY, et le "
                f"schéma de la version manque : le type {node_type} n'est "
                "pas dans la partition, sa disposition ne peut venir que du "
                "schéma de la version du fichier (sch_<version>.s_t, livré "
                "avec le logiciel qui a écrit la pièce). Déposez ce schéma, "
                "ou renoncez à l'enveloppe — Selection(assembly=False), "
                "--no-assembly en ligne de commande — en connaissance de "
                "cause (F-47)"
            )
        spec = next(
            (n for n in current.nodes.values() if n.node_type == node_type), None
        )
        if spec is None:
            raise WriteError(
                f"schema {current.key} declares no node type {node_type}"
            )
        layouts[node_type] = list(spec.effective_fields)

    free = max(n.index for n in nodes) + 1
    assembly_index = free
    instances = {body.index: free + 1 + 2 * i for i, body in enumerate(bodies)}
    transforms = {body.index: free + 2 + 2 * i for i, body in enumerate(bodies)}

    # Node ids run downwards, odd for the instance and even for its transform,
    # so the last body's instance carries 1. That is the order SolidWorks
    # writes and there is no reason to invent another.
    count = len(bodies)
    ids = {body.index: 2 * (count - i) - 1 for i, body in enumerate(bodies)}
    positions = {body.index: rank for rank, body in enumerate(bodies)}

    assembly = _synthesise(
        ASSEMBLY_TYPE,
        assembly_index,
        layouts[ASSEMBLY_TYPE],
        highest_node_id=2 * count + 1,
        res_size=bodies[0].values.get("res_size", 1000.0),
        res_linear=bodies[0].values.get("res_linear", 1e-8),
        state=1,
        type=PART_TYPE_SOLID,
        sub_instance=instances[bodies[0].index],
    )

    out: list[Node] = [assembly]
    for node in nodes:
        if node.name != "BODY":
            out.append(node)
            continue
        at = positions[node.index]
        out.append(
            _synthesise(
                INSTANCE_TYPE,
                instances[node.index],
                layouts[INSTANCE_TYPE],
                node_id=ids[node.index],
                type=PART_TYPE_SOLID,
                part=node.index,
                transform=transforms[node.index],
                assembly=assembly_index,
                next_in_part=(
                    instances[bodies[at + 1].index] if at + 1 < count else 0
                ),
                prev_in_part=instances[bodies[at - 1].index] if at else 0,
            )
        )
        values = dict(node.values)
        values["ref_instance"] = instances[node.index]
        out.append(
            Node(
                node_type=node.node_type,
                name=node.name,
                index=node.index,
                values=values,
                start=node.start,
                end=node.end,
            )
        )
        out.append(
            _synthesise(
                TRANSFORM_TYPE,
                transforms[node.index],
                layouts[TRANSFORM_TYPE],
                node_id=ids[node.index] + 1,
                owner=instances[node.index],
                rotation_matrix=[1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0],
                scale=1.0,
                perspective_vector=[NULL_DOUBLE] * 3,
            )
        )
    return out


def renumber(nodes: list[Node], layouts: dict[int, list[FieldSpec]]) -> list[Node]:
    """Give the nodes indices 1..N in order, and follow every reference.

    A reference to an index no node carries is **not** a mistake: a file
    Parasolid wrote has ten of them, ``BODY.next`` among them, naming entities
    it did not transmit. Mapping those to zero says something different — that
    there is nothing there — and a file so altered is refused where the
    original was accepted. So each one keeps a distinct index of its own,
    placed past the last node, where it still names nothing in the file.
    """
    mapping = {node.index: i for i, node in enumerate(nodes, start=1)}
    outside = len(nodes)

    def target(index: int) -> int:
        nonlocal outside
        if index == 0:
            return 0
        if index not in mapping:
            outside += 1
            mapping[index] = outside
        return mapping[index]

    return [
        Node(
            node_type=node.node_type,
            name=node.name,
            index=mapping[node.index],
            values=followed.values,
            start=node.start,
            end=node.end,
        )
        for node, followed in zip(
            nodes, remap(nodes, layouts, lambda index, *_: target(index)), strict=False
        )
    ]


#: Fields chaining attributes, as (forward, backward) pairs. An attribute sits
#: in two lists at once: the one its owner keeps, and the one its definition
#: keeps.
_ATTRIBUTE_CHAINS = (("next", "previous"), ("next_of_type", "previous_of_type"))


def drop_attributes(
    nodes: list[Node], layouts: dict[int, list[FieldSpec]], names: set[str]
) -> list[Node]:
    """Remove the attributes of these definitions, and heal what held them."""
    from .model import Model

    model = Model(nodes=nodes)
    wanted = {
        definition.index
        for definition in model.of_type("ATTRIB_DEF")
        if model._definition_name(definition) in names
    }
    if not wanted:
        return nodes
    return drop_attribute_nodes(
        nodes,
        layouts,
        {
            node.index
            for node in nodes
            if node.name == "ATTRIBUTE" and node.values.get("definition") in wanted
        },
    )


def drop_attribute_nodes(
    nodes: list[Node], layouts: dict[int, list[FieldSpec]], doomed: set[int]
) -> list[Node]:
    """Remove these attributes by index, and heal what held them.

    Not the same operation as dropping every attribute. An attribute sits in
    two doubly-linked chains and is counted by a ``LIST`` its owner keeps, so
    taking one out means stitching both chains back together, moving whatever
    heads pointed at it, and shortening the list. Leave any of that undone and
    the file states a count it does not have — which, measured, brings
    SolidWorks down rather than merely being refused. See FORMAT.md F-50.

    Value nodes go with their attribute unless another still names them.

    Taking them by index rather than by definition is what makes an attribute
    bisectable: when the culprit is one attribute among two hundred sharing a
    dozen definitions, dropping by kind cannot separate them.
    """
    by_index = {node.index: node for node in nodes}
    doomed = {i for i in doomed if i in by_index and by_index[i].name == "ATTRIBUTE"}
    if not doomed:
        return nodes

    # Value nodes, unless a surviving attribute still names them.
    spoken_for: set[int] = set()
    for node in nodes:
        if node.name != "ATTRIBUTE" or node.index in doomed:
            continue
        fields = node.values.get("fields")
        for target in fields if isinstance(fields, list) else [fields]:
            if isinstance(target, int) and target:
                spoken_for.add(target)
    values: set[int] = set()
    for index in doomed:
        fields = by_index[index].values.get("fields")
        for target in fields if isinstance(fields, list) else [fields]:
            if isinstance(target, int) and target and target not in spoken_for:
                values.add(target)

    def survivor(index: int, direction: str) -> int:
        """Follow *direction* past the doomed, to the first attribute that stays."""
        seen = set()
        while index in doomed and index not in seen:
            seen.add(index)
            index = by_index[index].values.get(direction) or 0
        return index if index not in doomed else 0

    links = {name for pair in _ATTRIBUTE_CHAINS for name in pair}

    def heal(index: int, node: Node, spec: FieldSpec) -> int:
        if index in values:
            return 0
        if index not in doomed:
            return index
        # A chain link keeps going the way it pointed; anything else is a
        # head, and a head moves onto the first attribute that survives.
        return survivor(index, spec.name if spec.name in links else "next")

    healed = remap(
        [n for n in nodes if n.index not in doomed and n.index not in values],
        layouts,
        heal,
        # A list block's entries are compacted, not cleared: zeroing them
        # first would hide from the compaction the very entries it removes,
        # and the count would stay above what the block holds.
        skip=lambda node, spec: node.name == "POINTER_LIS_BLOCK"
        and spec.name == "entries",
    )
    return _compact_lists(healed, doomed)


def _compact_lists(nodes: list[Node], removed: set[int]) -> list[Node]:
    """Shorten every attribute list by what was taken out of it.

    The entries are a fixed-width array padded with zeros, so an entry is not
    cleared but deleted: the rest shifts down and the count follows.
    """
    shrunk: dict[int, int] = {}
    out = []
    for node in nodes:
        if node.name != "POINTER_LIS_BLOCK":
            out.append(node)
            continue
        entries = node.values.get("entries")
        if not isinstance(entries, list):
            out.append(node)
            continue
        kept = [e for e in entries if not (isinstance(e, int) and e in removed)]
        gone = len(entries) - len(kept)
        if not gone:
            out.append(node)
            continue
        values = dict(node.values)
        values["entries"] = kept + [0] * gone
        values["n_entries"] = max(0, node.values.get("n_entries", 0) - gone)
        shrunk[node.index] = gone
        out.append(
            Node(
                node_type=node.node_type,
                name=node.name,
                index=node.index,
                values=values,
                start=node.start,
                end=node.end,
            )
        )

    if not shrunk:
        return out

    final = []
    for node in out:
        if node.name != "LIST":
            final.append(node)
            continue
        gone = 0
        block = node.values.get("list_block")
        seen = set()
        while isinstance(block, int) and block and block not in seen:
            seen.add(block)
            gone += shrunk.get(block, 0)
            block = next(
                (n.values.get("next_block") for n in out if n.index == block), 0
            )
        if not gone:
            final.append(node)
            continue
        values = dict(node.values)
        values["list_length"] = max(0, node.values.get("list_length", 0) - gone)
        final.append(
            Node(
                node_type=node.node_type,
                name=node.name,
                index=node.index,
                values=values,
                start=node.start,
                end=node.end,
            )
        )
    return final


def _repack_chained_lists(nodes: list[Node]) -> list[Node]:
    """Refill every chained pointer list so no block gapes mid-chain.

    Editing a list — an entry dropped here, another appended there — can
    leave a block part-full in the middle of its chain: the colour of face
    lands in the tail block before the private colour is carved out of the
    head. The totals still agree, but a Parasolid reader finds entry *k* by
    arithmetic — block ``k // width``, slot ``k % width`` — and where the
    head gapes it dereferences a zero. SolidWorks then refuses the whole
    transmit. Measured on loim's body 222726, the only chained list of the
    corpus: head at 19 of 20, tail at 2. Blocks are refilled to the brim in
    chain order, the counts follow, and the finger goes back on the last
    entry, where the file's own lists keep it.
    """
    by_index = {n.index: n for n in nodes}
    fixed: dict[int, Node] = {}
    for node in nodes:
        if node.name != "LIST":
            continue
        first = node.values.get("list_block")
        if not (isinstance(first, int) and first in by_index):
            continue
        chain = []
        block = first
        seen: set[int] = set()
        while (
            isinstance(block, int)
            and block in by_index
            and block not in seen
            and by_index[block].name == "POINTER_LIS_BLOCK"
        ):
            seen.add(block)
            chain.append(by_index[block])
            block = by_index[block].values.get("next_block")
        if len(chain) < 2:
            continue  # un bloc seul ne peut pas bâiller en milieu de chaîne
        entries: list = []
        for b in chain:
            used = b.values.get("n_entries") or 0
            entries.extend((b.values.get("entries") or [])[:used])
        width = len(chain[0].values.get("entries") or []) or 1
        at = 0
        for b in chain:
            take = entries[at : at + width]
            at += len(take)
            values = dict(b.values)
            values["entries"] = list(take) + [0] * (width - len(take))
            values["n_entries"] = len(take)
            fixed[b.index] = rebuild(b, values)
        values = dict(node.values)
        values["list_length"] = len(entries)
        if entries:
            values["finger_index"] = len(entries)
            holder = min((len(entries) - 1) // width, len(chain) - 1)
            values["finger_block"] = chain[holder].index
        fixed[node.index] = rebuild(node, values)
    if not fixed:
        return nodes
    return [fixed.get(n.index, n) for n in nodes]


def _attribute_lists(nodes: list[Node]) -> set[int]:
    """The lists that index a body's attributes, and the blocks holding them.

    A body's ``attribute_chains`` names a ``LIST`` that declares how many
    entries it has, and ``POINTER_LIS_BLOCK``s carrying them. Drop the
    attributes and leave those behind and the file states fifteen entries
    above fifteen zeros — which does not merely fail to open, it takes
    SolidWorks down with it. Whatever indexes nothing has to go too.
    """
    by_index = {node.index: node for node in nodes}
    doomed: set[int] = set()
    for node in nodes:
        chain = node.values.get("attribute_chains")
        while isinstance(chain, int) and chain in by_index and chain not in doomed:
            listing = by_index[chain]
            if listing.name != "LIST":
                break
            doomed.add(chain)
            for key in ("finger_block", "list_block"):
                block = listing.values.get(key)
                while (
                    isinstance(block, int)
                    and block in by_index
                    and block not in doomed
                    and by_index[block].name == "POINTER_LIS_BLOCK"
                ):
                    doomed.add(block)
                    block = by_index[block].values.get("next_block")
            chain = listing.values.get("next")
    return doomed



@dataclass(frozen=True)
class Selection:
    """What goes into a part transmit, and what is left in the partition.

    One object rather than nine parameters. Every one of these was added while
    chasing a file SolidWorks refused, and each addition used to mean editing
    five signatures — ``prepare``, ``write_transmit``, ``verify``,
    ``to_part_transmit`` and the command line — because the same list was
    spelled out at every level. Adding the next one now means adding a field.

    ``assembly`` needs the current schema because the ``ASSEMBLY``,
    ``INSTANCE`` and ``TRANSFORM`` layouts are not in the partition: nothing
    in a partition is of those types. See F-47. Without it, one whole body
    ships unwrapped — the shape SolidWorks' own single-body export has, and
    it imports (Q-18) — while two or more bodies, or a body cut out of a
    larger part, raise: :func:`wrap_in_assembly` used to pass its turn
    there, and hand back a file Parasolid refuses without a word.
    """

    #: Keep only these bodies, by index. ``None`` keeps them all.
    bodies: frozenset[int] | None = None
    #: Drop every body that is not a solid, as SolidWorks' own export does.
    solids_only: bool = False
    #: Keep the attributes. Turning this off is how "the file is refused"
    #: becomes a question with two halves: the geometry, or what hangs off it.
    attributes: bool = True
    #: Drop the attributes of these definitions, by name, healing the chains.
    without_attributes: frozenset[str] = frozenset()
    #: Drop these attributes by index. What ``without_attributes`` cannot do:
    #: separate one attribute from the two hundred sharing its definition.
    without_attribute_nodes: frozenset[int] = frozenset()
    #: Declare every body's geometry resolved, as an export does. See F-49.
    nominal_as_resolved: bool = False
    #: Root several bodies on an assembly. Required by Parasolid — see F-47.
    assembly: bool = True
    #: Give every face the standard colour attribute, as an export does.
    #: Without it a transmit written from a partition opens grey — see F-52.
    face_colours: bool = True

    def apply(
        self,
        nodes: list[Node],
        layouts: dict[int, list[FieldSpec]],
        current: Schema | None = None,
    ) -> list[Node]:
        """The nodes a part transmit carries, in order, still at their own indices.

        One method, so that the writer and its verification cannot disagree
        about what was meant to be in the file.
        """
        kept = drop_partition_nodes(nodes, layouts)
        if self.without_attributes:
            kept = drop_attributes(kept, layouts, set(self.without_attributes))
        if self.without_attribute_nodes:
            kept = drop_attribute_nodes(
                kept, layouts, set(self.without_attribute_nodes)
            )
        if not self.attributes:
            kept = drop_nodes(
                kept, ATTRIBUTE_NODES, layouts, indices=_attribute_lists(kept)
            )
        if self.bodies is not None or self.solids_only:
            kept = select_bodies(
                kept,
                layouts,
                set(self.bodies) if self.bodies is not None else None,
                solids_only=self.solids_only,
            )
        if self.nominal_as_resolved:
            kept = resolve_nominal(kept)
        if self.face_colours:
            kept = spread_colours_to_faces(kept, layouts)
            # SolidWorks' own export carries none of these: what it states
            # privately per body is now stated per face, in the open.
            kept = drop_attributes(kept, layouts, {PRIVATE_COLOUR_ATTRIBUTE})
        if self.assembly:
            # Sans `current`, l'enveloppe est infabricable. Un corps unique
            # et entier s'en passe — l'export mono-corps de SolidWorks n'en
            # a pas et s'importe (Q-18) — donc `least=2` le laisse passer.
            # Dès deux corps, ou pour un corps découpé d'une pièce plus
            # grande (Q-18 encore : celui-là peut être refusé nu), ceci lève
            # au lieu de passer son tour : un transmit multi-corps sans
            # assemblage est refusé par Parasolid, et `verify` ne peut pas
            # le voir — il compare l'écrivain à cette méthode-ci, pas aux
            # règles de Parasolid.
            cut_out = self.bodies is not None or self.solids_only
            least = 1 if (current is not None or cut_out) else 2
            kept = wrap_in_assembly(kept, layouts, current, least=least)
        # L'édition peut laisser un bloc entamé au milieu d'une chaîne ;
        # le lecteur Parasolid lit les listes par arithmétique de blocs
        # pleins, alors on retasse en dernier geste.
        return _repack_chained_lists(kept)



#: Parasolid's own colour attribute — three reals in 0..1, owned by a face.
#: The cube's partition and SolidWorks' ``.x_t`` of it declare it identically,
#: which is why the constants below are read off rather than chosen.
COLOUR_ATTRIBUTE = "SDL/TYSA_COLOUR"
COLOUR_TYPE_ID = 8001
COLOUR_LEGAL_OWNERS = (0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

#: SolidWorks' private per-body colour, type id 8040. Its own export never
#: carries it: the body's colour is spread over its faces as
#: :data:`COLOUR_ATTRIBUTE` instead.
PRIVATE_COLOUR_ATTRIBUTE = "SDL/TYSA_COLOUR_2"


class _Chains:
    """The bookkeeping an attribute lives in, and how to add one to it.

    An attribute is not a value hung on a face. It sits in the face's own
    chain, in a second chain of every attribute of its kind within its body,
    and is counted by a list the body keeps with one entry per kind. Adding
    one means all three, and getting any of them wrong gives a file that
    states a count it does not have — measured, that brings SolidWorks down
    rather than merely being refused. See F-50.
    """

    def __init__(self, nodes: list[Node]) -> None:
        self.by_index = {node.index: node for node in nodes}
        self.changed: dict[int, dict] = {}
        self.born: list[Node] = []
        self._next = max((node.index for node in nodes), default=0) + 1

    def free(self) -> int:
        """An index no node carries."""
        self._next += 1
        return self._next - 1

    def add(self, node: Node) -> None:
        self.by_index[node.index] = node
        self.born.append(node)

    def value(self, index: int, field: str):
        if index in self.changed and field in self.changed[index]:
            return self.changed[index][field]
        node = self.by_index.get(index)
        return node.values.get(field) if node else None

    def set(self, index: int, field: str, value) -> None:
        self.changed.setdefault(index, {})[field] = value

    def tail(self, head, field: str = "next") -> int:
        """The last node of a chain, or 0 when it is empty."""
        seen: set[int] = set()
        while isinstance(head, int) and head in self.by_index and head not in seen:
            seen.add(head)
            following = self.value(head, field)
            if not isinstance(following, int) or following not in self.by_index:
                return head
            head = following
        return 0

    def applied(self, nodes: list[Node]) -> list[Node]:
        out = []
        for node in nodes:
            change = self.changed.get(node.index)
            if change:
                values = dict(node.values)
                values.update(change)
                node = rebuild(node, values)
            out.append(node)
        return out + self.born


def _note_in_list(chains: _Chains, body: Node, head: int) -> None:
    """Record one more attribute kind in the body's list of them.

    The list holds **one entry per definition**, pointing at the first
    attribute of that kind — ten entries for the cube's ten definitions,
    measured. A kind the body did not have before therefore adds an entry,
    and adding attributes of a kind it already had does not.
    """
    listing = chains.value(body.index, "attribute_chains")
    if not isinstance(listing, int) or listing not in chains.by_index:
        raise WriteError(f"body {body.index} keeps no list of its attributes")

    block = chains.value(listing, "list_block")
    last = 0
    while isinstance(block, int) and block in chains.by_index:
        entries = list(chains.value(block, "entries") or [])
        used = chains.value(block, "n_entries") or 0
        if used < len(entries):
            entries[used] = head
            chains.set(block, "entries", entries)
            chains.set(block, "n_entries", used + 1)
            chains.set(
                listing, "list_length", (chains.value(listing, "list_length") or 0) + 1
            )
            return
        last = block
        block = chains.value(block, "next_block")

    # Every block full: the list grows by one more, which is why a list has a
    # chain of blocks at all. ``loim`` has a body with two of them.
    if not last:
        raise WriteError(f"body {body.index} keeps no block of attribute kinds")
    template = chains.by_index[last]
    width = len(template.values.get("entries") or [])
    fresh = chains.free()
    chains.add(
        Node(
            node_type=template.node_type,
            name=template.name,
            index=fresh,
            values={
                "n_entries": 1,
                "index_map_offset": 0,
                "next_block": 0,
                "entries": [head] + [0] * (width - 1),
            },
        )
    )
    chains.set(last, "next_block", fresh)
    chains.set(listing, "list_length", (chains.value(listing, "list_length") or 0) + 1)


def spread_colours_to_faces(
    nodes: list[Node], layouts: dict[int, list[FieldSpec]]
) -> list[Node]:
    """Give every face the colour a Parasolid consumer reads.

    A SolidWorks partition states a body's colour once, as its private
    ``SDL/TYSA_COLOUR_2``, and only a face that *derogates* carries the
    standard ``SDL/TYSA_COLOUR``. Nothing outside SolidWorks reads the private
    one, so a transmit written straight from a partition opens grey.

    SolidWorks' own export answers it plainly: ``loim``'s partition holds 73
    private colours, one per body, and its ``.x_t`` holds 6 358 standard ones
    — **one per face**. This does the same. Each face keeps its own colour
    where it states one and takes its body's otherwise.

    Nothing here is invented: the definition is the partition's own where it
    has one, and is built from the private one otherwise, keeping the type id
    and legal owners that the partition and the export both state. See
    FORMAT.md F-52.
    """
    from .model import Model

    model = Model(nodes=nodes)
    chains = _Chains(nodes)
    by_index = chains.by_index
    names = {d.index: model._definition_name(d) for d in model.of_type("ATTRIB_DEF")}
    standard = next((i for i, n in names.items() if n == COLOUR_ATTRIBUTE), None)
    private = next(
        (i for i, n in names.items() if n == PRIVATE_COLOUR_ATTRIBUTE), None
    )

    stated: dict[int, list[float]] = {}
    for attribute in model.of_type("ATTRIBUTE"):
        if names.get(attribute.values.get("definition")) not in (
            COLOUR_ATTRIBUTE,
            PRIVATE_COLOUR_ATTRIBUTE,
        ):
            continue
        values = model.attribute_reals(attribute)
        owner = attribute.values.get("owner")
        if len(values) >= 3 and isinstance(owner, int) and owner:
            stated.setdefault(owner, values[:3])

    # A face that already carries the standard attribute is left alone: it
    # says what it is, and saying it twice is not the same file.
    settled = {
        attribute.values.get("owner")
        for attribute in model.of_type("ATTRIBUTE")
        if names.get(attribute.values.get("definition")) == COLOUR_ATTRIBUTE
    }

    work: list[tuple[Node, list[tuple[Node, list[float]]]]] = []
    for body in model.bodies:
        colour = stated.get(body.index)
        faces = []
        for shell in model.shells_of(body):
            for face in model.faces_of(shell):
                paint = stated.get(face.index, colour)
                if paint is not None and face.index not in settled:
                    faces.append((face, paint))
        if faces:
            work.append((body, faces))
    if not work:
        return nodes

    template = by_index.get(standard if standard is not None else private)
    if template is None:
        raise WriteError("the part states no colour to spread")
    reals = next((n for n in nodes if n.name == "REAL_VALUES"), None)
    kind = next((n for n in nodes if n.name == "ATTRIBUTE"), None)
    if reals is None or kind is None:
        raise WriteError("the part carries no attribute to model a colour on")

    if standard is None:
        identifier = by_index.get(template.values.get("identifier"))
        if identifier is None:
            raise WriteError("the colour definition names no identifier")
        standard, name_index = chains.free(), chains.free()
        chains.add(
            Node(
                node_type=identifier.node_type,
                name=identifier.name,
                index=name_index,
                values={"string": [ord(c) for c in COLOUR_ATTRIBUTE]},
            )
        )
        values = dict(template.values)
        values.update(
            identifier=name_index,
            type_id=COLOUR_TYPE_ID,
            legal_owners=list(COLOUR_LEGAL_OWNERS),
            next=0,
        )
        chains.add(
            Node(
                node_type=template.node_type,
                name=template.name,
                index=standard,
                values=values,
            )
        )
        # Joined at the tail of the definition chain, where nothing else has
        # to be rewritten: whatever holds the head still holds it.
        last = chains.tail(min(names) if names else 0)
        if last:
            chains.set(last, "next", standard)

    for body, faces in work:
        first = 0
        earlier = 0
        for face, paint in faces:
            value_index, attribute_index = chains.free(), chains.free()
            chains.add(
                Node(
                    node_type=reals.node_type,
                    name=reals.name,
                    index=value_index,
                    values={"values": list(paint)},
                )
            )

            node_id = (chains.value(body.index, "highest_node_id") or 0) + 1
            chains.set(body.index, "highest_node_id", node_id)

            tail = chains.tail(chains.value(face.index, "attributes_features"))
            attribute = Node(
                node_type=kind.node_type,
                name=kind.name,
                index=attribute_index,
                values={
                    "node_id": node_id,
                    "definition": standard,
                    "owner": face.index,
                    "next": 0,
                    "previous": tail,
                    "next_of_type": 0,
                    "previous_of_type": earlier,
                    "fields": value_index,
                },
            )
            chains.add(attribute)

            if tail:
                chains.set(tail, "next", attribute_index)
            else:
                chains.set(face.index, "attributes_features", attribute_index)
            if earlier:
                chains.set(earlier, "next_of_type", attribute_index)
            else:
                first = attribute_index
            earlier = attribute_index

        _note_in_list(chains, body, first)

    return chains.applied(nodes)
