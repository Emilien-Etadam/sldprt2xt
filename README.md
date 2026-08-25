🇫🇷 [Version française](README.fr.md)

# sldprt2xt

Gets the geometry out of a SolidWorks file. Without SolidWorks.

`part.SLDPRT` → `part.x_t`, a Parasolid file.
## Install

```sh
pip install sldprt2xt
```

## Use

```sh
sldprt2xt part.SLDPRT
```

That writes `part.x_t` right next to it. That's all.

Somewhere else:

```sh
sldprt2xt part.SLDPRT -o output/
```

Several files:

```sh
sldprt2xt *.SLDPRT -o output/
```

A whole folder, subfolders included — the tree structure is mirrored in the
output, and SolidWorks lock files (`~$part.SLDPRT`) are ignored:

```sh
sldprt2xt my_parts/ -o output/
```

From Python:

```python
from sldprt2xt import to_x_t

to_x_t("part.SLDPRT")             # writes part.x_t
to_x_t("part.SLDPRT", "output/")  # elsewhere — the folder is created if needed
```

Every failure raises `ConversionError` (or `SchemasNotFound`), with a message
that says what to do. Writes are atomic: never a truncated file under its
final name.

## The tables are built in

A Parasolid file cannot be read without a table saying which fields each node
type carries. **This package states the facts of those tables itself**
(`schema_facts.py`): every SolidWorks version known at its release — 2003
through 2026 — converts with nothing else installed.

Only two cases where you provide anything:

- **a SolidWorks version newer than this package**, for a multi-body part.
  Three ways out, from simplest to most manual: update the package; hand over
  a multi-body `.x_t` exported by the same SolidWorks (`--donor part.x_t` —
  the tool learns what it is missing from it); or point `--schemas` at the
  `pschema` folder of an installation.
- **your own schema files**, if you prefer: a supplied folder always takes
  precedence over the built-in tables (`--schemas`, or the `P_SCHEMA`
  variable Parasolid itself consults — a local SolidWorks installation is
  found on its own anyway).

## What comes out

Every body in the part, with its colours. The geometry is **the file's own,
transcribed** — nothing is rebuilt, nothing is approximated.

## What it works on

`.SLDPRT` files from SolidWorks 2003 through 2026, both generations of the
file format.

Linux, macOS, Windows. Python 3.10 or later.

## License

AGPL-3.0. See [LICENSE](LICENSE).

Parasolid is a trademark of Siemens Industry Software Inc., SolidWorks a
trademark of Dassault Systèmes. This project is affiliated with neither.
