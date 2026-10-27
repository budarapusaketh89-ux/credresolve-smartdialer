from app.services import ALLOWED_TRANSITIONS

def test_completed_call_is_terminal():
    assert ALLOWED_TRANSITIONS["COMPLETED"] == set()

def test_call_cannot_jump_from_queued_to_connected():
    assert "CONNECTED" not in ALLOWED_TRANSITIONS["QUEUED"]

def test_ringing_can_be_answered():
    assert "ANSWERED" in ALLOWED_TRANSITIONS["RINGING"]
