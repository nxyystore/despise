"""Interaction builders + REST wiring, and the typing refresh loop."""

import asyncio

import pytest
from test_rest import Recorder, rest_with

from despise.interactions import (
    BUTTON_LINK,
    BUTTON_PRIMARY,
    CHANNEL_MESSAGE_WITH_SOURCE,
    MODAL_SUBMIT,
    SELECT_STRING,
    SELECT_USER,
    UPDATE_MESSAGE,
    autocomplete_response,
    build_action_row,
    build_button,
    build_modal,
    build_select,
    build_text_input,
    defer,
    defer_thinking,
    message_response,
    modal_response,
    new_id,
    pong,
    update_response,
)
from despise.rest import TYPING_REFRESH_S, Rest, TypingLoop

run = asyncio.run


# -- components -------------------------------------------------------------
def test_button_shape():
    b = build_button("Click", "cid", style=BUTTON_PRIMARY)
    assert b == {
        "type": 2,
        "style": BUTTON_PRIMARY,
        "label": "Click",
        "disabled": False,
        "custom_id": "cid",
    }


def test_link_button_drops_custom_id():
    b = build_button("Open", "ignored", style=BUTTON_LINK, url="https://x.dev")
    assert b["style"] == BUTTON_LINK
    assert b["url"] == "https://x.dev"
    assert "custom_id" not in b


def test_button_label_length_is_validated():
    with pytest.raises(ValueError, match="label exceeds"):
        build_button("x" * 200, "cid")


def test_button_custom_id_length_is_validated():
    with pytest.raises(ValueError, match="custom_id exceeds"):
        build_button("ok", "c" * 200)


def test_new_id_is_unique_and_bounded():
    ids = {new_id("btn:") for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) <= 100 for i in ids)
    assert all(i.startswith("btn:") for i in ids)


def test_action_row_groups_buttons():
    row = build_action_row(build_button("a", "1"), build_button("b", "2"))
    assert row["type"] == 1
    assert len(row["components"]) == 2


def test_action_row_rejects_unknown_component_types():
    with pytest.raises(ValueError, match="unsupported component type"):
        build_action_row({"type": 99})


def test_action_row_limit():
    with pytest.raises(ValueError, match="at most 5"):
        build_action_row(*[build_button(str(i), str(i)) for i in range(6)])


def test_action_row_needs_a_component():
    with pytest.raises(ValueError, match="at least one"):
        build_action_row()


def test_string_select_requires_options():
    with pytest.raises(ValueError, match="needs options"):
        build_select("cid")


def test_string_select_shape():
    s = build_select(
        "cid",
        [{"label": "One", "value": "1", "default": True}],
        placeholder="pick",
    )
    assert s["type"] == 3
    assert s["options"][0]["default"] is True
    assert s["placeholder"] == "pick"
    assert s["max_values"] == 1


def test_user_select_has_no_options():
    s = build_select("cid", select_type=SELECT_USER, min_values=1, max_values=3)
    assert s["type"] == 5  # user select component type
    assert "options" not in s
    assert s["min_values"] == 1


def test_action_row_accepts_one_select():
    select = build_select("cid", [{"label": "One", "value": "1"}])
    row = build_action_row(select)
    assert row == {"type": 1, "components": [select]}


def test_action_row_rejects_select_plus_button():
    select = build_select("cid", [{"label": "One", "value": "1"}])
    with pytest.raises(ValueError, match="must all be buttons"):
        build_action_row(select, build_button("x", "b"))


def test_select_option_limit():
    options = [{"label": str(i), "value": str(i)} for i in range(30)]
    with pytest.raises(ValueError, match="at most 25"):
        build_select("cid", options)


def test_modal_and_text_input():
    modal = build_modal("mid", "Feedback", build_text_input("name", "Your name"))
    assert modal["custom_id"] == "mid"
    assert modal["components"][0]["type"] == 1
    assert modal["components"][0]["components"][0]["type"] == 4
    assert modal_response(modal)["type"] == MODAL_SUBMIT


