# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Parasolid XT: the schema, and the node stream it decodes.

An XT transmit is a flat list of *nodes*. Each node opens with its type
number; what follows is only meaningful given the **schema** for that type —
the table saying which fields it carries and in what order. Without the
schema, an XT transmit is an undifferentiated run of bytes.

    schema.py   read a ``sch_NNNNN.s_t`` schema file
    text.py     read a text transmit (``.x_t``) — our ground truth
    binary.py   read the neutral-binary transmit SolidWorks embeds
"""

from .schema import FieldSpec, NodeSpec, Schema, load_schema, parse_schema

__all__ = ["FieldSpec", "NodeSpec", "Schema", "load_schema", "parse_schema"]
