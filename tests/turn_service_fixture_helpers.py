"""Small transport fixture helpers; importing them must not compose the application."""
from codex_web.models import ThreadRunSettings


class _Settings:
    def __init__(self) -> None:
        self.remembered = None

    def get(self, thread_id):
        return ThreadRunSettings()

    def remember(self, thread_id, **kwargs):
        self.remembered = kwargs
        return ThreadRunSettings(**kwargs)

    @staticmethod
    def effective_developer_instructions(thread_id, value):
        return value


class _Recovery:
    def raise_if_thread_replaced(self, thread_id):
        return None

    def release_stale_active_turn(self, thread_id, reason):
        return None

    def replacement_thread_id(self, thread_id):
        return None

    async def replace_stale_bot_thread(self, binding, reason):
        return binding

    async def replace_stale_web_thread(self, thread_id, project, reason):
        return "replacement-thread"


class _Resume:
    @staticmethod
    def handoff_timeout():
        return 3.0

    @staticmethod
    def is_timeout_error(exc):
        return False

    @staticmethod
    def is_stale_thread_error(exc):
        return False

    def schedule(self, thread_id, project_id, params):
        raise AssertionError("forced resume was not expected")


class _Bindings:
    @staticmethod
    def for_thread(thread_id):
        return []
