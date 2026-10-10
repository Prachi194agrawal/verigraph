from app.graph.modules import assign_module_names, is_test_path, resolve_relative
from app.graph.parser import PythonParser

SOURCE = b'''
"""Module doc."""
from . import sibling
from ..pkg.mod import thing as alias
import os.path as osp, sys


@decorate(1)
async def f(a, /, b: int, *args, c, d: str = "x", e=2, **kw) -> int:
    """Doc of f."""
    try:
        g(1, *args, key=2, **kw)
    except ValueError as err:
        pass
    with open(a) as fh:
        osp.join(fh, err)
    for i, (j, k) in pairs:
        pass
    if (n := len(a)):
        pass
    handlers[k]()


class C(Base, mod.Other, Generic[T], metaclass=M):
    x = 1

    def __init__(self, *, k):
        self.y = k
        super().__init__()

    @staticmethod
    def s(v):
        def inner():
            return v
        return inner()

    @property
    def p(self):
        return Session().get("u")
'''


def parse():
    return PythonParser().parse(SOURCE, "pkg/sub/mod.py", "pkg.sub.mod", False)


def test_definitions_and_qualified_names():
    parsed = parse()
    names = {f.qualified_name for f in parsed.functions}
    assert names == {"pkg.sub.mod.f", "pkg.sub.mod.C.__init__", "pkg.sub.mod.C.s",
                     "pkg.sub.mod.C.s.<locals>.inner", "pkg.sub.mod.C.p"}
    assert [c.qualified_name for c in parsed.classes] == ["pkg.sub.mod.C"]
    assert parsed.file.top_level == {"f": ("function", "pkg.sub.mod.f"), "C": ("class", "pkg.sub.mod.C")}


def test_function_details():
    f = next(fn for fn in parse().functions if fn.name == "f")
    assert f.is_async and f.decorators == ["decorate"] and f.docstring == "Doc of f."
    assert f.start_line == 8 and f.name_line == 9  # decorator line vs def line
    assert [(p.name, p.kind, p.has_default) for p in f.params] == [
        ("a", "positional_only", False), ("b", "positional", False), ("args", "var_positional", False),
        ("c", "keyword_only", False), ("d", "keyword_only", True), ("e", "keyword_only", True),
        ("kw", "var_keyword", False)]
    assert f.signature.startswith("f(a, /, b: int") and f.signature.endswith("-> int")
    assert {"err", "fh", "i", "j", "k", "n"} <= f.local_names


def test_methods_kinds_and_self():
    by_name = {fn.name: fn for fn in parse().functions}
    assert by_name["__init__"].kind == "method" and by_name["__init__"].self_name == "self"
    assert by_name["s"].kind == "staticmethod" and by_name["s"].self_name is None
    assert by_name["p"].kind == "property"
    inner = by_name["inner"]
    assert inner.is_nested and inner.parent_qn == "pkg.sub.mod.C.s"
    assert by_name["s"].nested_defs == {"inner": "pkg.sub.mod.C.s.<locals>.inner"}


def test_class_bases_and_attributes():
    cls = parse().classes[0]
    assert cls.bases == ["Base", "mod.Other", "Generic"]  # keyword arguments (metaclass) are not bases
    assert {"x", "y"} <= cls.attributes  # class attribute and self.y


def test_imports_resolve_relative():
    imports = parse().file.imports
    got = [(i.module, i.name, i.alias, i.level) for i in imports]
    assert ("pkg.sub", "sibling", None, 1) in got
    assert ("pkg.pkg.mod", "thing", "alias", 2) in got
    assert ("os.path", None, "osp", 0) in got and ("sys", None, None, 0) in got


