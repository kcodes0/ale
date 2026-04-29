from ale.config import Settings
from ale.models import ChatMessage
from ale.router import Router
from ale.threads import ThreadStore


def test_router_infers_personas(tmp_path):
    settings = Settings(discord_token=None, anthropic_api_key=None, state_dir=tmp_path)
    router = Router(settings, ThreadStore(tmp_path / "threads"))

    assert router.infer_persona("please do deep research with sources") == "linguist"
    # Engineer is no longer keyword-routable — it is invoked exclusively via
    # the /engineer slash command (and the internal incident handoff).
    assert router.infer_persona("implement this in the repo") == "actor"
    assert router.infer_persona("debug the auth flow please") == "actor"
    assert router.infer_persona("hey what's up") == "actor"


def test_router_detects_explicit_persona_marker(tmp_path):
    settings = Settings(discord_token=None, anthropic_api_key=None, state_dir=tmp_path)
    router = Router(settings, ThreadStore(tmp_path / "threads"))

    assert router.explicit_persona("Linguist: research this") == "linguist"
    # `Engineer:` prefix no longer routes to engineer — slash command only.
    assert router.explicit_persona("Engineer: inspect logs") is None
    assert router.explicit_persona("please research this") is None


async def test_router_reuses_matching_thread(tmp_path):
    settings = Settings(discord_token=None, anthropic_api_key=None, state_dir=tmp_path)
    store = ThreadStore(tmp_path / "threads")
    state = store.create("Heartbeat tuning", thread_id="heartbeat-tuning")
    store.append_message(state.meta.thread_id, ChatMessage(role="user", content="heartbeat interval"))
    store.save_recap(state.meta.thread_id, "The thread is about heartbeat interval tuning.")
    router = Router(settings, store)

    route = await router.route(
        "what did we decide about heartbeat interval?",
        discord_channel_id=None,
        discord_user_id=None,
    )

    assert route.thread_id == "heartbeat-tuning"
    assert not route.is_new


async def test_explicit_persona_skips_llm_router(tmp_path):
    settings = Settings(
        discord_token=None,
        anthropic_api_key=None,
        state_dir=tmp_path,
        enable_llm_router=True,
    )
    store = ThreadStore(tmp_path / "threads")
    state = store.create("Prior chat", thread_id="prior-chat")
    store.append_message(state.meta.thread_id, ChatMessage(role="user", content="hello"))
    router = Router(settings, store)

    route = await router.route(
        "Linguist: research the SDK",
        discord_channel_id=None,
        discord_user_id=None,
    )

    assert route.persona == "linguist"
    assert route.thread_id == "linguist-research-the-sdk"
