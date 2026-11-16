import pytest

from plugins.conductor_deliverables.skills.xlsx.scripts.profile import classify, profile_sheet


@pytest.mark.parametrize("heading", ["Host count", "Service count", "Region count"])
def test_numeric_count_columns_keep_number_classification_and_warnings(heading):
    result = profile_sheet("Metrics", [[heading], ["120"], ["240"]], False, {}, {})

    assert result["columns"][0]["type"] == "number"
    assert result["issues"] == [
        {"check": "number_as_text", "level": "warn", "count": 2, "examples": ["A2 (120)", "A3 (240)"]}
    ]


@pytest.mark.parametrize("heading", ["Account ID", "SKU", "Zip", "Phone", "Ref", "Symbol"])
def test_existing_identifier_headings_remain_identifiers(heading):
    assert classify(heading, ["ABC-001", "DEF-002"]) == ("identifier", 0.9)


def test_quantity_evidence_still_outranks_identifier_heading():
    assert classify("Symbol", ["10.5", "20.5"]) == ("number", 1.0)


@pytest.mark.parametrize("heading", ["CUSIP", "ISIN", "Ticker"])
def test_securities_identifier_words_are_not_special_cased(heading):
    # The upstream finance identifiers were dropped from ID_WORDS (N66):
    # such a heading is judged by its values like any other column.
    assert classify(heading, ["ABC-001", "DEF-002"]) != ("identifier", 0.9)
