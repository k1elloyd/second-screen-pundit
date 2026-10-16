from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from second_screen_pundit.agents.models import CommentaryLine, Recap


class CachedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    answer: str
    fact_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)


class CachedMatchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match_id: str = Field(pattern=r"^m\d{2}$")
    label: str
    commentary: list[CommentaryLine]
    questions: list[CachedQuestion]
    recap: Recap


def load_cached_match(
    match_id: str,
    *,
    cache_dir: Path = Path("data/cached"),
) -> CachedMatchOutput:
    path = cache_dir / f"{match_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"No cached narrative is available for {match_id}") from exc
    cached = CachedMatchOutput.model_validate(data)
    if cached.match_id != match_id:
        raise ValueError(f"Cached narrative {path} contains match {cached.match_id}, not {match_id}")
    return cached


def closest_cached_question(
    cached: CachedMatchOutput,
    question: str,
    *,
    minimum_similarity: float = 0.45,
) -> CachedQuestion | None:
    if not question.strip():
        return None
    best = max(
        cached.questions,
        key=lambda candidate: SequenceMatcher(
            None,
            question.casefold().strip(),
            candidate.question.casefold().strip(),
        ).ratio(),
        default=None,
    )
    if best is None:
        return None
    similarity = SequenceMatcher(
        None,
        question.casefold().strip(),
        best.question.casefold().strip(),
    ).ratio()
    return best if similarity >= minimum_similarity else None
