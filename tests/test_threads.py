from ale.models import ChatMessage
from ale.threads import ThreadStore, slugify


def test_slugify_keeps_thread_ids_filesystem_safe():
    assert slugify("Hello, Ale! thread: 42") == "hello-ale-thread-42"


def test_thread_store_round_trip(tmp_path):
    store = ThreadStore(tmp_path)
    state = store.create("Heartbeat tuning", thread_id="heartbeat-tuning", persona="engineer")

    store.append_message(
        state.meta.thread_id,
        ChatMessage(role="user", content="Tune the heartbeat interval", author_name="Jason"),
    )
    store.save_recap(state.meta.thread_id, "We discussed heartbeat timing.")

    loaded = store.load("heartbeat-tuning")

    assert loaded.meta.title == "Heartbeat tuning"
    assert loaded.meta.persona == "engineer"
    assert loaded.meta.message_count == 1
    assert loaded.recap == "We discussed heartbeat timing.\n"
    assert loaded.messages[0].content == "Tune the heartbeat interval"


def test_thread_search_uses_title_and_recap(tmp_path):
    store = ThreadStore(tmp_path)
    state = store.create("Research pipeline", thread_id="research-pipeline")
    store.save_recap(state.meta.thread_id, "Linguist should cite primary sources.")

    matches = store.search("primary sources")

    assert [match.thread_id for match in matches] == ["research-pipeline"]
