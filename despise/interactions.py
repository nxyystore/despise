"""Interaction response builders.

Modern selfbot frameworks are mostly interaction responders, so these live
in their own module: the shapes are fiddly (every one of them has a required
``custom_id``) and getting them wrong fails silently at runtime rather than
with an exception.
"""

from __future__ import annotations

import uuid

# Interaction callback types.
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5
DEFERRED_UPDATE_MESSAGE = 6
UPDATE_MESSAGE = 7
APPLICATION_COMMAND_AUTOCOMPLETE_RESULT = 8
MODAL_SUBMIT = 9
PREMIUM_REQUIRED = 10
LAUNCH_ACTIVITY = 12

# Component styles.
BUTTON_PRIMARY = 1
BUTTON_SECONDARY = 2
BUTTON_SUCCESS = 3
BUTTON_DANGER = 4
BUTTON_LINK = 5
BUTTON_PREMIUM = 6

TEXT_INPUT_STYLE_SHORT = 1
TEXT_INPUT_STYLE_PARAGRAPH = 2

# Select menu types. These are the wire-level component types.
SELECT_STRING = 3
SELECT_USER = 5
SELECT_ROLE = 6
SELECT_MENTIONABLE = 7
SELECT_CHANNEL = 8

_SELECT_TYPES = {
    SELECT_STRING,
    SELECT_USER,
    SELECT_ROLE,
    SELECT_MENTIONABLE,
    SELECT_CHANNEL,
}

MAX_BUTTONS = 5
MAX_ACTIONS = 5
MAX_LABEL_LENGTH = 80
MAX_TITLE_LENGTH = 45
MAX_TEXT_INPUT_LABEL_LENGTH = 45
MAX_CUSTOM_ID_LENGTH = 100
MAX_PLACEHOLDER_LENGTH = 150
MAX_OPTIONS = 25
MAX_OPTION_LENGTH = 100


def _clip(value: str, limit: int, field: str) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters ({len(text)})")
    return text


def new_id(prefix: str = "") -> str:
    """A unique custom_id. Discord does not validate these beyond length."""
    token = uuid.uuid4().hex
    return f"{prefix}{token}"[:MAX_CUSTOM_ID_LENGTH]


def build_button(
    label: str,
    custom_id: str,
    *,
    style: int = BUTTON_PRIMARY,
    emoji: str | None = None,
    url: str | None = None,
    disabled: bool = False,
) -> dict:
    """A button component. ``url`` implies a link button and forbids custom_id."""
    component = {
        "type": 2,
        "style": style,
        "label": _clip(label, MAX_LABEL_LENGTH, "label"),
        "disabled": disabled,
    }
    if url is not None:
        component["style"] = BUTTON_LINK
        component["url"] = url
    else:
        component["custom_id"] = _clip(custom_id, MAX_CUSTOM_ID_LENGTH, "custom_id")
    if emoji:
        component["emoji"] = {"name": emoji}
    return component


def build_action_row(*components: dict) -> dict:
    """Wrap components in a row.

    A row holds up to five buttons, or exactly one select/text input. Selects
    still need to be placed in an action row when attached to a message.
    """
    if not components:
        raise ValueError("an action row needs at least one component")
    if len(components) > MAX_BUTTONS:
        raise ValueError(f"at most {MAX_BUTTONS} components per action row")
    if len(components) > 1 and any(c.get("type") != 2 for c in components):
        raise ValueError("multiple components in an action row must all be buttons")
    if len(components) == 1 and components[0].get("type") not in (2, *_SELECT_TYPES, 4):
        raise ValueError("unsupported component type in an action row")
    return {"type": 1, "components": list(components)}


def build_select(
    custom_id: str,
    options: list[dict] | None = None,
    *,
    placeholder: str | None = None,
    select_type: int = SELECT_STRING,
    min_values: int = 0,
    max_values: int = 1,
    disabled: bool = False,
) -> dict:
    """A select menu. ``options`` is required for SELECT_STRING only."""
    if select_type not in _SELECT_TYPES:
        raise ValueError(f"unknown select type {select_type!r}")
    component = {
        "type": select_type,
        "custom_id": _clip(custom_id, MAX_CUSTOM_ID_LENGTH, "custom_id"),
        "min_values": min_values,
        "max_values": max_values,
        "disabled": disabled,
    }
    if placeholder is not None:
        component["placeholder"] = _clip(placeholder, MAX_PLACEHOLDER_LENGTH, "placeholder")
    if select_type == SELECT_STRING:
        if not options:
            raise ValueError("a string select needs options")
        if len(options) > MAX_OPTIONS:
            raise ValueError(f"at most {MAX_OPTIONS} options per select")
        component["options"] = [
            {
                "label": _clip(o.get("label", ""), MAX_OPTION_LENGTH, "option label"),
                "value": _clip(o.get("value", ""), MAX_OPTION_LENGTH, "option value"),
                "description": _clip(
                    o.get("description", ""), MAX_OPTION_LENGTH, "option description"
                ),
                "emoji": o.get("emoji"),
                "default": bool(o.get("default", False)),
            }
            for o in options
        ]
    return component


