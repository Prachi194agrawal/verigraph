from minipkg import run as run_pkg
from minipkg.core import TurboEngine, run
from minipkg.utils import scale


def test_run():
    assert run(1) == 4


def test_run_reexport():
    assert run_pkg(2) == 6


def test_scale():
    assert scale(2) == 4


class TestEngine:
    def test_turbo(self):
        assert TurboEngine("t").run(1) == 8