def test_modal_needs_inputs():
    with pytest.raises(ValueError, match="at least one input"):
        build_modal("mid", "Title")


# -- responses --------------------------------------------------------------
def test_message_response_shape():
    r = message_response("hi", components=[build_action_row(build_button("a", "1"))])
    assert r["type"] == CHANNEL_MESSAGE_WITH_SOURCE
    assert r["data"]["content"] == "hi"
    assert r["data"]["tts"] is False
    assert "flags" not in r["data"]


def test_ephemeral_sets_the_flag():
    assert message_response("hi", ephemeral=True)["data"]["flags"] == 64


def test_update_response_shape():
    r = update_response("edited", components=[])
    assert r["type"] == UPDATE_MESSAGE
    assert r["data"]["content"] == "edited"
    assert r["data"]["components"] == []


def test_defer_variants():
    assert defer()["type"] == 5
    assert defer(ephemeral=True)["data"]["flags"] == 64
    assert defer_thinking()["type"] == 6


def test_autocomplete_caps_choices():
    r = autocomplete_response([{"name": str(i), "value": str(i)} for i in range(40)])
    assert len(r["data"]["choices"]) == 25


def test_pong():
    assert pong()["type"] == 1


def test_string_select_constant():
    assert SELECT_STRING == 3


# -- REST wiring ------------------------------------------------------------
def test_run_interaction_posts():
    rest, rec = rest_with([{}])
    payload = {"t": 2, "id": "i1", "application_id": "a", "session_id": "s"}
    run(rest.run_interaction(payload))
    assert rec.calls[0][:2] == ("POST", "/interactions")
    assert rec.calls[0][2]["json_data"] == payload


def test_interaction_callback_puts():
    rest, rec = rest_with([{}])
    response = message_response("done")
    run(rest.interaction_callback("i1", "tok", response))
    assert rec.calls[0][:2] == ("PUT", "/interactions/i1/tok/callback")
    assert rec.calls[0][2]["json_data"] == response


def test_interaction_callback_uploads_files():
    rest, rec = rest_with([{}])
    run(
        rest.interaction_callback(
            "i1", "tok", message_response("done"), files=[b"hello"]
        )
    )
    assert rec.calls[0][:2] == ("PUT", "/interactions/i1/tok/callback")
    fields = rec.calls[0][2]["fields"]
    assert '"attachments"' in fields["payload_json"]
    assert rec.calls[0][2]["files"][0]["field"] == "files[0]"


def test_edit_and_fetch_original_response():
    rest, rec = rest_with([{"id": "m"}, {"id": "m"}])
    run(rest.edit_interaction_response("app", "tok", content="x"))
    run(rest.original_response("app", "tok"))
    assert rec.calls[0][:2] == ("PATCH", "/webhooks/app/tok/messages/@original")
    assert rec.calls[0][2]["json_data"]["content"] == "x"
    assert "session_id" not in rec.calls[0][2]["json_data"]
    assert rec.calls[1][:2] == ("GET", "/webhooks/app/tok/messages/@original")
    assert rec.calls[1][2].get("params") is None


# -- platform ---------------------------------------------------------------
def test_get_invite():
    rest, rec = rest_with([{"code": "abc"}])
    invite = run(rest.get_invite("abc"))
    assert invite.url == "https://discord.gg/abc"
    assert rec.calls[0][0] == "GET"


def test_follow_channel():
    rest, rec = rest_with(
        [{"channel_id": "c1", "webhook_id": "w1"}]
    )
    followed = run(rest.follow_channel("c1", target_channel_id="c2"))
    assert followed == {"channel_id": "c1", "webhook_id": "w1"}
    assert rec.calls[0][:2] == ("POST", "/channels/c1/followers")
    assert rec.calls[0][2]["json_data"] == {"webhook_channel_id": "c2"}
    assert "params" not in rec.calls[0][2] or rec.calls[0][2]["params"] is None


