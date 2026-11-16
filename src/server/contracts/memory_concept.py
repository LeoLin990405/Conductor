"""The ontology of a memory: its class, its concepts, and the filters on them.

The shared-memory ontology gives every memory a ``class`` -- what kind of
claim it is -- and one or more ``about`` concept ids (``project:conductor``).
Two things live here, and both are needed by more than one layer:

* the normalization of a declared value (a class, a list of concept ids), and
* the three states of a filter. ``UNSET`` is "the caller did not narrow on
  this field"; ``None`` is "the rows whose field is NULL"; a string is "the
  rows whose field is this value". The distinction matters because the browse
  page can select the group of memories that name no concept, and asking for
  that group is not the same as asking for everything.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

#: How many concept ids one memory may name.
MAX_CONCEPT_VALUES: Final[int] = 16

#: Bound on one ontology value: a class name or a concept id.
MAX_CONCEPT_VALUE: Final[int] = 256


class Unset:
    """The marker for a filter the caller did not narrow on."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


#: "No filter on this field." ``None`` means the rows whose field is NULL.
UNSET: Final[Unset] = Unset()


def concept_filter(value: str | None | Unset) -> tuple[bool, str | None]:
    """One ontology filter as ``(is_null, value)``.

    An unset filter narrows nothing; a NULL filter narrows to the rows that
    name nothing; a value filter narrows to the rows that name it. A blank
    value is not a value: it narrows nothing, because a filter that names
    nothing must not narrow by accident.
    """
    if isinstance(value, Unset):
        return (False, None)
    if value is None:
        return (True, None)
    text = str(value).strip()
    if not text:
        return (False, None)
    return (False, text[:MAX_CONCEPT_VALUE])


def concept_values(value: Any) -> list[str]:
    """The concept ids a memory names, in the order it names them.

    A concept id is a short token (``project:conductor``). The list is
    deduplicated and bounded, and a value that is not a usable token is
    dropped rather than stored: an ontology field that names nothing must not
    become a concept of its own.
    """
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or len(text) > MAX_CONCEPT_VALUE or text in out:
            continue
        out.append(text)
        if len(out) >= MAX_CONCEPT_VALUES:
            break
    return out


def concept_class(value: Any) -> str | None:
    """The class of a memory -- what kind of claim it is -- or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_CONCEPT_VALUE:
        return None
    return text


def row_metadata(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The metadata of a row, or an empty mapping when it has none."""
    value = row.get("metadata")
    return value if isinstance(value, Mapping) else {}


def concept_ontology(source: Mapping[str, Any]) -> dict[str, Any]:
    """The class and concepts a source declares, normalized for a hit.

    A federated source annotates its own memories, and the annotation has to
    survive the trip: a memory that names a concept must still name it when
    the Worker reads it and the Ticket audits it. The values are normalized
    here -- one source of truth for what a class and a concept id are -- so a
    hit never carries an ontology that no reader would accept.

    A source may put the annotation at the top level or inside its own
    metadata mapping; the top level wins, because it is the source's own
    declaration of the memory rather than a wrapper around it. A part that
    names nothing is left out rather than stored as an empty value.
    """
    out: dict[str, Any] = {}
    nested = source.get("metadata")
    if isinstance(nested, Mapping):
        nested_class = concept_class(nested.get("class"))
        if nested_class is not None:
            out["class"] = nested_class
        nested_concepts = concept_values(nested.get("about"))
        if nested_concepts:
            out["about"] = nested_concepts
    memory_class = concept_class(source.get("class"))
    if memory_class is not None:
        out["class"] = memory_class
    concepts = concept_values(source.get("about"))
    if concepts:
        out["about"] = concepts
    return out


def row_class(row: Mapping[str, Any]) -> str | None:
    """The class a row declares, from the column or from the metadata.

    The projection keeps the class in its own column; a federated source
    supplies it in the metadata. The column wins when the two disagree: it
    is the indexed field, and it is what a re-projection last wrote.
    """
    column = concept_class(row.get("class"))
    if column is not None:
        return column
    return concept_class(row_metadata(row).get("class"))


def row_concepts(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Every concept id a row declares, in the order it declares them.

    The projection keeps the concepts in two places: the ``about`` column
    holds the primary one (it is the indexed pair with ``class``), and the
    metadata holds every one of them. A federated source supplies them in
    the metadata alone. The metadata wins when the two disagree, because it
    is the full declaration and the column holds only the first of it.

    A memory that names no concept has none: an unnamed concept is not a
    concept, and inventing one would make a record claim a link the memory
    never made.
    """
    values = concept_values(row_metadata(row).get("about"))
    if not values:
        values = concept_values(row.get("about"))
    return tuple(values)


__all__ = [
    "MAX_CONCEPT_VALUES",
    "MAX_CONCEPT_VALUE",
    "UNSET",
    "Unset",
    "concept_class",
    "concept_filter",
    "concept_ontology",
    "concept_values",
    "row_class",
    "row_concepts",
    "row_metadata",
]
