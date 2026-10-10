"""Symbol checker for generated code: does every repo symbol it uses exist, with a valid number of arguments?

Hallucination categories follow the Delulu benchmark:
  missing_symbol   invented API: a module/class attribute or import that the repository does not have
  wrong_arity      too many / too few positional arguments for a repository function
  unknown_keyword  a keyword argument the function does not accept
  undefined_name   a name that is defined nowhere (not local, imported, module-level or builtin)
  missing_import   a repository name used without importing it (it exists, but not in this module)
  syntax_error     the code does not parse
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field

from app.eval.index import BuildIndex
from app.graph.model import Param
from app.graph.symbols import Target

BUILTINS = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__class__"}
HALLUCINATION_CATEGORIES = ("missing_symbol", "wrong_arity", "unknown_keyword", "undefined_name", "missing_import",
                            "syntax_error")


@dataclass
class SymbolUse:
    symbol: str
    kind: str  # import | attribute | call | name | syntax
    exists: bool
    arity_ok: bool | None = None
    category: str | None = None
    detail: str = ""
    line: int | None = None
    target: str | None = None


@dataclass
class CodeCheck:
    syntax_ok: bool
    uses: list[SymbolUse] = field(default_factory=list)
    called_functions: set[str] = field(default_factory=set)

    @property
    def problems(self) -> list[SymbolUse]:
        return [u for u in self.uses if u.category]


def check_arity(params: list[Param], n_positional: int, keywords: list[str], drop_first: bool) -> tuple[bool, str, str]:
    """(ok, category, detail) for a call with the given arguments (no *args/**kwargs splats)."""
    ps = list(params)
    if drop_first and ps and ps[0].kind in ("positional", "positional_only"):
        ps = ps[1:]
    positional = [p for p in ps if p.kind in ("positional_only", "positional")]
    var_pos = any(p.kind == "var_positional" for p in ps)
    var_kw = any(p.kind == "var_keyword" for p in ps)
    by_keyword = {p.name for p in ps if p.kind in ("positional", "keyword_only")}
    if n_positional > len(positional) and not var_pos:
        return False, "wrong_arity", f"{n_positional} positional arguments, at most {len(positional)} accepted"
    unknown = [k for k in keywords if k not in by_keyword]
    if unknown and not var_kw:
        return False, "unknown_keyword", f"unknown keyword argument(s): {', '.join(unknown)}"
    filled = {p.name for p in positional[:n_positional]}
    doubled = [k for k in keywords if k in filled]
    if doubled:
        return False, "wrong_arity", f"multiple values for argument(s): {', '.join(doubled)}"
    filled |= set(keywords)
    missing = [p.name for p in ps if p.kind in ("positional_only", "positional", "keyword_only")
               and not p.has_default and p.name not in filled]
    if missing:
        return False, "wrong_arity", f"missing required argument(s): {', '.join(missing)}"
    return True, "", ""


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


class _Checker:
    def __init__(self, index: BuildIndex, module: str | None, class_qn: str | None, kind: str | None = None) -> None:
        self.index = index
        self.module = module
        self.class_qn = class_qn
        self.kind = kind  # the original function's kind (method, staticmethod, ...), when known
        self.env: dict[str, Target] = {}
        self.locals: set[str] = set()
        self.var_types: dict[str, Target] = {}
        self.open_vars: set[str] = set()  # self/cls: may be any subclass of the class being written
        self.assigned_attrs: set[tuple[str, str]] = set()  # (variable, attr) assigned in the snippet
        self.result = CodeCheck(syntax_ok=True)
        self._memo: dict[int, tuple[Target | None, bool]] = {}
        self._reported: set[tuple[str, str, int | None]] = set()

    def use(self, u: SymbolUse) -> None:
        key = (u.symbol, u.category or "", u.line)
        if key not in self._reported:
            self._reported.add(key)
            self.result.uses.append(u)

    # -- environment -------------------------------------------------------------------------------------

    def collect(self, tree: ast.AST) -> None:
        if self.module:
            for name, (kind, target) in self.index.namespaces.get(self.module, {}).items():
                self.env[name] = Target(kind, target)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.locals.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                a = node.args
                for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                    if arg is not None:
                        self.locals.add(arg.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                self.locals.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self.locals.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                self.locals.update(node.names)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name):
                self.assigned_attrs.add((node.value.id, node.attr))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self.imports(node)
        # `self` / `cls` of the method being written. The original's kind wins over the snippet's decorators:
        # the snippet replaces only the body, so a staticmethod stays static even if the model omits @staticmethod.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and self.class_qn:
                args = node.args.posonlyargs + node.args.args
                decorators = {d.id for d in node.decorator_list if isinstance(d, ast.Name)}
                static = self.kind == "staticmethod" or (self.kind is None and "staticmethod" in decorators)
                if args and not static:
                    is_cls = self.kind == "classmethod" or "classmethod" in decorators or args[0].arg == "cls"
                    self.var_types[args[0].arg] = Target("class" if is_cls else "instance", self.class_qn)
                    self.open_vars.add(args[0].arg)
                break
        # flow-insensitive inference of `x = RepoClass(...)`, only for names bound exactly once
        assigned: dict[str, list[ast.expr]] = {}
        bindings: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigned.setdefault(node.targets[0].id, []).append(node.value)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bindings[node.id] = bindings.get(node.id, 0) + 1
            elif isinstance(node, ast.arg):
                bindings[node.arg] = bindings.get(node.arg, 0) + 1
        for name, values in assigned.items():
            if (len(values) == 1 and bindings.get(name) == 1 and isinstance(values[0], ast.Call)
                    and name not in self.var_types):
                produced, _ = self.resolve(values[0], report=False)
                if produced is not None and produced.kind == "instance":
                    self.var_types[name] = produced

    def imports(self, node: ast.Import | ast.ImportFrom) -> None:
        idx = self.index
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                bound = alias.asname or top
                if idx.is_module(top):
                    if not idx.is_module(alias.name):
                        self.use(SymbolUse(alias.name, "import", False, category="missing_symbol",
                                           detail=f"module {alias.name} does not exist", line=node.lineno))
                    self.env[bound] = Target("module", alias.name if alias.asname else top)
                else:
                    self.env[bound] = Target("external", top)
            return
        module = node.module
        if node.level:
            module = idx.resolve_relative(self.module or "", node.level, node.module) if self.module else None
        for alias in node.names:
            bound = alias.asname or alias.name
            if alias.name == "*":
                continue
            if module and idx.is_module(module):
                sub = f"{module}.{alias.name}"
                if idx.is_module(sub):
                    self.env[bound] = Target("module", sub)
                    continue
                target = idx.resolve_name(module, alias.name) if module in idx.modules else None
                if target is None or target.name == "builtins":
                    self.use(SymbolUse(f"{module}.{alias.name}", "import", False, category="missing_symbol",
                                       detail=f"{module} has no {alias.name}", line=node.lineno))
                    self.locals.add(bound)
                else:
                    self.env[bound] = target
                    self.use(SymbolUse(f"{module}.{alias.name}", "import", True, line=node.lineno, target=target.name))
            else:
                self.env[bound] = Target("external", (module or "").split(".")[0])

    # -- resolution --------------------------------------------------------------------------------------

    def resolve(self, node: ast.expr, report: bool = True) -> tuple[Target | None, bool]:
        """(target, bound) for an expression; bound = reached through an instance (self is implicit)."""
        key = id(node)
        if report and key in self._memo:
            return self._memo[key]
        result: tuple[Target | None, bool] = (None, False)
        if isinstance(node, ast.Name):
            if node.id in self.var_types:
                result = (self.var_types[node.id], False)
            elif node.id in self.locals:
                result = (None, False)
            elif node.id in self.env:
                result = (self.env[node.id], False)
            elif node.id in BUILTINS:
                result = (Target("external", "builtins"), False)
        elif isinstance(node, ast.Attribute):
            base, _ = self.resolve(node.value, report)
            if base is not None and base.kind in ("module", "class", "instance"):
                member = self.index.member(base, node.attr)
                var = node.value.id if isinstance(node.value, ast.Name) else None
                if member is None and (
                        (base.kind != "module" and _is_dunder(node.attr))  # __dict__, __class__, __name__ ...
                        or (var in self.open_vars and self.index.defined_in_subclass(base.name, node.attr))):
                    pass  # exists, but its type is unknown
                elif member is None:
                    if report and (var, node.attr) not in self.assigned_attrs:
                        self.use(SymbolUse(f"{base.name}.{node.attr}", "attribute", False, category="missing_symbol",
                                           detail=f"{base.kind} {base.name} has no attribute {node.attr}",
                                           line=node.lineno))
                else:
                    if report and member.kind in ("function", "class", "module"):
                        self.use(SymbolUse(f"{base.name}.{node.attr}", "attribute", True, line=node.lineno,
                                           target=member.name))
                    result = (member, base.kind == "instance" or (base.kind == "class" and member.kind == "function"
                                                                  and self._is_classmethod(member.name)))
            elif base is not None and base.kind == "external":
                result = (base, False)
        elif isinstance(node, ast.Call):
            target, _ = self.resolve(node.func, report)
            if target is not None and target.kind == "class":
                result = (Target("instance", target.name), False)
            elif target is not None and target.kind == "function":
                fn = self.index.functions.get(target.name)
                if fn is not None and fn.returns:
                    result = (Target("instance", fn.returns), False)
        if report:
            self._memo[key] = result
        return result

    def _is_classmethod(self, qn: str) -> bool:
        fn = self.index.functions.get(qn)
        return fn is not None and fn.kind == "classmethod"

    # -- checks ------------------------------------------------------------------------------------------

    def check(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self.check_name(node)
            elif isinstance(node, ast.Attribute):
                self.resolve(node)
            elif isinstance(node, ast.Call):
                self.check_call(node)

    def check_name(self, node: ast.Name) -> None:
        name = node.id
        if name in self.locals or name in self.var_types or name in BUILTINS:
            return
        if name in self.env:
            t = self.env[name]
            if t.kind in ("function", "class"):
                self.use(SymbolUse(name, "name", True, line=node.lineno, target=t.name))
            return
        elsewhere = [qn for qn in self.index.symbols_ending_with(name) if qn.rsplit(".", 1)[0] in self.index.modules]
        if elsewhere:
            self.use(SymbolUse(name, "name", True, category="missing_import",
                               detail=f"{name} exists in the repo ({sorted(elsewhere)[0]}) but is not imported",
                               line=node.lineno))
        else:
            self.use(SymbolUse(name, "name", False, category="undefined_name", detail=f"{name} is not defined",
                               line=node.lineno))

    def check_call(self, node: ast.Call) -> None:
        target, bound = self.resolve(node.func)
        if target is None:
            return
        if target.kind == "class":
            init = self.index.constructor(target.name)
            if init is None:
                return
            fn_qn, drop_first = init, True
        elif target.kind == "function":
            fn_qn = target.name
            fn = self.index.functions.get(fn_qn)
            if fn is None:
                return
            drop_first = bound and fn.kind in ("method", "property", "classmethod")
        else:
            return
        self.result.called_functions.add(fn_qn)
        fn = self.index.functions[fn_qn]
        has_splat = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
        if has_splat:
            return
        ok, category, detail = check_arity(fn.params, len(node.args), [k.arg for k in node.keywords if k.arg],
                                           drop_first)
        label = ast.unparse(node.func)
        self.use(SymbolUse(label, "call", True, arity_ok=ok, category=category or None,
                           detail=detail or f"valid call of {fn_qn}", line=node.lineno, target=fn_qn))


def check_code(code: str, index: BuildIndex, module: str | None = None, class_qn: str | None = None,
               kind: str | None = None) -> CodeCheck:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return CodeCheck(False, [SymbolUse("<code>", "syntax", False, category="syntax_error", detail=str(exc),
                                           line=exc.lineno)])
    checker = _Checker(index, module, class_qn, kind)
    checker.collect(tree)
    checker.check(tree)
    return checker.result


def code_metrics(check: CodeCheck) -> dict[str, float]:
    repo_uses = [u for u in check.uses if u.kind in ("import", "attribute", "name")
                 and (u.target is not None or u.category == "missing_symbol")]
    missing = sum(u.category == "missing_symbol" for u in check.uses)
    calls = [u for u in check.uses if u.kind == "call"]
    bad_calls = sum(u.category in ("wrong_arity", "unknown_keyword") for u in calls)
    return {
        "syntax_ok": 1.0 if check.syntax_ok else 0.0,
        "symbol_refs": float(len(repo_uses)),
        "missing_symbol_rate": missing / len(repo_uses) if repo_uses else 0.0,
        "wrong_arity_rate": bad_calls / len(calls) if calls else 0.0,
        "undefined_names": float(sum(u.category == "undefined_name" for u in check.uses)),
        "hallucinated": 1.0 if any(u.category in HALLUCINATION_CATEGORIES for u in check.uses) else 0.0,
    }
