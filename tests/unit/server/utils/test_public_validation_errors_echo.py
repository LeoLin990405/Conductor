"""``public_validation_errors`` beyond the value itself: a union tag, a key, a
validator's own sentence.

Each of these is text the caller chose that pydantic carries in ``msg`` or
``loc`` rather than ``input``, so dropping ``input`` alone did not remove it.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Literal

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from src.server.utils.error_sanitization import (
    LOC_KEY_PLACEHOLDER,
    public_validation_errors,
    schema_names,
)

SECRET = "sk-" "ant-api03-FAKEFAKEFAKEFAKE0123456789"
# No credential shape, so only the exact-value cut can catch it.
PLAIN = "Zq8mW3xLp0vTn5Rk"


class _Cat(BaseModel):
    kind: Literal["cat"]


class _Dog(BaseModel):
    kind: Literal["dog"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: int = 0


class _Interpolating(BaseModel):
    word: str

    @field_validator("word")
    @classmethod
    def _check(cls, value: str) -> str:
        raise ValueError(f"unknown word {value} here")


def _public(annotation, value):
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(annotation).validate_python(value)
    return public_validation_errors(
        caught.value.errors(), owned=schema_names(annotation)
    )


def test_a_union_tag_is_not_quoted_and_the_valid_tags_are_kept():
    union = Annotated[_Cat | _Dog, Field(discriminator="kind")]
    (entry,) = _public(union, {"kind": SECRET})
    assert entry["type"] == "union_tag_invalid"
    assert SECRET[:12] not in json.dumps(entry)
    assert "'cat'" in entry["msg"] and "'dog'" in entry["msg"]


def test_a_union_tag_without_its_context_still_gives_the_rule():
    (entry,) = public_validation_errors(
        [{"type": "union_tag_invalid", "loc": ("kind",), "msg": f"Input tag '{SECRET}' x"}],
        owned={"kind"},
    )
    assert entry == {
        "type": "union_tag_invalid",
        "loc": ["kind"],
        "msg": "Input tag does not match any of the expected tags",
    }


def test_an_extra_field_name_shaped_like_a_credential_is_redacted_in_loc():
    (entry,) = _public(_Strict, {SECRET: 1})
    assert entry["type"] == "extra_forbidden"
    assert entry["loc"] == [LOC_KEY_PLACEHOLDER]


def test_an_extra_field_is_never_repeated_even_a_harmless_one():
    """The server cannot tell a typo from a pasted secret of no known shape."""
    for extra in ("nmae", PLAIN):
        (entry,) = _public(_Strict, {extra: 1})
        assert entry["loc"] == [LOC_KEY_PLACEHOLDER]
        assert extra not in json.dumps(entry)


@pytest.mark.parametrize("annotation", [dict[int, int], dict[uuid.UUID, int]])
def test_a_dict_key_that_failed_to_parse_is_cut_whatever_its_shape(annotation):
    errors = _public(annotation, {PLAIN: 1})
    assert errors and PLAIN not in json.dumps(errors)
    assert errors[0]["loc"] == [LOC_KEY_PLACEHOLDER, "[key]"]


def test_a_validator_sentence_loses_the_value_it_interpolated_and_keeps_the_rule():
    (entry,) = _public(_Interpolating, {"word": PLAIN})
    assert entry["type"] == "value_error"
    assert entry["msg"] == "Value error, unknown word [REDACTED] here"


def test_field_names_indexes_and_ordinary_sentences_are_untouched():
    class _Row(BaseModel):
        count: int

    errors = _public(list[_Row], [{"count": "x"}, {"count": 1}])
    assert errors == [
        {
            "type": "int_parsing",
            "loc": [0, "count"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
        }
    ]


def test_a_short_rejected_value_is_not_cut_out_of_the_sentence():
    # "a" appears in nearly every message; cutting it would mangle the rule.
    (entry,) = _public(dict[str, int], {"k": "a"})
    assert entry["msg"] == "Input should be a valid integer, unable to parse string as an integer"


def test_a_caller_chosen_key_is_replaced_whatever_its_size():
    key = "k" * 50_000
    (entry,) = _public(dict[str, int], {key: "x"})
    assert entry["loc"][-1] == LOC_KEY_PLACEHOLDER
    assert len(json.dumps(entry)) < 1500


def test_a_long_sentence_is_clipped_without_leaving_a_prefix_of_the_value():
    value = "Zq8mW3xLp0vTn5Rk" * 2000  # 32 KB, no credential shape
    (entry,) = _public(_Interpolating, {"word": value})
    assert entry["msg"] == "Value error, unknown word [REDACTED] here"
    assert "Zq8m" not in json.dumps(entry)


def test_a_huge_key_in_a_validation_error_does_not_stall_the_loop():
    started = time.perf_counter()
    _public(dict[str, int], {"a" * 400_000: "x"})
    assert time.perf_counter() - started < 1.0
