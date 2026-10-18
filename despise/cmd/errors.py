"""Command argument errors."""


class UsageError(Exception):
    """A command was invoked with bad arguments.

    Carries ``usage`` (``"!ban <member> [reason]"``) so error handlers
    can tell the user what works instead of showing a traceback.
    """

    def __init__(self, message: str, *, usage: str | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class CheckFailure(UsageError):
    """A @require check blocked a command.

    Subclasses UsageError so existing error handlers catch it without
    changes. Handlers can tell the two apart with isinstance: usage
    errors usually deserve a reply with ``.usage``; failed checks
    (wrong channel, non-owner probing) are usually best answered
    with silence.
    """

    def __init__(
        self,
        message: str,
        *,
        usage: str | None = None,
        predicate=None,
    ) -> None:
        super().__init__(message, usage=usage)
        self.predicate = predicate
