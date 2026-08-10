from __future__ import annotations

from could_not_dispatch.targeting import next_order, parse_lines, split_channel_entries


def test_parse_lines_trims_drops_blanks_and_keeps_order():
    assert parse_lines("  Sky \n\n Rai 1\n") == ["Sky", "Rai 1"]


def test_parse_lines_drops_case_insensitive_duplicates():
    assert parse_lines("Sky\nSKY\nsky") == ["Sky"]


def test_parse_lines_accepts_a_list():
    assert parse_lines(["Sky", " Rai "]) == ["Sky", "Rai"]


def test_parse_lines_treats_empty_input_as_nothing():
    assert parse_lines(None) == []
    assert parse_lines("") == []
    assert parse_lines("   \n  ") == []


def test_split_channel_entries_separates_numbers_from_names():
    numbers, names = split_channel_entries(["101", "Rai 1", "20.5"])
    assert numbers == [101.0, 20.5]
    assert names == ["Rai 1"]


def test_next_order_puts_the_fallback_after_the_last_stream():
    assert next_order({7: 3}, 7) == 4


def test_next_order_starts_at_zero_for_a_channel_with_no_streams():
    assert next_order({}, 7) == 0
    assert next_order({7: None}, 7) == 0
