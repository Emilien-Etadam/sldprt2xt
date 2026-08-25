# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""La ligne de commande."""

from __future__ import annotations

import argparse
import glob as globbing
import sys
import time
from pathlib import Path

from .convert import ConversionError, to_x_t
from .schemas import SchemasNotFound, find_folder

USAGE = """\
  sldprt2xt piece.SLDPRT              le .x_t se pose a cote
  sldprt2xt *.SLDPRT -o sorties/      plusieurs fichiers
  sldprt2xt dossier/ -o sorties/      tout un dossier, sous-dossiers compris"""

#: SolidWorks pose un verrou ``~$piece.SLDPRT`` à côté de toute pièce ouverte.
#: Ce n'est pas une pièce, et un balayage qui le ramasse fait échouer le lot.
LOCK_PREFIX = "~$"


def parts_under(paths: list[Path]) -> list[tuple[Path, Path]]:
    """Les pièces désignées, chacune avec son nom de sortie relatif.

    Un dossier vaut tout ce qu'il contient, sous-dossiers compris — et la
    hiérarchie suit : deux pièces de même nom dans deux sous-dossiers gardent
    deux sorties. Les jokers sont développés ici parce que cmd et PowerShell
    ne le font pas, et les verrous SolidWorks sont écartés des balayages ; un
    fichier nommé explicitement passe tel quel, c'est lui le chef.
    """
    found: list[tuple[Path, Path]] = []
    for path, named_explicitly in _expanded(paths):
        if path.is_dir():
            for part in sorted(path.rglob("*")):
                if part.suffix.lower() != ".sldprt":
                    continue
                if part.name.startswith(LOCK_PREFIX):
                    continue
                found.append((part, part.relative_to(path).with_suffix(".x_t")))
        elif named_explicitly or not path.name.startswith(LOCK_PREFIX):
            found.append((path, Path(path.stem + ".x_t")))
    return found


def _expanded(paths: list[Path]) -> list[tuple[Path, bool]]:
    """Chaque argument, joker développé — avec qui fut nommé tel quel.

    Un motif qui matche est un balayage (ses verrous s'écartent) ; un chemin
    sans joker, ou un motif qui ne matche rien, reste l'argument de
    l'utilisateur et sera traité — ou refusé — sous son propre nom.
    """
    out: list[tuple[Path, bool]] = []
    for path in paths:
        raw = str(path)
        if any(mark in raw for mark in "*?[") and not path.exists():
            matches = sorted(Path(found) for found in globbing.glob(raw))
            if matches:
                out.extend((match, False) for match in matches)
                continue
        out.append((path, True))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sldprt2xt",
        description="Extraire la geometrie Parasolid d'un fichier SolidWorks.",
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="FICHIER")
    parser.add_argument(
        "-o", "--out", type=Path, metavar="DOSSIER", help="ou ecrire les .x_t"
    )
    parser.add_argument(
        "--schemas", type=Path, metavar="DOSSIER", help="dossier des schemas Parasolid"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="ne rien dire si tout va bien"
    )
    args = parser.parse_args(argv)

    try:
        folder = find_folder(args.schemas)
    except SchemasNotFound as missing:
        print(missing, file=sys.stderr)
        return 2

    parts = parts_under(args.paths)
    if not parts:
        print("aucun fichier .SLDPRT la-dedans", file=sys.stderr)
        return 1

    done = failed = 0
    written: dict[Path, Path] = {}
    for part, relative in parts:
        destination = args.out / relative if args.out else part.with_suffix(".x_t")
        if destination in written:
            print(
                f"{part} : meme destination que {written[destination]} "
                f"({destination}) — non ecrit",
                file=sys.stderr,
            )
            failed += 1
            continue
        started = time.monotonic()
        try:
            result = to_x_t(part, destination, schemas=folder)
        except ConversionError as failure:
            print(f"{part.name} : {failure}", file=sys.stderr)
            failed += 1
        except Exception as failure:  # un fichier en echec n'arrete pas le lot
            print(f"{part.name} : {type(failure).__name__}: {failure}", file=sys.stderr)
            failed += 1
        else:
            done += 1
            written[destination] = part
            if not args.quiet:
                size = result.stat().st_size / 1e6
                print(f"{result}  {size:.2f} Mo  ({time.monotonic() - started:.1f} s)")

    if len(parts) > 1 and not args.quiet:
        print(f"\n{done} converti(s), {failed} en echec")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
