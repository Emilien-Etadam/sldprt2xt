# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Un `.SLDPRT` en entrée, un `.x_t` en sortie.

Les schémas se résolvent en échelle, du plus autoritaire au plus autonome :
un fichier ``sch_*.s_t`` déposé prime, les tables intégrées du paquet
(:mod:`sldprt2xt.builtin`) prennent le relais, et un ``.x_t`` donneur couvre
une version plus récente qu'elles. Sans rien d'installé ni de fourni, tout
fichier connu des tables se convertit quand même.

Tout échec de conversion sort d'ici en :class:`ConversionError` — c'est le
contrat : un appelant qui attrape ``ConversionError`` (et
:class:`~sldprt2xt.schemas.SchemasNotFound`) a tout attrapé.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from .builtin import base_schema, envelope_for, envelope_from_x_t
from .container import open_container
from .parasolid import is_partition_stream, main_partition, to_part_transmit
from .schemas import HOW_TO_GET_THEM, find_folder
from .xt.binary import parse_nodes
from .xt.schema import find_schema, load_schema

#: Nom tronqué à cette longueur dans l'en-tête du fichier écrit.
MAX_NAME = 40


class ConversionError(Exception):
    """Ce fichier-ci n'a pas pu être converti — le message dit pourquoi."""


class _Decoded(NamedTuple):
    """Ce qu'un fichier donne à lire, et ce qu'il faut pour le réécrire."""

    transmit: object
    nodes: list
    layouts: dict
    base: object
    #: Le schéma de la version du fichier, ou ``None`` s'il n'est pas déposé.
    version: object
    header: dict
    folder: Path | None


def bodies_in(path: str | Path, *, schemas: str | Path | None = None) -> int:
    """Combien de corps porte cette pièce.

    Compter ne demande que le schéma de base : le fichier épisse lui-même la
    disposition de chaque type de nœud. Le schéma de la version, s'il est
    déposé, sert de recoupement ; absent, le compte tombe juste quand même.
    """
    decoded = _decode(Path(path), find_folder(schemas))
    return sum(1 for node in decoded.nodes if node.name == "BODY")


def to_x_t(
    path: str | Path,
    destination: str | Path | None = None,
    *,
    schemas: str | Path | None = None,
    donor: str | Path | None = None,
) -> Path:
    """Écrire le Parasolid de *path* et rendre le chemin du fichier écrit.

    *destination* est un **dossier** — qu'il existe déjà ou non — sauf s'il se
    termine par ``.x_t``, auquel cas c'est le fichier à écrire. Par défaut, le
    ``.x_t`` se pose à côté de la pièce.

    *donor* est un ``.x_t`` multi-corps exporté par le même SolidWorks : ses
    dispositions d'enveloppe servent quand la version du fichier est plus
    récente que les tables intégrées et qu'aucun schéma n'est déposé.
    """
    source = Path(path)
    out = _destination(source, destination)
    decoded = _decode(source, find_folder(schemas))

    bodies = sum(1 for node in decoded.nodes if node.name == "BODY")
    # L'enveloppe se résout dès qu'une source la donne, corps unique compris :
    # l'envelopper est mesuré sûr (Q-18), et la sortie reste alors identique
    # au bit près quelle que soit la source des schémas. Seul le multi-corps
    # en fait une exigence.
    envelope = decoded.version or envelope_for(decoded.transmit.version_schema)
    if envelope is None and donor is not None:
        try:
            envelope = envelope_from_x_t(donor)
        except Exception as failure:
            raise ConversionError(
                f"donneur inutilisable : {failure}"
            ) from failure
    if envelope is None and bodies > 1:
        raise ConversionError(
            f"cette pièce porte {bodies} corps, et leur enveloppe ASSEMBLY "
            f"vient du schéma de sa version — sch_"
            f"{decoded.transmit.version_schema}, plus récent que les tables "
            "intégrées de ce paquet. Trois sorties : mettre le paquet à "
            "jour ; donner un .x_t multi-corps exporté par le même "
            "SolidWorks (donor=/--donor) ; ou déposer le fichier de schéma "
            f"(--schemas).\n{HOW_TO_GET_THEM}"
        )

    try:
        text = to_part_transmit(
            decoded.transmit,
            decoded.nodes,
            decoded.base,
            decoded.layouts,
            envelope,
            key=_safe_key(source.stem),
            max_node_types=decoded.header.get("max_node_types"),
        )
    except Exception as failure:
        raise ConversionError(
            f"écriture impossible : {type(failure).__name__}: {failure}"
        ) from failure

    out.parent.mkdir(parents=True, exist_ok=True)
    # Écriture atomique : le transmit naît sous un nom de travail et ne prend
    # le nom final qu'entier. Un disque plein ou un Ctrl-C ne laisse jamais
    # un .x_t tronqué qui aurait l'air d'une réussite.
    partial = out.with_name(out.name + ".part")
    # Parasolid coupe ses lignes lui-même : pas de traduction de fin de ligne.
    partial.write_text(text, encoding="latin-1", newline="\n")
    os.replace(partial, out)
    return out


