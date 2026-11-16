"""Native CLI usage has a per-call record even without a LangChain callback."""

from src.utils.tracking.per_call_token_tracker import PerCallTokenTracker


def test_native_usage_is_recorded_and_aggregated():
    tracker = PerCallTokenTracker()
    tracker.record_native_usage(
        model_name="codex",
        input_tokens=120,
        output_tokens=8,
        cache_read_tokens=900,
        served_model="codex",
    )
    records = tracker.get_per_call_records()
    assert len(records) == 1
    assert records[0]["billing_type"] == "native"
    assert records[0]["usage"]["input_tokens"] == 120
    assert records[0]["native_cache_read_tokens"] == 900
    assert tracker.get_aggregated_usage()["codex"]["input_tokens"] == 120
