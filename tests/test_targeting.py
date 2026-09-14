from __future__ import annotations

from could_not_dispatch.targeting import (
    Link,
    next_order,
    parse_lines,
    plan_fallbacks,
    split_channel_entries,
)

SHARED = 22924


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


def test_every_uncovered_channel_needs_a_fallback_of_its_own():
    plan = plan_fallbacks([], [], [1, 2])
    assert plan.uncovered_channels == [1, 2]
    assert plan.shared_links == []
    assert plan.spare_streams == []


def test_a_fallback_shared_by_several_channels_stays_with_the_lowest_channel_only():
    links = [Link(12, 3, SHARED), Link(10, 1, SHARED), Link(11, 2, SHARED)]
    plan = plan_fallbacks(links, [SHARED], [1, 2, 3])
    assert plan.shared_links == [11, 12]
    assert plan.uncovered_channels == []
    assert plan.spare_streams == []


def test_channels_that_already_own_their_fallback_are_left_alone():
    links = [Link(10, 1, 100), Link(11, 2, 101)]
    plan = plan_fallbacks(links, [100, 101], [1, 2])
    assert plan.shared_links == []
    assert plan.uncovered_channels == []
    assert plan.spare_streams == []


def test_a_fallback_no_channel_carries_is_spare():
    links = [Link(10, 1, 100)]
    plan = plan_fallbacks(links, [102, 100, 101], [1])
    assert plan.spare_streams == [101, 102]


def test_a_channel_excluded_after_apply_loses_its_fallback():
    links = [Link(10, 1, SHARED), Link(11, 9, SHARED)]
    plan = plan_fallbacks(links, [SHARED], [1, 2])
    assert plan.excluded_links == [11]
    assert plan.shared_links == []
    assert plan.uncovered_channels == [2]


def test_the_stream_of_an_excluded_channel_becomes_spare():
    links = [Link(10, 1, 100), Link(11, 9, 101)]
    plan = plan_fallbacks(links, [100, 101], [1])
    assert plan.excluded_links == [11]
    assert plan.spare_streams == [101]


def test_a_fallback_with_streams_added_after_it_goes_back_to_the_end():
    links = [Link(10, 1, 100, order=4), Link(11, 2, 101, order=3)]
    plan = plan_fallbacks(links, [100, 101], [1, 2], last_orders={1: 4, 2: 5})
    assert plan.buried_links == [11]


def test_a_fallback_already_last_stays_where_it_is():
    links = [Link(10, 1, 100, order=4)]
    plan = plan_fallbacks(links, [100], [1], last_orders={1: 4})
    assert plan.buried_links == []


def test_an_excluded_channel_is_not_moved_only_detached():
    links = [Link(11, 9, 101, order=0)]
    plan = plan_fallbacks(links, [101], [1], last_orders={9: 6})
    assert plan.excluded_links == [11]
    assert plan.buried_links == []