def _destination(source: Path, destination: str | Path | None) -> Path:
    """Où écrire — un dossier, sauf mention explicite d'un ``.x_t``.

    Décider sur ``is_dir()`` faisait d'un dossier pas encore créé un nom de
    fichier : ``to_x_t("piece.SLDPRT", "sorties/")`` sur une machine fraîche
    écrivait toute la géométrie dans un fichier littéralement nommé
    ``sorties``, écrasé à la pièce suivante.
    """
    if destination is None:
        return source.with_suffix(".x_t")
    out = Path(destination)
    if out.suffix.lower() == ".x_t":
        return out
    return out / (source.stem + ".x_t")


def _safe_key(stem: str) -> str:
    """Un nom qui survit à l'en-tête : latin-1, sans ``;`` ni contrôle.

    La clé part dans ``KEY={clé};`` d'un fichier encodé latin-1, et ``verify``
    ne relit pas les mots-clés d'en-tête : un ``;``, un retour à la ligne ou
    un caractère hors latin-1 fabriqueraient un en-tête faux — ou un plantage
    d'encodage — que rien n'attraperait. Le nom du fichier **écrit** garde,
    lui, le vrai nom de la pièce.
    """
    kept = []
    for character in stem[:MAX_NAME]:
        try:
            character.encode("latin-1")
        except UnicodeEncodeError:
            kept.append("_")
            continue
        kept.append("_" if character == ";" or ord(character) < 32 else character)
    return "".join(kept).strip() or "part"


def _decode(source: Path, folder: Path | None) -> _Decoded:
    """Les nœuds du fichier, et de quoi les réécrire.

    Le schéma de base vient d'un fichier déposé s'il y en a un — il prime —
    et de la table intégrée sinon. Le schéma de la version n'est chargé que
    depuis un fichier : intégré, il n'existe que sous forme d'enveloppe, et
    c'est :func:`to_x_t` qui la résout au moment d'écrire.
    """
    try:
        transmit = main_partition(open_container(source, is_partition_stream))
    except Exception as failure:
        raise ConversionError(f"lecture impossible : {failure}") from failure
    if transmit is None:
        raise ConversionError("pas de géométrie Parasolid dans ce fichier")

    base_file = (
        find_schema(folder, f"SCH_{transmit.base_schema}") if folder else None
    )
    if base_file is not None:
        base = load_schema(base_file)
    elif transmit.base_schema == "13006":
        base = base_schema()
    else:
        raise ConversionError(
            f"ce fichier référence le schéma de base {transmit.base_schema}, "
            "que les tables intégrées ne portent pas (elles énoncent 13006, "
            "celui de tous les SolidWorks observés) — déposez "
            f"sch_{transmit.base_schema}.s_t (--schemas)"
        )
    version_file = (
        find_schema(folder, transmit.version_schema) if folder else None
    )
    version = load_schema(version_file) if version_file else None

    header: dict = {}
    try:
        nodes, layouts = parse_nodes(
            transmit.data, transmit.body_offset, base, current=version, header=header
        )
    except Exception as failure:
        raise ConversionError(
            f"décodage impossible : {type(failure).__name__}: {failure}"
        ) from failure
    return _Decoded(transmit, nodes, layouts, base, version, header, folder)