def test_return_and_assignment_tracking():
    src = (b"def make():\n    e = Engine()\n    e = other()\n    s = Session()\n    return s\n\n"
           b"def build(name):\n    if not name:\n        return None\n    return Engine(name)\n\n"
           b"class A:\n    def chain(self):\n        return self\n")
    fns = {fn.name: fn for fn in PythonParser().parse(src, "m.py", "m", False).functions}
    assert fns["make"].assigned_calls == {"e": ["Engine", "other"], "s": ["Session"]}
    assert fns["make"].return_exprs == ["?"]  # returns a variable: not inferable from the return alone
    assert fns["build"].return_exprs == ["Engine"]  # `return None` does not count
    assert fns["chain"].return_exprs == ["<self>"]


def test_rebound_names_are_not_typed():
    from app.graph.builder import build_structure

    src = ("class Foo:\n    def bar(self):\n        pass\n\n"
           "def ok():\n    x = Foo()\n    x.bar()\n\n"
           "def loop(items):\n    x = Foo()\n    for x in items:\n        x.bar()\n\n"
           "def param(x):\n    x = Foo()\n    x.bar()\n")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "m.py").write_text(src)
        g = build_structure(Path(tmp), "m", tmp, None, ["import"])
    assert ("m.ok", "m.Foo.bar") in g.calls  # bound once, from a call: typed
    assert ("m.loop", "m.Foo.bar") not in g.calls  # rebound by a for loop: unknown
    assert ("m.param", "m.Foo.bar") not in g.calls  # a parameter: unknown


def test_call_sites():
    calls = {(c.caller.rsplit(".", 1)[-1], c.callee) for c in parse().call_sites}
    assert ("f", "g") in calls and ("f", "osp.join") in calls and ("f", "len") in calls
    g = next(c for c in parse().call_sites if c.callee == "g")
    assert (g.n_positional, g.keywords, g.star_args, g.star_kwargs) == (1, ["key"], True, True)
    dynamic = next(c for c in parse().call_sites if c.callee == "handlers[k]")
    assert dynamic.name is None
    sup = next(c for c in parse().call_sites if c.name == "__init__")
    assert sup.receiver_call == "super"
    get = next(c for c in parse().call_sites if c.name == "get")
    assert get.receiver_call == "Session" and get.caller == "pkg.sub.mod.C.p"
    # decorator calls run in the enclosing (module) scope, so no Function owns them
    assert not any(c.callee == "decorate" for c in parse().call_sites)


def test_error_tolerance():
    parsed = PythonParser().parse(b"def ok():\n    return 1\n\nx = = 1\n\ndef also():\n    ok()\n", "x.py", "x", False)
    assert {f.name for f in parsed.functions} == {"ok", "also"}
    assert parsed.file.parse_errors > 0


def test_definition_ranges_match_parse_names():
    ranges = PythonParser().definition_ranges(SOURCE, "pkg.sub.mod")
    assert ("function", "pkg.sub.mod.C.s.<locals>.inner", 33, 34) in ranges
    assert {qn for kind, qn, _, _ in ranges if kind == "function"} == {f.qualified_name for f in parse().functions}


def test_module_names():
    paths = ["src/pkg/__init__.py", "src/pkg/a.py", "tests/test_a.py", "setup.py", "docs/conf.py", "conf.py"]
    infos = assign_module_names(paths)
    assert infos["src/pkg/__init__.py"].name == "pkg" and infos["src/pkg/__init__.py"].is_package
    assert infos["src/pkg/a.py"].name == "pkg.a" and infos["src/pkg/a.py"].root == "src"
    assert infos["tests/test_a.py"].name == "test_a"
    assert infos["docs/conf.py"].name == "docs.conf" and infos["conf.py"].name == "conf"  # collision fallback


def test_relative_and_test_paths():
    assert resolve_relative("a.b.c", False, 1, "") == "a.b"
    assert resolve_relative("a.b", True, 1, "x") == "a.b.x"
    assert resolve_relative("a.b.c", False, 2, "d") == "a.d"
    assert resolve_relative("a", False, 2, "") is None
    assert is_test_path("tests/unit/helpers.py") and is_test_path("pkg/test_x.py") and not is_test_path("pkg/x.py")
