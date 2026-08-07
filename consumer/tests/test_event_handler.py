from consumer import event_handler
from consumer.messages import WateringEventMessage
from consumer.tests.conftest import event_payload


def msg(**overrides) -> WateringEventMessage:
    return WateringEventMessage.model_validate(event_payload(**overrides))


async def test_duplicate_event_changes_nothing(fake_db, sent):
    fake_db.mark_event_processed.return_value = False

    await event_handler.process(msg())

    fake_db.update_watering_event.assert_not_awaited()
    assert sent == []


async def test_started_event_records_relay_time_and_stays_quiet(fake_db, sent):
    m = msg(event="watering_started")

    await event_handler.process(m)

    args, kwargs = fake_db.update_watering_event.await_args
    assert args == (m.command_id, "dispatched")
    # started_at comes from the Pi's own recorded_at, not broker-delivery time.
    assert kwargs["started_at"] == m.recorded_at
    # A start is not a completion — completed_at must stay untouched.
    assert kwargs.get("completed_at") is None
    assert sent == []   # nothing to tell the user until it finishes


async def test_completed_event_closes_the_command_and_notifies(fake_db, sent):
    m = msg(event="watering_completed", duration_sec=10, plant_id=2)

    await event_handler.process(m)

    args, kwargs = fake_db.update_watering_event.await_args
    assert args == (m.command_id, "completed")
    assert kwargs["completed_at"] is not None
    assert len(sent) == 1
    assert "2" in sent[0] and "10s" in sent[0]


async def test_duplicate_terminal_event_does_not_re_notify(fake_db, sent):
    # The guarded UPDATE refuses to touch an already-terminal row and reports
    # False; without honouring that, a Pi republishing its cached terminal event
    # would send the user a second "watered" message for one watering.
    fake_db.update_watering_event.return_value = False

    await event_handler.process(msg(event="watering_completed"))

    assert sent == []


async def test_failed_event_reports_the_error(fake_db, sent):
    await event_handler.process(
        msg(event="watering_failed", error="relay_not_responding", duration_sec=None))

    assert fake_db.update_watering_event.await_args.args[1] == "failed"
    assert "relay_not_responding" in sent[0]


async def test_expired_event_is_recorded_silently(fake_db, sent):
    await event_handler.process(msg(event="command_expired", duration_sec=None))

    assert fake_db.update_watering_event.await_args.args[1] == "expired"
    assert sent == []


async def test_event_is_marked_processed_with_its_command_id(fake_db, sent):
    m = msg()

    await event_handler.process(m)

    args, kwargs = fake_db.mark_event_processed.await_args
    assert args[0] == m.event_id
    assert kwargs["command_id"] == m.command_id
    assert kwargs["event_type"] == "watering_completed"
    # Same connection as the lifecycle update — they must commit together.
    assert kwargs["conn"] is fake_db.update_watering_event.await_args.kwargs["conn"]
