"""The server-sent events line parser (no database)."""

from agents_usage.sse import SseEvent, SseParser, parse_sse


def test_events_split_on_blank_lines():
    lines = ["event: snapshot", 'data: {"a": 1}', "", "event: session_removed", "data: x", ""]
    assert list(parse_sse(lines)) == [
        SseEvent("snapshot", '{"a": 1}', None),
        SseEvent("session_removed", "x", None),
    ]


def test_multiline_data_joined():
    assert list(parse_sse(["event: e", "data: a", "data: b", ""])) == [SseEvent("e", "a\nb", None)]


def test_comment_and_unknown_lines_skipped():
    lines = [": keepalive", "retry: 5", "garbage", "id: 7", "event: e", "data: d", ""]
    assert list(parse_sse(lines)) == [SseEvent("e", "d", "7")]


def test_event_without_data_not_dispatched():
    assert list(parse_sse(["event: x", "", "event: y", "data: z", ""])) == [
        SseEvent("y", "z", None)
    ]


def test_message_is_the_default_event_name():
    assert list(parse_sse(["data: d", ""])) == [SseEvent("message", "d", None)]


def test_only_one_leading_space_is_stripped_and_carriage_returns_dropped():
    assert list(parse_sse(["data:  two\r", "data:none", "\r"])) == [
        SseEvent("message", " two\nnone", None)
    ]


def test_unterminated_event_is_not_dispatched():
    assert list(parse_sse(["event: e", "data: d"])) == []


def test_fields_do_not_leak_into_the_next_event():
    lines = ["id: 1", "event: a", "data: x", "", "data: y", ""]
    assert list(parse_sse(lines)) == [SseEvent("a", "x", "1"), SseEvent("message", "y", None)]


def test_parser_feeds_one_line_at_a_time():
    parser = SseParser()
    assert [parser.feed(line) for line in ("event: e", "data: d")] == [None, None]
    assert parser.feed("") == SseEvent("e", "d", None)
    assert parser.feed("") is None