def test_webhook_lifecycle():
    rest, rec = rest_with([{"id": "w"}, [{"id": "w"}], {"id": "m"}, {}, {}])
    run(rest.create_webhook("c1", "hook"))
    run(rest.channel_webhooks("c1"))
    run(rest.execute_webhook("w", "tok", content="hi", wait=True))
    run(rest.delete_webhook("w"))
    run(rest.delete_webhook_with_token("w", "tok"))
    assert rec.calls[0][:2] == ("POST", "/channels/c1/webhooks")
    assert rec.calls[1][:2] == ("GET", "/channels/c1/webhooks")
    assert rec.calls[2][2]["params"] == {"wait": "true"}
    assert rec.calls[3][:2] == ("DELETE", "/webhooks/w")
    assert rec.calls[4][:2] == ("DELETE", "/webhooks/w/tok")


def test_execute_webhook_needs_a_payload():
    rest, _ = rest_with([])
    with pytest.raises(ValueError, match="something to send"):
        run(rest.execute_webhook("w", "tok"))


def test_newsletters():
    rest, rec = rest_with([[], {}, {"id": "m"}])
    run(rest.newsletters())
    run(rest.get_newsletter("n1"))
    run(rest.newsletter_message("n1", content="hello", embeds=[{"title": "t"}]))
    assert rec.calls[0][:2] == ("GET", "/newsletters")
    assert rec.calls[1][:2] == ("GET", "/newsletters/n1")
    # newsletter posts are multipart: a payload_json field plus the message
    assert rec.calls[2][0] == "POST"
    assert rec.calls[2][1] == "/newsletters/n1/messages"
    assert "payload_json" in rec.calls[2][2]["fields"]


# -- typing loop ------------------------------------------------------------
class FakeHttp:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def request(self, method, path, **kwargs):
        if self.fail:
            raise RuntimeError("typing failed")
        self.calls.append((method, path))
        return {}


def test_typing_loop_posts_immediately_and_refreshes():
    http = FakeHttp()
    loop = TypingLoop(http, "c1", interval=0.01)

    async def main():
        async with loop:
            await asyncio.sleep(0.05)
        return list(http.calls)

    calls = run(main())
    assert len(calls) >= 2, "expected an immediate post plus refreshes"
    assert calls[0] == ("POST", "/channels/c1/typing")
    assert loop.running is False


def test_typing_loop_start_is_idempotent():
    http = FakeHttp()
    loop = TypingLoop(http, "c1", interval=0.01)

    async def main():
        loop.start()
        first = loop._task
        loop.start()
        assert loop._task is first
        loop.stop()

    run(main())


def test_typing_loop_survives_a_failed_post():
    http = FakeHttp(fail=True)
    loop = TypingLoop(http, "c1", interval=0.01)

    async def main():
        async with loop:
            await asyncio.sleep(0.04)
        return loop.running

    assert run(main()) is False  # exited cleanly rather than dying on the error


def test_typing_loop_validates_the_interval():
    with pytest.raises(ValueError, match="interval must be positive"):
        TypingLoop(FakeHttp(), "c1", interval=0)


def test_default_refresh_is_under_the_expiry_window():
    assert TYPING_REFRESH_S <= 10.0


def test_rest_typing_loop_factory():
    rest = Rest(FakeHttp())  # type: ignore[arg-type]
    loop = rest.typing_loop("c1", interval=5)
    assert isinstance(loop, TypingLoop)
    assert loop.channel_id == "c1"
    assert loop.interval == 5
    assert rest.typing_loop("c1").interval == TYPING_REFRESH_S


def test_one_shot_typing_still_works():
    recorder = Recorder([{}])
    rest = Rest(recorder)  # type: ignore[arg-type]
    run(rest.typing("c1"))
    assert recorder.calls[0][:2] == ("POST", "/channels/c1/typing")
