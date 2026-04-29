import json

from ale.observability import EventLogger


def test_event_logger_writes_global_and_subsystem_logs(tmp_path):
    logger = EventLogger(tmp_path)

    logger.event("discord", "message_received", discord_message_id="1")
    logger.turn(thread_id="t1", persona="actor")

    all_events = [json.loads(line) for line in (tmp_path / "all-events.jsonl").read_text().splitlines()]
    discord_events = [json.loads(line) for line in (tmp_path / "discord.log").read_text().splitlines()]
    turns = [json.loads(line) for line in (tmp_path / "turns.jsonl").read_text().splitlines()]

    assert all_events[0]["subsystem"] == "discord"
    assert discord_events[0]["event"] == "message_received"
    assert turns[0]["event"] == "turn"


def test_event_logger_rotates_logs(tmp_path):
    logger = EventLogger(tmp_path, max_bytes=80, backup_count=2)

    for index in range(6):
        logger.event("discord", "message_received", discord_message_id=str(index), pad="x" * 40)

    assert (tmp_path / "discord.log").exists()
    assert (tmp_path / "discord.log.1").exists()
    assert (tmp_path / "all-events.jsonl.1").exists()
    assert not (tmp_path / "discord.log.3").exists()
