"""``public_validation_errors``: the rule that was broken, never the value."""

from __future__ import annotations

import enum
import json
import uuid
from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.server.utils.error_sanitization import (
    caller_runs,
    public_path,
    public_validation_errors,
    schema_names,
    validation_error_text,
)

SECRET = "sk-" "ant-api03-FAKEFAKEFAKEFAKE0123456789"


class _Model(BaseModel):
    text: str = Field(max_length=10)
    lease: uuid.UUID
    items: list[int] = Field(max_length=2)
    word: str = "ok"

    @field_validator("word")
    @classmethod
    def _plain(cls, value: str) -> str:
        if value != "ok":
            raise ValueError("word is not allowed")
        return value


def _errors(**payload):
    with pytest.raises(ValidationError) as caught:
        _Model(**payload)
    return caught.value.errors()


def test_only_the_type_the_field_and_the_sentence_survive():
    raw = _errors(text=SECRET, lease=SECRET, items=[1, 2, 3], word=SECRET)
    # The default shape carries the rejected values and the validator's context.
    assert any("input" in err for err in raw) and any("ctx" in err for err in raw)
    public = public_validation_errors(raw, owned=schema_names(_Model))
    assert len(public) == len(raw)
    assert all(set(entry) == {"type", "loc", "msg"} for entry in public)
    assert SECRET[:20] not in json.dumps(public)


def test_the_field_path_and_the_sentence_still_say_what_to_fix():
    public = public_validation_errors(
        _errors(text="x" * 11, lease=str(uuid.uuid4()), items=[]),
        owned=schema_names(_Model),
    )
    assert public == [
        {
            "type": "string_too_long",
            "loc": ["text"],
            "msg": "String should have at most 10 characters",
        }
    ]


def test_a_uuid_error_does_not_quote_a_character_of_the_value():
    public = public_validation_errors(
        _errors(text="a", lease=SECRET, items=[]), owned=schema_names(_Model)
    )
    assert public == [
        {"type": "uuid_parsing", "loc": ["lease"], "msg": "Input should be a valid UUID"}
    ]


class _Mode(enum.StrEnum):
    summary = "summary"
    detailed = "detailed"


class _Choice(BaseModel):
    mode: Literal["summary", "detailed"]
    kind: _Mode


@pytest.mark.parametrize("typo", ["Detailed", "DETAILED", "detailed "])
def test_a_literal_or_enum_keeps_its_list_when_the_typo_is_one_of_its_words(typo):
    # The list is the schema's own and never the input. The cut of the request's
    # text would take it out exactly when the typo is one of its words, and then
    # the sentence stops saying what the field accepts.
    body = {"mode": typo, "kind": typo}
    with pytest.raises(ValidationError) as caught:
        _Choice(**body)
    sentence = "Input should be 'summary' or 'detailed'"

    public = public_validation_errors(
        caught.value.errors(),
        owned=schema_names(_Choice),
        caller=caller_runs(body),
    )
    assert [(e["type"], e["loc"], e["msg"]) for e in public] == [
        ("literal_error", ["mode"], sentence),
        ("enum", ["kind"], sentence),
    ]
    assert validation_error_text(caught.value, owned=schema_names(_Choice)) == (
        f"mode: {sentence}; kind: {sentence}"
    )


def test_a_validator_message_is_kept_and_its_exception_context_is_not():
    public = public_validation_errors(
        _errors(text="a", lease=str(uuid.uuid4()), items=[], word="nope"),
        owned=schema_names(_Model),
    )
    assert public[0]["type"] == "value_error"
    assert public[0]["msg"] == "Value error, word is not allowed"
    assert "ctx" not in public[0]
    json.dumps(public)  # loc and msg are plain JSON


class _Holder(BaseModel):
    entries: dict[str, _Model]


def _held(key: str) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _Holder(entries={key: {"text": "x" * 11, "lease": str(uuid.uuid4()), "items": []}})
    return caught.value


def test_public_path_keeps_the_schemas_names_and_positions_and_hides_the_rest():
    owned = schema_names(_Holder)
    assert public_path(("entries", "text"), owned=owned) == "entries.text"
    assert public_path(("items", 3, "text"), owned=owned) == "items[3].text"
    assert public_path((), owned=owned) == ""
    # A key the caller chose is not the schema's to repeat, whatever it looks like.
    assert public_path(("entries", SECRET, "text"), owned=owned) == "entries.<key>.text"
    assert SECRET not in public_path(("entries", SECRET), owned=owned)
    # A request's leading ``body`` or ``query`` is kept in a 422's loc; a document
    # has none, so there it is a key like any other.
    assert public_path(("body", "text"), owned=owned) == "<key>.text"
    # Unless the caller has vetted it, by adding it to the names it passes.
    assert (
        public_path(("entries", "vetted"), owned=owned | {"vetted"}) == "entries.vetted"
    )


def test_validation_error_text_drops_leading_parts_after_making_them_public():
    owned = schema_names(_Holder)
    sentence = "String should have at most 10 characters"
    assert validation_error_text(_held(SECRET), owned=owned) == (
        f"entries.<key>.text: {sentence}"
    )
    # The caller's own path names the entry; the key is hidden whichever part is cut.
    assert validation_error_text(_held(SECRET), owned=owned, under=1) == (
        f"<key>.text: {sentence}"
    )
    assert validation_error_text(_held(SECRET), owned=owned, under=2) == (
        f"text: {sentence}"
    )
    assert validation_error_text(_held(SECRET), owned=owned, under=3) == sentence
