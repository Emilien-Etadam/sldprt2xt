# sldprt2xt

Sort la géométrie d'un fichier SolidWorks. Sans SolidWorks.

`piece.SLDPRT` → `piece.x_t`, un fichier Parasolid que FreeCAD, Fusion, Rhino,
Blender (add-on CAD), NX, Onshape et à peu près tout le reste savent ouvrir.

## Installer

```sh
pip install sldprt2xt
```

## Utiliser

```sh
sldprt2xt piece.SLDPRT
```

Ça écrit `piece.x_t` juste à côté. C'est tout.

Ailleurs :

```sh
sldprt2xt piece.SLDPRT -o sorties/
```

Plusieurs fichiers :

```sh
sldprt2xt *.SLDPRT -o sorties/
```

Un dossier entier, sous-dossiers compris — l'arborescence est reproduite
dans la sortie, et les verrous SolidWorks (`~$pièce.SLDPRT`) sont ignorés :

```sh
sldprt2xt mes_pieces/ -o sorties/
```

Dans du Python :

```python
from sldprt2xt import to_x_t

to_x_t("piece.SLDPRT")              # écrit piece.x_t
to_x_t("piece.SLDPRT", "sorties/")  # ailleurs — le dossier est créé au besoin
```

Tout échec lève `ConversionError` (ou `SchemasNotFound`), avec un message qui
dit quoi faire. L'écriture est atomique : jamais de fichier tronqué sous son
nom final.

## Une chose à savoir : les tables de schéma

Un fichier Parasolid ne se lit pas sans sa table de correspondance. Ces tables
sont livrées avec les logiciels Parasolid — elles ne sont pas à nous, donc
elles ne sont pas dans ce paquet.

**Si SolidWorks est installé sur la machine, l'outil les trouve tout seul et
vous n'avez rien à faire.**

Sinon, prenez-les où vous les avez :

| d'où | chemin |
|---|---|
| SOLIDWORKS | `C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\data\pschema` |
| Plasticity | le dossier `parasolid-schema` de son installation |
| public | [ThraceShah/PKToy](https://github.com/ThraceShah/PKToy), dossier `PKToy.Lib/pschema` |

Puis :

```sh
sldprt2xt piece.SLDPRT --schemas /chemin/vers/pschema
```

ou une bonne fois pour toutes :

```sh
export P_SCHEMA=/chemin/vers/pschema
```

Ce qu'il faut exactement dépend de la pièce :

- **une pièce d'un seul corps** se convertit avec la seule table de base
  (`sch_13006`, dans le jeu public) — quelle que soit la version de
  SolidWorks qui l'a écrite ;
- **une pièce de plusieurs corps** exige en plus la table de la version du
  fichier : le format impose de regrouper les corps sous une enveloppe, et la
  forme de cette enveloppe vient de cette table-là. Le jeu public les couvre
  jusqu'à SolidWorks 2021 environ ; au-delà, prenez la table livrée avec le
  logiciel qui a écrit le fichier. Le message d'erreur le dit le moment venu.

## Ce qui sort

Tous les corps de la pièce, avec leurs couleurs. La géométrie est **celle du
fichier, recopiée** — rien n'est reconstruit, rien n'est approché.

Ce qui ne sort pas : l'arbre de création, les cotes, les esquisses. Seulement
la forme.

## Ça marche sur quoi

Les `.SLDPRT` de SolidWorks 2003 à 2026, les deux générations de format de
fichier. Testé sur 29 pièces : 27 passent.

Linux, macOS, Windows. Python 3.10 ou plus. Une seule dépendance.

## Licence

AGPL-3.0. Voir [LICENSE](LICENSE).

Parasolid est une marque de Siemens Industry Software Inc., SolidWorks une
marque de Dassault Systèmes. Ce projet n'est affilié ni à l'un ni à l'autre.
