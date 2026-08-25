# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Extraire la géométrie Parasolid d'un fichier SolidWorks, sans SolidWorks.

    from sldprt2xt import to_x_t

    to_x_t("piece.SLDPRT")                 # écrit piece.x_t à côté
    to_x_t("piece.SLDPRT", "sorties/")     # ailleurs
"""

from .convert import ConversionError, bodies_in, to_x_t
from .schemas import SchemasNotFound, find_folder

__version__ = "1.1.0"
__all__ = [
    "ConversionError",
    "SchemasNotFound",
    "__version__",
    "bodies_in",
    "find_folder",
    "to_x_t",
]
