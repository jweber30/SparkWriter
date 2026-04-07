import pytest

from usb_writer_core import DeviceBusyError, SessionStore, SessionUpdate, WriteIntent, WriteStatus


def make_intent(device_id: str) -> WriteIntent:
    return WriteIntent(device_id=device_id, iso_source="/tmp/test.iso")


def test_session_store_blocks_parallel_device_usage():
    store = SessionStore()
    store.create("s1", make_intent("/dev/sdb"))

    with pytest.raises(DeviceBusyError):
        store.create("s2", make_intent("/dev/sdb"))

    assert store.is_device_busy("/dev/sdb") is True

    store.mark_completed("s1")
    assert store.is_device_busy("/dev/sdb") is False

    session = store.create("s3", make_intent("/dev/sdb"))
    active = store.get_active_session_for_device("/dev/sdb")
    assert active is not None
    assert active.session_id == session.session_id


def test_force_allows_override_of_busy_device():
    store = SessionStore()
    store.create("s1", make_intent("/dev/sdc"))

    store.create("s2", make_intent("/dev/sdc"), force=True)
    active = store.get_active_session_for_device("/dev/sdc")
    assert active is not None
    assert active.session_id == "s2"


def test_release_method_clears_device_claim():
    store = SessionStore()
    store.create("s1", make_intent("/dev/sdd"))
    store.release("s1")

    assert store.is_device_busy("/dev/sdd") is False


def test_terminal_status_updates_release_claim():
    store = SessionStore()
    store.create("s1", make_intent("/dev/sde"))
    store.update("s1", SessionUpdate(status=WriteStatus.FAILED))

    assert store.is_device_busy("/dev/sde") is False