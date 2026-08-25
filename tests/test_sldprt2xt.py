# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Emilien-Etadam

"""Les tests qu'on peut faire tourner partout, plus ceux qui veulent un corpus.

Le corpus ne se devine pas : il ne tourne que si ``SLDPRT2XT_CORPUS`` désigne
un dossier existant. Aucun chemin relatif au répertoire de lancement — la même
suite doit dire la même chose d'où qu'on l'appelle.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sldprt2xt import ConversionError, SchemasNotFound, find_folder, to_x_t
from sldprt2xt.cli import main, parts_under
from sldprt2xt.container import detect_container
from sldprt2xt.convert import _destination, _safe_key


def _corpus() -> Path | None:
    named = os.environ.get("SLDPRT2XT_CORPUS")
    if not named:
        return None
    path = Path(named)
    return path if path.is_dir() else None


CORPUS = _corpus()


def parts() -> list[Path]:
    """Les pièces du corpus — verrous SolidWorks écartés, casse ignorée."""
    if CORPUS is None:
        return []
    return sorted(
        path
        for path in CORPUS.glob("*")
        if path.suffix.lower() == ".sldprt" and not path.name.startswith("~$")
    )


# ---------------------------------------------------------------------------
# Sans corpus ni schémas : la mécanique.
# ---------------------------------------------------------------------------


def test_a_folder_stands_for_every_part_under_it(tmp_path: Path):
    (tmp_path / "sous").mkdir()
    (tmp_path / "a.SLDPRT").write_bytes(b"")
    (tmp_path / "sous" / "b.sldprt").write_bytes(b"")
    (tmp_path / "pas-une-piece.txt").write_bytes(b"")

    found = parts_under([tmp_path])

    assert [(part.name, str(out)) for part, out in found] == [
        ("a.SLDPRT", "a.x_t"),
        ("b.sldprt", str(Path("sous") / "b.x_t")),
    ]


def test_same_stem_in_two_subfolders_keeps_two_outputs(tmp_path: Path):
    """Le miroir de l'arborescence est ce qui empêche l'écrasement muet."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "support.SLDPRT").write_bytes(b"")
    (tmp_path / "b" / "support.SLDPRT").write_bytes(b"")

    outs = [str(out) for _, out in parts_under([tmp_path])]

    assert outs == [str(Path("a") / "support.x_t"), str(Path("b") / "support.x_t")]


def test_solidworks_locks_are_left_out_of_sweeps(tmp_path: Path):
    """``~$pièce.SLDPRT`` n'est pas une pièce ; un balayage qui le ramasse
    fait échouer le lot alors que toutes les vraies pièces ont converti."""
    (tmp_path / "vraie.SLDPRT").write_bytes(b"")
    (tmp_path / "~$vraie.SLDPRT").write_bytes(b"")

    found = parts_under([tmp_path])

    assert [part.name for part, _ in found] == ["vraie.SLDPRT"]


def test_a_named_file_is_taken_as_named(tmp_path: Path):
    """Un fichier désigné passe tel quel, même sans le bon suffixe."""
    target = tmp_path / "chose.dat"
    target.write_bytes(b"")

    assert [part for part, _ in parts_under([target])] == [target]


def test_wildcards_are_expanded_here_because_windows_shells_do_not(tmp_path: Path):
    """cmd et PowerShell passent ``*.SLDPRT`` en littéral : l'outil développe."""
    (tmp_path / "a.SLDPRT").write_bytes(b"")
    (tmp_path / "b.SLDPRT").write_bytes(b"")
    (tmp_path / "~$a.SLDPRT").write_bytes(b"")

    found = parts_under([tmp_path / "*.SLDPRT"])

    assert [part.name for part, _ in found] == ["a.SLDPRT", "b.SLDPRT"]


def test_a_destination_folder_needs_not_exist_yet(tmp_path: Path):
    """« sorties/ » sans dossier existant reste un dossier — pas un fichier
    nommé sorties, écrasé à la pièce suivante."""
    out = _destination(Path("piece.SLDPRT"), tmp_path / "sorties")

    assert out == tmp_path / "sorties" / "piece.x_t"
    assert _destination(Path("p.SLDPRT"), tmp_path / "x.x_t") == tmp_path / "x.x_t"
    assert _destination(Path("d/p.SLDPRT"), None) == Path("d/p.x_t")


