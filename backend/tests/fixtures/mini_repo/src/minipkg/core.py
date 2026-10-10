"""Engines that use the helpers in utils."""

from . import utils
from .utils import helper, normalize as norm


class BaseEngine:
    """Base class for all engines."""

    def start(self):
        return self._prepare()

    def _prepare(self):
        return helper(1)


class Engine(BaseEngine):
    def __init__(self, name):
        self.name = name

    def run(self, x):
        y = norm(x)
        return self.start() + utils.scale(y)

    @staticmethod
    def build(name):
        return Engine(name)


class TurboEngine(Engine):
    def run(self, x):
        return super().run(x) * 2


def run(x):
    engine = Engine.build("default")
    return engine.run(x)


def dispatch(handlers, key):
    return handlers[key]()


def restart(engine):
    return engine.start()
