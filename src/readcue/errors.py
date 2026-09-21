class ReadcueError(Exception):
    """Base class for errors that should be shown to the user without a traceback."""


class ConfigError(ReadcueError):
    pass


class NotFoundError(ReadcueError):
    pass


class LLMError(ReadcueError):
    pass


class NotifyError(ReadcueError):
    pass
