from iac_code.types.stream_events import ContextUsageEvent, MessageEndEvent, StreamEvent, Usage


def test_context_usage_event_is_a_stream_event():
    event = ContextUsageEvent(usage={"total_tokens": 1234, "context_window": 60000})
    assert event.type == "context_usage"
    assert event.usage["total_tokens"] == 1234
    # Must be a member of the StreamEvent union so translators can dispatch on it.
    assert ContextUsageEvent in StreamEvent.__args__


def test_message_end_event_still_carries_usage():
    # Guard: we hook off MessageEndEvent; keep its shape intact.
    end = MessageEndEvent(stop_reason="end_turn", usage=Usage())
    assert end.type == "message_end"
