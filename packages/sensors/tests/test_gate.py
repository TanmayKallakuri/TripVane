from tripvane_sensors.runtime.gate import RECENT_HASHES_SIZE, cheap_gate, new_recent_hashes


def test_rejects_empty_and_short_input() -> None:
    recent = new_recent_hashes()
    assert not cheap_gate("", recent)
    assert not cheap_gate("short", recent)
    assert not cheap_gate("1234567", recent)
    assert not cheap_gate("   abc   \n\t  ", recent)
    assert len(recent) == 0


def test_accepts_input_of_eight_characters() -> None:
    assert cheap_gate("12345678", new_recent_hashes())


def test_rejects_a_repeated_payload() -> None:
    recent = new_recent_hashes()
    assert cheap_gate("How do I export invoices?", recent)
    assert not cheap_gate("How do I export invoices?", recent)
    # payload_hash normalizes case and whitespace, so this is the same payload.
    assert not cheap_gate("  how do I   EXPORT invoices? ", recent)


def test_repeat_is_accepted_again_once_it_ages_out() -> None:
    recent = new_recent_hashes()
    assert cheap_gate("first payload here", recent)
    for index in range(RECENT_HASHES_SIZE):
        assert cheap_gate(f"filler payload {index}", recent)
    assert len(recent) == RECENT_HASHES_SIZE
    assert cheap_gate("first payload here", recent)
