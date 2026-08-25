# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Trouver les tables de schéma Parasolid, sans rien demander à personne.

Un fichier Parasolid ne se décode pas sans la table qui dit quels champs porte
chaque type de nœud. Ces tables sont livrées avec les logiciels Parasolid ;
elles ne sont pas à nous, donc elles ne sont pas dans ce paquet. Ce module les
cherche là où elles se trouvent déjà sur la machine.
"""

from __future__ import annotations

import glob
import os
from functools import cache
from pathlib import Path

#: Là où un logiciel Parasolid pose ses tables, par ordre de vraisemblance.
#: `P_SCHEMA` est la variable que Parasolid lui-même consulte.
_ENV = ("SLDPRT2XT_SCHEMAS", "P_SCHEMA")

_PLACES = (
    r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\data\pschema",
    r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\schema_18",
    r"C:\Program Files\Common Files\eDrawings*\pschema",
    "/Applications/SOLIDWORKS/pschema",
    "~/.local/share/parasolid/pschema",
    "~/.cache/sldprt2xt/schemas",
)


class SchemasNotFound(Exception):
    """Aucun dossier de schémas trouvé — le message dit quoi faire."""


@cache
def find_folder(explicit: str | Path | None = None) -> Path:
    """Le dossier de schémas à utiliser.

    Ordre : ce qui est passé en argument, puis les variables d'environnement,
    puis les emplacements habituels d'installation.

    En cache : un lot de mille pièces valide le dossier une fois, pas mille —
    la validation parcourt tout le dossier. Conséquence assumée : déposer des
    schémas ou changer la variable d'environnement en cours de processus ne
    sera pas vu.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not _has_schemas(path):
            raise SchemasNotFound(f"aucun fichier sch_*.s_t sous {path}")
        return path

    for name in _ENV:
        value = os.environ.get(name)
        if value and _has_schemas(Path(value).expanduser()):
            return Path(value).expanduser()

    for place in _PLACES:
        for path in _expand(place):
            if _has_schemas(path):
                return path

    raise SchemasNotFound(HOW_TO_GET_THEM)


def _expand(pattern: str) -> list[Path]:
    """Les chemins que ce motif désigne, sans jamais lever.

    Les emplacements listés sont ceux de plusieurs systèmes : sous Linux, un
    chemin Windows n'est pas seulement absent, il peut être imprononçable
    pour l'outillage. Chercher ne doit pas pouvoir échouer — et le joker doit
    pouvoir tomber n'importe où dans le motif, pas seulement à la fin :
    ``glob.glob`` sait déjà tout ça.
    """
    try:
        expanded = os.path.expanduser(pattern)
        if not any(mark in expanded for mark in "*?["):
            return [Path(expanded)]
        return sorted(Path(found) for found in glob.glob(expanded))
    except (OSError, ValueError):
        return []


def _has_schemas(folder: Path) -> bool:
    """Ce dossier porte-t-il au moins un ``sch_*.s_t`` — quelle qu'en soit la casse ?

    Un jeu copié d'un vieux partage Windows arrive parfois en ``.S_T`` : le
    glob de Linux, sensible à la casse, le déclarerait absent alors que le
    chargeur, lui, accepte toutes les casses.
    """
    try:
        if not folder.is_dir():
            return False
        return any(path.name.lower().endswith(".s_t") for path in folder.rglob("*"))
    except OSError:
        return False


HOW_TO_GET_THEM = """\
Tables de schéma Parasolid introuvables.

sldprt2xt en a besoin pour lire la géométrie. Elles sont livrées avec les
logiciels Parasolid ; prenez celles que vous avez déjà :

  • SOLIDWORKS    C:\\Program Files\\SOLIDWORKS Corp\\SOLIDWORKS\\data\\pschema
  • Plasticity    le dossier parasolid-schema de son installation
  • un jeu public https://github.com/ThraceShah/PKToy  (dossier PKToy.Lib/pschema)

Puis indiquez le dossier :

  sldprt2xt piece.SLDPRT --schemas /chemin/vers/pschema

ou une bonne fois pour toutes :

  export P_SCHEMA=/chemin/vers/pschema"""