def test_the_header_key_survives_hostile_stems():
    """« ; », contrôle ou hors latin-1 dans un nom de pièce ne doivent ni
    fausser l'en-tête ni faire planter l'encodage."""
    assert _safe_key("va;vient") == "va_vient"
    assert _safe_key("œuvre") == "_uvre"
    assert _safe_key("零件") == "__"
    assert _safe_key("fermoir complet V2") == "fermoir complet V2"
    assert _safe_key(";;") == "__"
    assert _safe_key("") == "part"


def test_an_empty_schema_folder_says_so(tmp_path: Path):
    with pytest.raises(SchemasNotFound, match=str(tmp_path)):
        find_folder(tmp_path)


def test_the_not_found_message_says_where_to_get_them():
    """Un message qui dit seulement « absent » laisse le lecteur sur place."""
    from sldprt2xt.schemas import HOW_TO_GET_THEM

    assert "pschema" in HOW_TO_GET_THEM
    assert "P_SCHEMA" in HOW_TO_GET_THEM
    assert "--schemas" in HOW_TO_GET_THEM


def test_uppercase_schema_extensions_are_still_schemas(tmp_path: Path):
    """Un jeu copié d'un vieux partage arrive parfois en ``.S_T`` : le glob
    de Linux, sensible à la casse, le déclarait absent."""
    (tmp_path / "SCH_13006.S_T").write_text("")

    assert find_folder(tmp_path) == tmp_path


def test_the_command_line_refuses_an_empty_folder(tmp_path: Path, capsys):
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "sch_13006.s_t").write_text("")
    empty = tmp_path / "vide"
    empty.mkdir()

    code = main([str(empty), "--schemas", str(schemas)])

    assert code == 1
    assert "aucun fichier .SLDPRT" in capsys.readouterr().err


def test_detect_container_still_names_both_envelopes():
    """La détection sur octets reste appelable seule — et dit ce qu'elle voit."""
    assert detect_container(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64) == "cfb"
    assert detect_container(b"garbage") == "unknown"


# ---------------------------------------------------------------------------
# Avec un corpus : la conversion vraie.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not parts(), reason="SLDPRT2XT_CORPUS absent ou vide")
@pytest.mark.parametrize("part", parts(), ids=lambda p: p.stem)
def test_a_part_becomes_a_parasolid_file(part: Path, tmp_path: Path):
    try:
        written = to_x_t(part, tmp_path)
    except SchemasNotFound as missing:
        pytest.skip(str(missing).splitlines()[0])
    except ConversionError as failure:
        if "schéma de sa version" in str(failure):
            pytest.skip("multi-corps sans schéma de version : refus attendu")
        raise

    text = written.read_text(encoding="latin-1")
    assert text.startswith("**ABCDEF")
    assert "**END_OF_HEADER" in text
    assert "GUISE=transmit;" in text
    lines = text.split("\n")
    header_end = next(
        n for n, line in enumerate(lines) if line.startswith("**END_OF_HEADER")
    )
    # Une ligne qui finit par une espace fait disparaître un séparateur chez
    # le lecteur de Parasolid, et les deux jetons qu'il séparait fusionnent.
    # Une seule y échappe : la dernière du corps, dont le séparateur final n'a
    # nulle part où aller. Les fichiers de Parasolid lui-même font pareil.
    body = lines[header_end + 1 :]
    assert not any(line.endswith(" ") for line in body[:-2])


@pytest.mark.skipif(not parts(), reason="SLDPRT2XT_CORPUS absent ou vide")
def test_writing_into_a_folder_that_does_not_exist_yet(tmp_path: Path):
    """L'exemple du README, exécuté tel quel sur une machine fraîche."""
    try:
        written = to_x_t(parts()[0], tmp_path / "sorties")
    except (SchemasNotFound, ConversionError) as failure:
        pytest.skip(str(failure).splitlines()[0])

    assert written.parent == tmp_path / "sorties"
    assert written.suffix == ".x_t"
    assert not written.with_name(written.name + ".part").exists()