def build_text_input(
    custom_id: str,
    label: str,
    *,
    style: int = TEXT_INPUT_STYLE_SHORT,
    placeholder: str | None = None,
    value: str | None = None,
    required: bool = True,
    min_length: int | None = None,
    max_length: int | None = None,
) -> dict:
    """A modal text input. Only valid inside a modal."""
    component = {
        "type": 4,
        "custom_id": _clip(custom_id, MAX_CUSTOM_ID_LENGTH, "custom_id"),
        "label": _clip(label, MAX_TEXT_INPUT_LABEL_LENGTH, "label"),
        "style": style,
        "required": required,
    }
    if placeholder is not None:
        component["placeholder"] = _clip(placeholder, MAX_PLACEHOLDER_LENGTH, "placeholder")
    if value is not None:
        component["value"] = value
    if min_length is not None:
        component["min_length"] = min_length
    if max_length is not None:
        component["max_length"] = max_length
    return component


def build_modal(custom_id: str, title: str, *components: dict) -> dict:
    """A modal. Text inputs are wrapped in the action rows Discord requires."""
    if not components:
        raise ValueError("a modal needs at least one input")
    if len(components) > MAX_ACTIONS:
        raise ValueError(f"at most {MAX_ACTIONS} inputs per modal")
    rows = []
    for component in components:
        kind = component.get("type")
        if kind == 1:
            rows.append(component)
        elif kind == 4:
            rows.append({"type": 1, "components": [component]})
        else:
            raise ValueError("modal components must be text inputs or action rows")
    return {
        "custom_id": _clip(custom_id, MAX_CUSTOM_ID_LENGTH, "custom_id"),
        "title": _clip(title, MAX_TITLE_LENGTH, "title"),
        "components": rows,
    }


def message_response(
    content: str | None = None,
    *,
    embeds: list | None = None,
    components: list | None = None,
    ephemeral: bool = False,
    allowed_mentions: dict | None = None,
) -> dict:
    """Callback type 4: reply to the interaction.

    File uploads are not represented here; pass ``files=`` to
    :meth:`Rest.interaction_callback` alongside the response.
    """
    data: dict = {"tts": False}
    if content is not None:
        data["content"] = content
    if embeds is not None:
        data["embeds"] = embeds
    if components:
        data["components"] = components
    if ephemeral:
        data["flags"] = 64  # EPHEMERAL
    if allowed_mentions is not None:
        data["allowed_mentions"] = allowed_mentions
    return {"type": CHANNEL_MESSAGE_WITH_SOURCE, "data": data}


def update_response(
    content: str | None = None,
    *,
    embeds: list | None = None,
    components: list | None = None,
) -> dict:
    """Callback type 7: edit the message the interaction came from."""
    data: dict = {}
    if content is not None:
        data["content"] = content
    if embeds is not None:
        data["embeds"] = embeds
    if components is not None:
        data["components"] = components
    return {"type": UPDATE_MESSAGE, "data": data}


def defer(ephemeral: bool = False) -> dict:
    """Callback type 5: acknowledge now, answer later."""
    data: dict = {}
    if ephemeral:
        data["flags"] = 64
    return {"type": DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, "data": data}


def defer_thinking() -> dict:
    """Callback type 6: show the thinking state on the invoking message."""
    return {"type": DEFERRED_UPDATE_MESSAGE, "data": {}}


def modal_response(modal: dict) -> dict:
    """Callback type 9: push a modal."""
    return {"type": MODAL_SUBMIT, "data": modal}


def autocomplete_response(choices: list[dict]) -> dict:
    """Callback type 8."""
    return {
        "type": APPLICATION_COMMAND_AUTOCOMPLETE_RESULT,
        "data": {"choices": choices[:MAX_OPTIONS]},
    }


def pong() -> dict:
    return {"type": PONG}


__all__ = [
    "PONG",
    "CHANNEL_MESSAGE_WITH_SOURCE",
    "DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE",
    "DEFERRED_UPDATE_MESSAGE",
    "UPDATE_MESSAGE",
    "APPLICATION_COMMAND_AUTOCOMPLETE_RESULT",
    "MODAL_SUBMIT",
    "PREMIUM_REQUIRED",
    "LAUNCH_ACTIVITY",
    "BUTTON_PRIMARY",
    "BUTTON_SECONDARY",
    "BUTTON_SUCCESS",
    "BUTTON_DANGER",
    "BUTTON_LINK",
    "BUTTON_PREMIUM",
    "TEXT_INPUT_STYLE_SHORT",
    "TEXT_INPUT_STYLE_PARAGRAPH",
    "SELECT_STRING",
    "SELECT_USER",
    "SELECT_ROLE",
    "SELECT_MENTIONABLE",
    "SELECT_CHANNEL",
    "new_id",
    "build_button",
    "build_action_row",
    "build_select",
    "build_text_input",
    "build_modal",
    "message_response",
    "update_response",
    "defer",
    "defer_thinking",
    "modal_response",
    "autocomplete_response",
    "pong",
]
