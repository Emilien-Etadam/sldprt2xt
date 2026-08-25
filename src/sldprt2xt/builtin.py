# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Les schémas que le paquet porte en lui — l'autonomie.

Trois sources, du plus sûr au plus souple :

- :func:`base_schema` — le schéma de base 13006, bâti depuis
  :mod:`sldprt2xt.schema_facts`. Suffit à décoder n'importe quel transmit :
  le fichier épisse lui-même la disposition de chaque type de nœud.
- :func:`envelope_for` — les dispositions ASSEMBLY/INSTANCE/TRANSFORM de la
  version demandée, si cette version est dans la table. C'est ce que
  l'écriture multi-corps exige et que rien dans une partition ne porte.
- :func:`envelope_from_x_t` — les mêmes dispositions, apprises d'un ``.x_t``
  donneur exporté par le même SolidWorks : un export multi-corps greffe ces
  dispositions dans son propre texte. C'est la sortie pour une version plus
  récente que la table.

Un fichier de schéma déposé prime toujours sur tout ceci — voir
:mod:`sldprt2xt.convert`.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from .schema_facts import BASE_KEY, BASE_NODES, ENVELOPE_BY_VERSION, ENVELOPE_FAMILIES
from .xt.schema import FieldSpec, NodeSpec, Schema

#: Les trois types de l'enveloppe, dans l'ordre des familles de la table.
_ASSEMBLY, _INSTANCE, _TRANSFORM = 10, 11, 100
_ENVELOPE_NAMES = {_ASSEMBLY: "ASSEMBLY", _INSTANCE: "INSTANCE", _TRANSFORM: "TRANSFORM"}


def _fields(rows) -> tuple[FieldSpec, ...]:
    return tuple(
        FieldSpec(name=name, type=kind, transmitted=sent, ptr_class=at, n_elts=n)
        for name, kind, sent, at, n in rows
    )


@cache
def base_schema() -> Schema:
    """Le schéma de base 13006, sans fichier."""
    nodes = {
        node_type: NodeSpec(
            node_type=node_type,
            name=name,
            description="",
            transmitted=sent,
            variable=variable,
            fields=_fields(rows),
        )
        for node_type, (name, sent, variable, rows) in BASE_NODES.items()
    }
    return Schema(key=BASE_KEY, modeller_version=None, nodes=nodes)


@cache
def envelope_for(version: str | int) -> Schema | None:
    """Les dispositions d'enveloppe de *version*, ou ``None`` si inconnue.

    Le schéma rendu ne porte que les trois types de l'enveloppe : c'est tout
    ce que l'écriture demande, et le recoupement de ``verify`` ne s'applique
    alors qu'à eux — exactement ceux que nous venons d'écrire.
    """
    try:
        family = ENVELOPE_BY_VERSION[int(version)]
    except (KeyError, ValueError):
        return None
    return _envelope(f"SCH_{version}", ENVELOPE_FAMILIES[family])


def envelope_from_x_t(path: str | Path) -> Schema:
    """Les dispositions d'enveloppe apprises d'un ``.x_t`` donneur.

    Le donneur doit être un export SolidWorks portant une enveloppe — un
    multi-corps en porte toujours une. Ses greffes déclarent les dispositions
    exactes que sa version écrit ; on les lit au lieu d'exiger la table.
    """
    from .parasolid import TransmitError, read_text_transmit
    from .xt import text as xt_text

    data = Path(path).read_bytes()
    transmit = read_text_transmit(data)
    body = transmit.data[transmit.body_offset :].decode("latin-1")
    layouts: dict[int, list[FieldSpec]] = {}
    xt_text.parse_nodes(body, base_schema(), layouts=layouts)

    missing = [t for t in (_ASSEMBLY, _INSTANCE, _TRANSFORM) if t not in layouts]
    if missing:
        raise TransmitError(
            f"{Path(path).name} : pas d'enveloppe ASSEMBLY dans ce donneur — "
            "prenez un .x_t multi-corps exporté par le même SolidWorks"
        )
    return _envelope(
        f"SCH_{transmit.version_schema}",
        tuple(
            tuple(
                (f.name, f.type, f.transmitted, f.ptr_class, f.n_elts)
                for f in layouts[t]
            )
            for t in (_ASSEMBLY, _INSTANCE, _TRANSFORM)
        ),
    )


def _envelope(key: str, family) -> Schema:
    nodes = {
        node_type: NodeSpec(
            node_type=node_type,
            name=_ENVELOPE_NAMES[node_type],
            description="",
            transmitted=True,
            variable=False,
            fields=_fields(rows),
        )
        for node_type, rows in zip((_ASSEMBLY, _INSTANCE, _TRANSFORM), family, strict=True)
    }
    return Schema(key=key, modeller_version=None, nodes=nodes)
