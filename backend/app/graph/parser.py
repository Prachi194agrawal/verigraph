"""Tree-sitter parser: one Python file in, definitions + imports + call sites out.

Tree-sitter is error-tolerant, so files with syntax errors (or Python 2 code) still yield whatever
parses cleanly; the number of ERROR nodes is recorded on the File node.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from dataclasses import dataclass

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from app.graph.model import CallSite, ClassDef, FileRecord, FunctionDef, ImportRecord, Param
from app.graph.modules import is_test_path, resolve_relative

PY_LANGUAGE = Language(tspython.language())
MAX_CODE_CHARS = 12_000
MAX_DOCSTRING_CHARS = 2_000

# Statements that can contain definitions; definition_ranges() only descends into these.
_COMPOUND = {
    "module", "block", "decorated_definition", "if_statement", "elif_clause", "else_clause", "try_statement",
    "except_clause", "except_group_clause", "finally_clause", "with_statement", "for_statement",
    "while_statement", "match_statement", "case_clause",
}
_BINDING_NODES = {"assignment", "augmented_assignment", "for_statement", "for_in_clause", "named_expression",
                  "as_pattern"}
_PROPERTY_DECORATORS = {"property", "cached_property", "functools.cached_property"}


@dataclass
class ParsedFile:
    file: FileRecord
    classes: list[ClassDef]
    functions: list[FunctionDef]
    call_sites: list[CallSite]


@dataclass
class _Scope:
    kind: str  # module | class | function
    qn: str  # qualified-name prefix for definitions inside this scope
    function: FunctionDef | None  # owner of call sites found in this scope
    cls: ClassDef | None = None  # the class whose body this is


class PythonParser:
    def __init__(self) -> None:
        self._parser = Parser(PY_LANGUAGE)

    def parse(self, source: bytes, path: str, module: str, is_package: bool) -> ParsedFile:
        tree = self._parser.parse(source)
        return _FileVisitor(source, path, module, is_package).run(tree.root_node)

    def definition_ranges(self, source: bytes, module: str) -> list[tuple[str, str, int, int]]:
        """(kind, qualified_name, start_line, end_line) for each function and class.

        A fast pass for the history miner, which needs line ranges for two versions of every
        file a commit touched. Names are built exactly as in `parse`.
        """
        tree = self._parser.parse(source)
        out: list[tuple[str, str, int, int]] = []
        stack: list[tuple[Node, str, str]] = [(tree.root_node, module, "module")]
        while stack:
            node, prefix, scope_kind = stack.pop()
            for child in node.children:
                t = child.type
                if t in ("function_definition", "class_definition"):
                    name_node = child.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = _text(source, name_node)
                    qn = f"{prefix}.<locals>.{name}" if scope_kind == "function" else f"{prefix}.{name}"
                    outer = child.parent if child.parent is not None and child.parent.type == "decorated_definition" else child
                    kind = "function" if t == "function_definition" else "class"
                    out.append((kind, qn, outer.start_point[0] + 1, child.end_point[0] + 1))
                    body = child.child_by_field_name("body")
                    if body is not None:
                        stack.append((body, qn, kind))
                elif t in _COMPOUND:
                    stack.append((child, prefix, scope_kind))
        return out


def _text(src: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


class _FileVisitor:
    def __init__(self, source: bytes, path: str, module: str, is_package: bool) -> None:
        self.src = source
        self.lines = source.split(b"\n")
        self.path = path
        self.module = module
        self.is_package = is_package
        self.is_test_file = is_test_path(path)
        self.file = FileRecord(
            path=path, module=module, is_test=self.is_test_file, is_package=is_package,
            loc=source.count(b"\n") + (0 if source.endswith(b"\n") else 1), sha1=hashlib.sha1(source).hexdigest(),
        )
        self.classes: dict[str, ClassDef] = {}
        self.functions: dict[str, FunctionDef] = {}
        self.calls: list[CallSite] = []

    # -- traversal -----------------------------------------------------------------------------------

    def run(self, root: Node) -> ParsedFile:
        stack: list[tuple[Node, _Scope]] = [(root, _Scope("module", self.module, None))]
        while stack:
            node, scope = stack.pop()
            t = node.type
            if t == "ERROR" or node.is_missing:
                self.file.parse_errors += 1
            if t == "function_definition":
                self._function(node, scope, stack)
                continue
            if t == "class_definition":
                self._class(node, scope, stack)
                continue
            if t in ("import_statement", "import_from_statement"):
                self._import(node, scope)
                continue
            if t == "call":
                self._call(node, scope)
            elif t == "return_statement" and scope.kind == "function" and scope.function is not None:
                self._return(node, scope.function)
            elif t in _BINDING_NODES:
                self._bindings(node, scope)
            elif t in ("global_statement", "nonlocal_statement") and scope.function is not None:
                for ident in node.named_children:
                    if ident.type == "identifier":
                        scope.function.global_names.add(self._t(ident))
            for child in reversed(node.children):
                stack.append((child, scope))
        return ParsedFile(self.file, list(self.classes.values()), list(self.functions.values()), self.calls)

    # -- definitions ---------------------------------------------------------------------------------

    def _function(self, node: Node, scope: _Scope, stack: list) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._t(name_node)
        outer = node.parent if node.parent is not None and node.parent.type == "decorated_definition" else node
        decorators = [self._decorator_name(d) for d in outer.children if d.type == "decorator"]
        params_node = node.child_by_field_name("parameters")
        params = self._params(params_node) if params_node is not None else []
        body = node.child_by_field_name("body")

        is_method = scope.kind == "class"
        if is_method:
            qn, class_qn = f"{scope.qn}.{name}", scope.qn
        elif scope.kind == "function":
            qn, class_qn = f"{scope.qn}.<locals>.{name}", None
        else:
            qn, class_qn = f"{self.module}.{name}", None

        kind = "function"
        if is_method:
            kind = "method"
            if "staticmethod" in decorators:
                kind = "staticmethod"
            elif "classmethod" in decorators:
                kind = "classmethod"
            elif any(d in _PROPERTY_DECORATORS or d.endswith((".setter", ".getter", ".deleter")) for d in decorators):
                kind = "property"

        if is_method:
            self_class = class_qn
            first = params[0] if params else None
            self_name = (first.name if kind != "staticmethod" and first is not None
                         and first.kind in ("positional", "positional_only") else None)
        elif scope.function is not None:  # closures see the enclosing method's self
            self_class, self_name = scope.function.self_class, scope.function.self_name
        else:
            self_class = self_name = None

        ret = node.child_by_field_name("return_type")
        signature = name + (self._t(params_node) if params_node is not None else "()")
        if ret is not None:
            signature += f" -> {self._t(ret)}"

        fn = FunctionDef(
            qualified_name=qn, name=name, module=self.module, file_path=self.path, class_qn=class_qn,
            parent_qn=scope.function.qualified_name if scope.kind == "function" and scope.function else None,
            signature=" ".join(signature.split()), params=params, decorators=decorators,
            start_line=outer.start_point[0] + 1, end_line=node.end_point[0] + 1,
            name_line=name_node.start_point[0] + 1, docstring=self._docstring(body),
            code=self._dedented(outer)[:MAX_CODE_CHARS], is_async=node.children[0].type == "async",
            is_nested=scope.function is not None, kind=kind, self_class=self_class, self_name=self_name,
        )
        fn.local_names.update(p.name for p in params)
        for p in params:  # a parameter's type is unknown, even if it is reassigned later
            fn.assigned_calls.setdefault(p.name, []).append("?")
        fn.is_test = self._is_test_function(fn)
        if fn.is_test:
            chain = class_qn[len(self.module) + 1:].replace(".", "::") + "::" if class_qn else ""
            fn.pytest_id = f"{self.path}::{chain}{name}"

        existing = self.functions.get(qn)
        if existing is None:
            self.functions[qn] = fn
        else:
            # Redefinition (conditional defs, property setters, @overload stubs). Calls from every body
            # are attributed to one node; for overloads the real implementation (the last one) wins.
            if any(d.endswith("overload") for d in existing.decorators):
                for attr in ("signature", "params", "decorators", "start_line", "end_line", "name_line",
                             "docstring", "code", "kind"):
                    setattr(existing, attr, getattr(fn, attr))
            existing.local_names.update(fn.local_names)
            fn = existing

        if scope.kind == "class" and scope.cls is not None:
            scope.cls.methods.setdefault(name, qn)
        elif scope.kind == "function" and scope.function is not None:
            scope.function.nested_defs[name] = qn
        else:
            self.file.top_level[name] = ("function", qn)

        if params_node is not None:  # default values are evaluated in the enclosing scope
            stack.append((params_node, scope))
        if body is not None:
            stack.append((body, _Scope("function", qn, fn)))

    def _class(self, node: Node, scope: _Scope, stack: list) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._t(name_node)
        qn = f"{scope.qn}.<locals>.{name}" if scope.kind == "function" else f"{scope.qn}.{name}"
        outer = node.parent if node.parent is not None and node.parent.type == "decorated_definition" else node
        bases: list[str] = []
        supers = node.child_by_field_name("superclasses")
        if supers is not None:
            for arg in supers.named_children:
                expr = arg.child_by_field_name("value") if arg.type == "subscript" else arg
                if expr is not None and expr.type in ("identifier", "attribute"):
                    bases.append(" ".join(self._t(expr).split()))
            stack.append((supers, scope))
        body = node.child_by_field_name("body")
        cls = ClassDef(
            qualified_name=qn, name=name, module=self.module, file_path=self.path, bases=bases,
            decorators=[self._decorator_name(d) for d in outer.children if d.type == "decorator"],
            start_line=outer.start_point[0] + 1, end_line=node.end_point[0] + 1,
            name_line=name_node.start_point[0] + 1, docstring=self._docstring(body),
            is_nested=scope.kind != "module",
        )
        cls.is_test = self.is_test_file and (
            name.startswith("Test") or any(b.split(".")[-1].endswith("TestCase") for b in bases)
        )
        cls = self.classes.setdefault(qn, cls)
        if scope.kind == "function" and scope.function is not None:
            scope.function.nested_defs[name] = qn
        elif scope.kind == "module":
            self.file.top_level[name] = ("class", qn)
        if body is not None:
            # Class bodies run at definition time: their calls belong to the enclosing function, if any.
            stack.append((body, _Scope("class", qn, scope.function, cls)))

    def _is_test_function(self, fn: FunctionDef) -> bool:
        if not self.is_test_file or fn.is_nested or not fn.name.startswith("test"):
            return False
        if fn.class_qn is None:
            return True
        cls = self.classes.get(fn.class_qn)
        return cls is not None and cls.is_test

    # -- imports, calls, bindings --------------------------------------------------------------------

    def _import(self, node: Node, scope: _Scope) -> None:
        line = node.start_point[0] + 1
        records: list[ImportRecord] = []
        if node.type == "import_statement":
            for n in node.children_by_field_name("name"):
                if n.type == "aliased_import":
                    records.append(ImportRecord(self._t(n.child_by_field_name("name")), None,
                                                self._t(n.child_by_field_name("alias")), line, False))
                elif n.type == "dotted_name":
                    records.append(ImportRecord(self._t(n), None, None, line, False))
        else:
            mod = node.child_by_field_name("module_name")
            if mod is None:
                return
            level = 0
            if mod.type == "relative_import":
                prefix = next((c for c in mod.children if c.type == "import_prefix"), None)
                level = self._t(prefix).count(".") if prefix is not None else 1
                dotted = next((c for c in mod.children if c.type == "dotted_name"), None)
                module = resolve_relative(self.module, self.is_package, level, self._t(dotted) if dotted else "")
            else:
                module = self._t(mod)
            if any(c.type == "wildcard_import" for c in node.children):
                records.append(ImportRecord(module, "*", None, line, True, level))
            for n in node.children_by_field_name("name"):
                if n.type == "aliased_import":
                    records.append(ImportRecord(module, self._t(n.child_by_field_name("name")),
                                                self._t(n.child_by_field_name("alias")), line, True, level))
                elif n.type == "dotted_name":
                    records.append(ImportRecord(module, self._t(n), None, line, True, level))
        if scope.kind == "function" and scope.function is not None:
            scope.function.local_imports.extend(records)
        else:
            self.file.imports.extend(records)

    def _call(self, node: Node, scope: _Scope) -> None:
        fn = scope.function
        if fn is None:  # module-level or class-body call: no Function owns it
            return
        func = node.child_by_field_name("function")
        if func is None:
            return
        name_node: Node | None = None
        receiver_call: str | None = None
        if func.type == "identifier":
            name_node = func
        elif func.type == "attribute":
            name_node = func.child_by_field_name("attribute")
            obj = func.child_by_field_name("object")
            if obj is not None and obj.type == "call":
                inner = obj.child_by_field_name("function")
                if inner is not None and inner.type in ("identifier", "attribute"):
                    receiver_call = " ".join(self._t(inner).split())
        n_pos, keywords, star, dstar = 0, [], False, False
        args = node.child_by_field_name("arguments")
        if args is not None:
            if args.type == "generator_expression":
                n_pos = 1
            else:
                for a in args.named_children:
                    if a.type == "keyword_argument":
                        keywords.append(self._t(a.child_by_field_name("name")))
                    elif a.type == "list_splat":
                        star = True
                    elif a.type == "dictionary_splat":
                        dstar = True
                    elif a.type != "comment":
                        n_pos += 1
        anchor = name_node if name_node is not None else func
        row, byte_col = anchor.start_point[0], anchor.start_point[1]
        self.calls.append(CallSite(
            caller=fn.qualified_name, file_path=self.path, callee=" ".join(self._t(func).split())[:200],
            name=self._t(name_node) if name_node is not None else None, line=row + 1,
            col=self._char_col(row, byte_col), n_positional=n_pos, keywords=keywords, star_args=star,
            star_kwargs=dstar, receiver_call=receiver_call,
        ))

    def _return(self, node: Node, fn: FunctionDef) -> None:
        value = next((c for c in node.named_children if c.type != "comment"), None)
        if value is None or value.type == "none":
            return  # returning None does not change the inferred class
        if value.type == "call":
            func = value.child_by_field_name("function")
            if func is not None and func.type in ("identifier", "attribute"):
                fn.return_exprs.append(" ".join(self._t(func).split()))
                return
        if value.type == "identifier" and fn.self_name and self._t(value) == fn.self_name and fn.kind != "classmethod":
            fn.return_exprs.append("<self>")
            return
        fn.return_exprs.append("?")

    def _bindings(self, node: Node, scope: _Scope) -> None:
        t = node.type
        if t in ("assignment", "augmented_assignment", "for_statement", "for_in_clause"):
            target = node.child_by_field_name("left")
        elif t == "named_expression":
            target = node.child_by_field_name("name")
        else:  # as_pattern: with ... as x / except E as x / case ... as x
            target = node.child_by_field_name("alias")
        if target is None:
            return
        simple = t == "assignment" and target.type == "identifier" and scope.kind == "function" and scope.function
        if simple:  # x = Foo(...): remember the callee so the import resolver can type x
            right = node.child_by_field_name("right")
            func = right.child_by_field_name("function") if right is not None and right.type == "call" else None
            callee = " ".join(self._t(func).split()) if func is not None and func.type in ("identifier", "attribute") \
                else "?"
            scope.function.assigned_calls.setdefault(self._t(target), []).append(callee)
        stack = [target]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                self._bind(scope, self._t(n), record=not simple)
            elif n.type == "attribute":
                self._bind_attribute(n, scope)
            elif n.type not in ("subscript", "call"):
                stack.extend(n.named_children)

    def _bind(self, scope: _Scope, name: str, record: bool = True) -> None:
        if scope.kind == "function" and scope.function is not None:
            if name not in scope.function.global_names:
                scope.function.local_names.add(name)
                if record:  # bound by a loop, with/except, walrus, tuple unpacking...: its type is unknown
                    scope.function.assigned_calls.setdefault(name, []).append("?")
        elif scope.kind == "class" and scope.cls is not None:
            scope.cls.attributes.add(name)
        elif scope.kind == "module":
            self.file.module_assignments.add(name)

    def _bind_attribute(self, node: Node, scope: _Scope) -> None:
        fn = scope.function
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if fn is None or obj is None or attr is None or obj.type != "identifier":
            return
        if fn.self_class and fn.self_name == self._t(obj) and fn.self_class in self.classes:
            self.classes[fn.self_class].attributes.add(self._t(attr))

    # -- helpers -------------------------------------------------------------------------------------

    def _params(self, node: Node) -> list[Param]:
        params: list[Param] = []
        kind = "positional"
        for child in node.named_children:
            t = child.type
            if t == "identifier":
                params.append(Param(self._t(child), kind))
            elif t == "typed_parameter":
                inner = child.named_children[0] if child.named_children else None
                if inner is None:
                    continue
                if inner.type == "list_splat_pattern":
                    params.append(Param(self._splat_name(inner, "args"), "var_positional"))
                    kind = "keyword_only"
                elif inner.type == "dictionary_splat_pattern":
                    params.append(Param(self._splat_name(inner, "kwargs"), "var_keyword"))
                else:
                    params.append(Param(self._t(inner), kind))
            elif t in ("default_parameter", "typed_default_parameter"):
                params.append(Param(self._t(child.child_by_field_name("name")), kind, has_default=True))
            elif t == "list_splat_pattern":
                params.append(Param(self._splat_name(child, "args"), "var_positional"))
                kind = "keyword_only"
            elif t == "dictionary_splat_pattern":
                params.append(Param(self._splat_name(child, "kwargs"), "var_keyword"))
            elif t == "keyword_separator":
                kind = "keyword_only"
            elif t == "positional_separator":
                for p in params:
                    if p.kind == "positional":
                        p.kind = "positional_only"
        return params

    def _splat_name(self, node: Node, default: str) -> str:
        return self._t(node.named_children[0]) if node.named_children else default

    def _docstring(self, body: Node | None) -> str | None:
        if body is None:
            return None
        for child in body.named_children:
            if child.type == "comment":
                continue
            if (child.type == "expression_statement" and child.named_children
                    and child.named_children[0].type in ("string", "concatenated_string")):
                try:
                    value = ast.literal_eval(self._t(child.named_children[0]))
                except (ValueError, SyntaxError, MemoryError, RecursionError):
                    return None
                return inspect.cleandoc(value)[:MAX_DOCSTRING_CHARS] if isinstance(value, str) else None
            return None
        return None

    def _decorator_name(self, decorator: Node) -> str:
        expr = next((c for c in decorator.named_children if c.type != "comment"), None)
        if expr is None:
            return ""
        if expr.type == "call":
            expr = expr.child_by_field_name("function") or expr
        return " ".join(self._t(expr).split())

    def _dedented(self, node: Node) -> str:
        """Source of a definition with its indentation removed (the first line starts mid-line in the file)."""
        line = self.lines[node.start_point[0]] if node.start_point[0] < len(self.lines) else b""
        indent = line[: node.start_point[1]].decode("utf-8", "replace")
        return textwrap.dedent(indent + self._t(node)) if not indent.strip() else self._t(node)

    def _char_col(self, row: int, byte_col: int) -> int:
        line = self.lines[row] if row < len(self.lines) else b""
        return len(line[:byte_col].decode("utf-8", "replace"))

    def _t(self, node: Node | None) -> str:
        return _text(self.src, node)
