from app.sessions import create_session, read_session


def test_signed_session_round_trip_and_tamper_rejection():
    token = create_session(12345)
    assert read_session(token) == 12345
    assert read_session(token + "x") is None
    assert read_session("invalid") is None
    assert read_session("not-base64.%%%") is None
