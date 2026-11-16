"""Deterministic reciprocal-rank fusion for independent memory sources."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence

from .models import MemoryHit

_Identity = tuple[str, str | None, str | None, str]


def _identity(hit: MemoryHit) -> _Identity:
    # A matching key across stores is not evidence that the rows are the same.
    return (hit.source, hit.tier, hit.workspace_id, hit.key)


def fuse_hits(
    hit_lists: Sequence[Sequence[MemoryHit]],
    k: int,
    *,
    rrf_k: int = 60,
) -> list[MemoryHit]:
    """Rank by one contribution per source and identity, independent of scores.

    Duplicate rows from one source do not increase a result's score. Distinct
    sources retain distinct identities and each contribute at most once.
    """
    if k <= 0 or rrf_k < 1:
        return []
    # Track the best rank per identity and source, including repeated lists
    # returned by one source. Final ties use lexical identity order.
    representatives: OrderedDict[_Identity, MemoryHit] = OrderedDict()
    source_ranks: dict[_Identity, dict[str, int]] = {}
    for rows in hit_lists:
        seen_in_list: set[_Identity] = set()
        for rank, hit in enumerate(rows, start=1):
            if not isinstance(hit, MemoryHit):
                continue
            identity = _identity(hit)
            if identity in seen_in_list:
                continue
            seen_in_list.add(identity)
            representatives.setdefault(identity, hit)
            ranks = source_ranks.setdefault(identity, {})
            prior = ranks.get(hit.source)
            if prior is None or rank < prior:
                ranks[hit.source] = rank

    fused: list[tuple[float, MemoryHit]] = []
    for identity, hit in representatives.items():
        ranks = source_ranks[identity]
        score = sum(1.0 / (rrf_k + rank) for rank in ranks.values())
        provenance = dict(hit.provenance)
        provenance["federated"] = {
            "rrf_k": rrf_k,
            "source_ranks": dict(sorted(ranks.items())),
            "original_score": hit.score,
        }
        fused.append(
            (
                score,
                MemoryHit(
                    source=hit.source,
                    key=hit.key,
                    text=hit.text,
                    score=score,
                    provenance=provenance,
                    tier=hit.tier,
                    workspace_id=hit.workspace_id,
                ),
            )
        )
    fused.sort(
        key=lambda row: (
            -row[0],
            row[1].source,
            row[1].tier or "",
            row[1].workspace_id or "",
            row[1].key,
        )
    )
    return [hit for _, hit in fused[:k]]
