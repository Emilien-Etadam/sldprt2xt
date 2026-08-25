# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Un `.SLDPRT` en entrée, un `.x_t` en sortie.

Tout échec de conversion sort d'ici en :class:`ConversionError` — c'est le
contrat : un appelant qui attrape ``ConversionError`` (et
:class:`~sldprt2xt.schemas.SchemasNotFound`) a tout attrapé.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from .container import open_container
from .parasolid import is_partition_stream, main_partition, to_part_transmit
from .schemas import find_folder
from .xt.binary import parse_nodes
from .xt.schema import SchemaError, resolve_schemas

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
    folder: Path


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
) -> Path:
    """Écrire le Parasolid de *path* et rendre le chemin du fichier écrit.

    *destination* est un **dossier** — qu'il existe déjà ou non — sauf s'il se
    termine par ``.x_t``, auquel cas c'est le fichier à écrire. Par défaut, le
    ``.x_t`` se pose à côté de la pièce.
    """
    source = Path(path)
    out = _destination(source, destination)
    decoded = _decode(source, find_folder(schemas))

    bodies = sum(1 for node in decoded.nodes if node.name == "BODY")
    if decoded.version is None and bodies > 1:
        raise ConversionError(
            f"cette pièce porte {bodies} corps, et leur enveloppe ASSEMBLY "
            f"exige le schéma de sa version — sch_"
            f"{decoded.transmit.version_schema}.s_t, absent de "
            f"{decoded.folder}. Prenez-le dans l'installation qui a produit "
            "le fichier ; un schéma voisin ne convient pas. (Une pièce d'un "
            "seul corps s'écrit sans.)"
        )

    try:
        text = to_part_transmit(
            decoded.transmit,
            decoded.nodes,
            decoded.base,
            decoded.layouts,
            decoded.version,
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


def _decode(source: Path, folder: Path) -> _Decoded:
    """Les nœuds du fichier, et de quoi les réécrire."""
    try:
        transmit = main_partition(open_container(source, is_partition_stream))
    except Exception as failure:
        raise ConversionError(f"lecture impossible : {failure}") from failure
    if transmit is None:
        raise ConversionError("pas de géométrie Parasolid dans ce fichier")

    try:
        base, version = resolve_schemas(
            folder, transmit.base_schema, transmit.schema_key
        )
    except SchemaError as missing:
        raise ConversionError(
            f"{missing} — voir le README, section « les tables de schéma »"
        ) from missing

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
