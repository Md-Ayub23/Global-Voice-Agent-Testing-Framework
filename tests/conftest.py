"""Marks the real-time tests as `slow`: whole voice calls (and load tests) that take as long as the calls
themselves. `uv run pytest` runs everything (about 23 minutes); `uv run pytest -m "not slow"` is the quick
check while you work (a few minutes); run the full suite before a commit."""

from __future__ import annotations

import pytest

# Tests that place real-time calls, by name; plus every test using a whole-run fixture below.
SLOW_TESTS = {
    "test_the_connection_test_can_make_a_short_vapi_call",
    "test_a_scripted_line_is_played_from_its_recording",
    "test_a_run_can_make_every_caller_speak_tamil",
    "test_a_tamil_voice_run_end_to_end",
    "test_a_voice_call_streams_live_and_its_timing_is_unchanged",
    "test_find_the_limit_stops_at_the_breaking_point",
    "test_caller_listens_when_the_agent_goes_on_talking",
    "test_latency_matches_the_reference_agent_within_50ms",
    "test_misread_reference_is_caught_only_in_voice",
    "test_slow_model_loading_neither_hides_the_greeting_nor_delays_the_recording",
    "test_a_caller_who_goes_quiet_and_pauses_on_a_noisy_phone_line",
    "test_a_scripted_cut_in_on_the_greeting_is_written_down_and_answered",
    "test_clinic_agent_with_a_caller_who_cuts_in_and_says_mm_hm",
    "test_latency_reference_agent_under_a_phone_line_with_lost_packets",
    "test_clinic_load_go_then_double_booking_no_go_with_baseline_ci_and_alerts",
    "test_load_reference_agent_capacity_is_found_exactly",
    "test_a_plivo_call_goes_both_ways_with_its_own_stream_messages",
    "test_a_real_phone_call_goes_both_ways_and_is_hung_up",
    "test_an_outbound_agent_calls_vocto_on_its_own_number",
    "test_a_speech_mode_run_compares_conditions_side_by_side",
    "test_a_speech_run_with_the_ladder_end_to_end",
}
SLOW_FIXTURES = {"clean_voice_run", "clinic_twilio_run", "vapi_run", "eleven_run", "retell_run", "livekit_run"}


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: places real-time calls; skip with -m \"not slow\"")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.originalname in SLOW_TESTS or SLOW_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.slow)
