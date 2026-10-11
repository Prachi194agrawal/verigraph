"""Fast structural adapters and conservative call resolution for mixed-language repositories.

These adapters extract declarations and observed call expressions with Tree-sitter. A CALLS edge
is emitted only for an unambiguous lexical/file binding, declaring-class method, or explicit local
import. Unknown receivers and overloads remain unresolved, rather than becoming benchmark gold.
This is not compiler type checking or runtime dispatch analysis.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from app.graph.languages import LanguageSpec, discover_source_files, is_multilanguage_test_path
from app.graph.model import CallSite, ClassDef, CodeGraph, FileRecord, FunctionDef, ImportRecord, Param
from app.graph.parser import MAX_CODE_CHARS, ParsedFile

log = logging.getLogger(__name__)
FUNCTION_NODES = {
    "function_declaration", "function_definition", "function_item", "method_declaration",
    "constructor_declaration", "method_definition", "method", "singleton_method", "arrow_function",
    "function_expression", "generator_function_declaration", "generator_function", "local_function_statement",
}
CLASS_NODES = {
    "class_declaration", "class_definition", "class_specifier", "struct_specifier", "struct_item",
    "enum_item", "trait_item", "interface_declaration", "record_declaration", "class", "module",
    "object_definition", "trait_definition", "object_declaration",
}
CALL_NODES = {
    "call_expression", "method_invocation", "invocation_expression", "call", "function_call",
    "function_call_expression", "member_call_expression", "scoped_call_expression", "command",
}
ASSIGN_NODES = {"variable_declarator", "assignment_expression", "assignment", "short_var_declaration",
                "let_declaration", "local_variable_declaration", "assignment_statement"}
JS_LANGUAGES = {"javascript", "typescript", "tsx"}
IDENTIFIER = re.compile(r"^[A-Za-z_$][\w$]*$")
ANONYMOUS_NODES = {"lambda_expression", "closure_expression", "anonymous_function", "func_literal",
                   "lambda_literal", "anonymous_method_expression"}


@dataclass
class ParsedUnit:
    parsed: ParsedFile
    scopes: dict[str, str] = field(default_factory=dict)
    package: str | None = None
    default_export: str | None = None
    exports: set[str] = field(default_factory=set)


@dataclass
class _Scope:
    prefix: str
    fn: FunctionDef | None = None
    cls: ClassDef | None = None


def _field(node: Node, *fields: str) -> Node | None:
    for name in fields:
        found = node.child_by_field_name(name)
        if found is not None:
            return found
    return None


def _descendants(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


class StructuralParser:
    def __init__(self, spec: LanguageSpec):
        # Lazy import lets existing Python-only setups continue running; coverage records missing parsers.
        from tree_sitter_language_pack import get_parser

        self.parser = get_parser(spec.grammar)
        self.spec = spec

    def parse(self, source: bytes, path: str) -> ParsedUnit:
        return _Visitor(source, path, self.spec).run(self.parser.parse(source).root_node)


class _Visitor:
    def __init__(self, source: bytes, path: str, spec: LanguageSpec):
        self.src, self.path, self.spec = source, path, spec
        # Include the full path and language; Python and frontend modules can never collide.
        self.module = f"{spec.name}:{path}"
        self.file = _file_record(source, path, spec, "structural")
        self.classes: dict[str, ClassDef] = {}
        self.functions: dict[str, FunctionDef] = {}
        self.calls: list[CallSite] = []
        self.scopes: dict[str, str] = {}
        self.package: str | None = None
        self.default_export: str | None = None
        self.exports: set[str] = set()

    def text(self, node: Node | None) -> str:
        return self.src[node.start_byte:node.end_byte].decode("utf-8", "replace") if node else ""

    def run(self, root: Node) -> ParsedUnit:
        stack = [(root, _Scope(self.module))]
        while stack:
            node, scope = stack.pop()
            if node.type == "ERROR" or node.is_missing:
                self.file.parse_errors += 1
            if node.type in ANONYMOUS_NODES or self.spec.name == "ruby" and node.type in {"do_block", "block"}:
                continue
            if node.type in CLASS_NODES or (self.spec.name == "go" and node.type == "type_spec"
                                           and _field(node, "type") is not None
                                           and _field(node, "type").type in {"struct_type", "interface_type"}):
                cls = self._class(node, scope)
                if cls:
                    body = _field(node, "body", "type")
                    if body is None:
                        body = next((c for c in node.named_children if c.type in {
                            "class_body", "template_body", "declaration_list", "field_declaration_list"}), None)
                    if body:
                        stack.append((body, _Scope(cls.qualified_name, scope.fn, cls)))
                    continue
            if node.type == "impl_item":
                name = self.text(_field(node, "type"))
                cls = self.classes.get(f"{self.module}.{name}")
                body = _field(node, "body")
                if cls and body:
                    stack.append((body, _Scope(cls.qualified_name, None, cls)))
                    continue
            if node.type in {"namespace_definition", "mod_item"}:
                name, body = self.text(_field(node, "name")), _field(node, "body")
                if name and body:
                    stack.append((body, _Scope(f"{scope.prefix}.{name}", scope.fn, scope.cls)))
                    continue
            if node.type in FUNCTION_NODES:
                fn = self._function(node, scope)
                if fn:
                    body = _field(node, "body")
                    if body is None:
                        body = next((c for c in node.named_children if c.type == "function_body"), None)
                    if body:
                        stack.append((body, _Scope(fn.qualified_name, fn, self.classes.get(fn.class_qn))))
                    continue
                # Never attribute anonymous callback bodies to the enclosing caller. Their dispatch
                # and test registration need framework-specific modeling, beyond these adapters.
                continue
            if node.type in CALL_NODES and scope.fn:
                self._call(node, scope.fn)
            if node.type in ASSIGN_NODES:
                commonjs = scope.fn is None and self._commonjs(node)
                if not commonjs:
                    self._bindings(node, scope.fn)
            if node.type in {"import_statement", "import_declaration", "import_spec", "use_declaration",
                             "preproc_include", "using_directive", "require_expression", "include_expression"}:
                self._imports(node)
            elif node.type == "package_clause":
                self.package = self.text(node).removeprefix("package").strip()
            elif node.type == "package_declaration":
                self.package = self.text(node).removeprefix("package").strip().rstrip(";")
            elif node.type == "export_statement" and _field(node, "source") is None:
                for child in _descendants(node):
                    if child.type == "export_specifier":
                        value = self.text(_field(child, "name"))
                        alias = self.text(_field(child, "alias")) or value
                        # Aliased and re-exported symbols need binding records; do not guess them.
                        if alias == value:
                            self.exports.add(value)
            for child in reversed(node.named_children):
                stack.append((child, scope))
        return ParsedUnit(ParsedFile(self.file, list(self.classes.values()), list(self.functions.values()),
                                     self.calls), self.scopes, self.package, self.default_export, self.exports)

    def _name(self, node: Node) -> Node | None:
        direct = _field(node, "name")
        if direct is not None:
            return direct
        declarator = _field(node, "declarator")
        while declarator is not None:
            if declarator.type in {"identifier", "field_identifier", "qualified_identifier"}:
                return declarator
            declarator = _field(declarator, "declarator")
        return next((c for c in node.named_children if c.type in {
            "identifier", "simple_identifier", "type_identifier", "constant", "name", "word"}), None)

    def _class(self, node: Node, scope: _Scope) -> ClassDef | None:
        name_node = self._name(node)
        if name_node is None:
            return None
        name = self.text(name_node)
        qn = self._unique(f"{scope.prefix}.{name}", self.classes, node)
        bases: list[str] = []
        for child in node.named_children:
            if child.type in {"class_heritage", "superclass", "base_class_clause", "base_clause", "base_list",
                              "extends_clause", "inheritance_specifier"}:
                for item in child.named_children:
                    if item.type not in {"access_specifier", "argument_list", "type_arguments"}:
                        value = self.text(item).removeprefix("extends ").removeprefix("implements ")
                        if IDENTIFIER.fullmatch(value) or re.fullmatch(r"[\w.$:]+", value):
                            bases.append(value)
        cls = ClassDef(qn, name, self.module, self.path, bases, [], node.start_point.row + 1,
                       node.end_point.row + 1, name_node.start_point.row + 1, None,
                       is_nested=scope.fn is not None, is_test=self.file.is_test, language=self.spec.name)
        self.classes[qn] = cls
        if scope.prefix == self.module:
            self._top(name, "class", qn)
            if node.parent and node.parent.type == "export_statement":
                self.exports.add(name)
                if re.match(r"export\s+default\b", self.text(node.parent)):
                    self.default_export = qn
        return cls

    @staticmethod
    def _unique(qn: str, existing: dict, node: Node) -> str:
        return f"{qn}@{node.start_point.row + 1}:{node.start_point.column}" if qn in existing else qn

    def _top(self, name: str, kind: str, qn: str):
        # All declarations still appear as graph nodes; ambiguous top-level bindings are withheld.
        if name in self.file.module_assignments:
            return
        if name in self.file.top_level:
            self.file.top_level.pop(name)
            self.file.module_assignments.add(name)
        else:
            self.file.top_level[name] = (kind, qn)

    def _function(self, node: Node, scope: _Scope) -> FunctionDef | None:
        name_node = self._name(node)
        if node.type in {"arrow_function", "function_expression", "generator_function"}:
            parent = node.parent
            # Anonymous callbacks do not become invented function names. Named variable functions do.
            if parent and parent.type in {"variable_declarator", "assignment_expression",
                                          "field_definition", "public_field_definition", "property_declaration"}:
                name_node = _field(parent, "name", "key", "left", "property")
            else:
                return None
        if name_node is None:
            return None
        name = self.text(name_node)
        if not IDENTIFIER.fullmatch(name):
            return None
        cls = scope.cls if scope.fn is None else None
        self_name = "this" if cls else None
        receiver_type = None
        if self.spec.name == "go" and node.type == "method_declaration":
            recv = _field(node, "receiver")
            if recv:
                decl = next(iter(recv.named_children), None)
                self_name = self.text(_field(decl, "name")) if decl else None
                recv_type = self.text(_field(decl, "type")).lstrip("*") if decl else ""
                receiver_type = recv_type
                cls = self.classes.get(f"{self.module}.{recv_type}")
        if cls and self.spec.name in {"ruby", "rust"}:
            self_name = "self"
        if cls and self.spec.name == "php":
            self_name = "$this"
        parent_prefix = cls.qualified_name if cls and scope.fn is None else scope.prefix
        if receiver_type and cls is None:
            # A Go receiver type may be declared in another file. Keep the method visible without
            # inventing a class or treating it as a package-level callable function.
            parent_prefix = f"{self.module}.<receiver:{receiver_type}>"
        qn = self._unique(f"{parent_prefix}.{name}", self.functions, node)
        param_node = _field(node, "parameters")
        if param_node is None:
            decl = _field(node, "declarator")
            param_node = _field(decl, "parameters") if decl else None
        if param_node is None:
            param_node = next((c for c in node.named_children if c.type == "function_value_parameters"), None)
        params = self._params(param_node)
        if self.spec.name == "swift" and param_node is None:
            params = self._params(node)
        signature = name + (self.text(param_node) if param_node else "()")
        fn = FunctionDef(
            qn, name, self.module, self.path, cls.qualified_name if cls else None,
            scope.fn.qualified_name if scope.fn else None, " ".join(signature.split()), params, [],
            node.start_point.row + 1, node.end_point.row + 1, name_node.start_point.row + 1, None,
            self.text(node)[:MAX_CODE_CHARS], is_async=self.text(node).lstrip().startswith("async "),
            is_nested=scope.fn is not None, kind="method" if cls else "function",
            is_test=self._is_test(node, name), self_class=cls.qualified_name if cls else
            scope.fn.self_class if scope.fn else None,
            self_name=self_name or (scope.fn.self_name if scope.fn else None), language=self.spec.name,
        )
        prefix_text = self.text(node).split("{", 1)[0]
        if cls and (re.search(r"\bstatic\b", prefix_text) or
                    self.spec.name == "rust" and not any(p.name == "self" for p in params)):
            fn.kind = "staticmethod"
        fn.local_names.update(p.name for p in params)
        self.functions[qn] = fn
        self.scopes[qn] = parent_prefix
        if cls and scope.fn is None:
            if name in cls.methods:
                cls.methods.pop(name)
                cls.attributes.add(name)  # overload ambiguity: no exact method binding
            elif name not in cls.attributes:
                cls.methods[name] = qn
        elif scope.fn:
            if name in scope.fn.nested_defs:
                scope.fn.nested_defs.pop(name)
                scope.fn.local_names.add(name)
            else:
                scope.fn.nested_defs[name] = qn
        elif parent_prefix == self.module:
            self._top(name, "function", qn)
        parent = node.parent
        if parent and parent.type == "variable_declarator":
            parent = parent.parent.parent if parent.parent else None
        if parent and parent.type == "export_statement":
            self.exports.add(name)
            if re.match(r"export\s+default\b", self.text(parent)):
                self.default_export = qn
        return fn

    def _is_test(self, node: Node, name: str) -> bool:
        if self.spec.name == "rust":
            previous = node.prev_named_sibling
            return bool(previous and previous.type == "attribute_item" and
                        re.search(r"#\[\s*(?:\w+::)?test\s*\]", self.text(previous)))
        if not self.file.is_test:
            return False
        if self.spec.name == "go":
            return name.startswith(("Test", "Example", "Fuzz"))
        if self.spec.name in {"java", "kotlin", "csharp", "php"}:
            return name.lower().startswith("test") or bool(re.search(r"@(Test|ParameterizedTest)\b|\[(Test|Fact|Theory)\b",
                                                                     self.text(node)))
        return name.lower().startswith("test")

    def _params(self, node: Node | None) -> list[Param]:
        if node is None:
            return []
        out = []
        children = node.named_children
        if node.type == "function_declaration" and self.spec.name == "swift":
            children = [child for child in children if child.type == "parameter"]
        for child in children:
            name = _field(child, "name", "pattern", "declarator")
            if name is None and child.type in {"identifier", "simple_identifier", "self_parameter"}:
                name = child
            if name is None:
                name = next((c for c in child.named_children if c.type in {
                    "identifier", "simple_identifier", "variable_name"}), None)
            value = self.text(name).strip("&$")
            if value and IDENTIFIER.fullmatch(value):
                out.append(Param(value, "var_positional" if "..." in self.text(child) else "positional",
                                 _field(child, "value", "default_value") is not None))
        return out

    def _bindings(self, node: Node, fn: FunctionDef | None):
        value = _field(node, "value", "right")
        if value and value.type in FUNCTION_NODES:
            return
        name = _field(node, "name", "left", "pattern")
        if name:
            if IDENTIFIER.fullmatch(self.text(name)):
                if fn:
                    fn.local_names.add(self.text(name))
                    fn.nested_defs.pop(self.text(name), None)
                else:
                    self.file.module_assignments.add(self.text(name))
                    self.file.top_level.pop(self.text(name), None)
            elif name.type in {"expression_list", "pattern_list", "tuple_pattern", "object_pattern"}:
                for child in _descendants(name):
                    if child.type in {"identifier", "shorthand_property_identifier_pattern"}:
                        if fn:
                            fn.local_names.add(self.text(child))
                        else:
                            self.file.module_assignments.add(self.text(child))

    def _commonjs(self, node: Node) -> bool:
        """Explicit literal CommonJS imports/exports; never follow computed require expressions."""
        if self.spec.name not in JS_LANGUAGES:
            return False
        if node.type == "variable_declarator":
            value, name = _field(node, "value"), _field(node, "name")
            if not value or value.type != "call_expression" or self.text(_field(value, "function")) != "require":
                return False
            args = _field(value, "arguments")
            if not args or len(args.named_children) != 1 or args.named_children[0].type != "string":
                return False
            module = self.text(args.named_children[0]).strip("\"'")
            line = node.start_point.row + 1
            if name and name.type == "identifier":
                self.file.imports.append(ImportRecord(module, None, self.text(name), line, False))
                return True
            if name and name.type == "object_pattern":
                for item in name.named_children:
                    if item.type == "shorthand_property_identifier_pattern":
                        self.file.imports.append(ImportRecord(module, self.text(item), None, line, True))
                    elif item.type == "pair_pattern":
                        imported, alias = _field(item, "key"), _field(item, "value")
                        if imported and alias and alias.type == "identifier":
                            self.file.imports.append(ImportRecord(module, self.text(imported), self.text(alias),
                                                                 line, True))
                return True
        if node.type == "assignment_expression":
            left, right = self.text(_field(node, "left")), _field(node, "right")
            if left == "module.exports" and right and right.type == "object":
                for item in right.named_children:
                    if item.type == "shorthand_property_identifier":
                        self.exports.add(self.text(item))
                    elif item.type == "pair":
                        key, value = self.text(_field(item, "key")), self.text(_field(item, "value"))
                        if key == value and IDENTIFIER.fullmatch(key):
                            self.exports.add(key)
                return True
            if re.fullmatch(r"(?:module\.exports|exports)\.[\w$]+", left) and right:
                member = left.rsplit(".", 1)[-1]
                if right.type == "identifier" and self.text(right) == member:
                    self.exports.add(member)
                return True
        return False

    def _call(self, node: Node, fn: FunctionDef):
        callee_node = _field(node, "function", "name", "method")
        receiver = _field(node, "object", "receiver")
        if self.spec.name in {"kotlin", "swift"} and callee_node is None:
            callee_node = next(iter(node.named_children), None)
        if callee_node is None:
            return
        callee = self.text(callee_node)
        if receiver:
            callee = f"{self.text(receiver)}.{callee}"
        callee = re.sub(r"\s+", "", callee).replace("->", ".").replace("::", ".")
        name = callee.rsplit(".", 1)[-1]
        if not IDENTIFIER.fullmatch(name):
            name = None
        args = _field(node, "arguments")
        n_args = len(args.named_children) if args else 0
        self.calls.append(CallSite(fn.qualified_name, self.path, callee, name,
                                   callee_node.start_point.row + 1, callee_node.start_point.column, n_args))

    def _imports(self, node: Node):
        text, line = self.text(node), node.start_point.row + 1
        if self.spec.name in JS_LANGUAGES and node.type == "import_statement":
            source = _field(node, "source")
            module = self.text(source).strip("\"'")
            if not module:
                return
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            if clause is None:
                self.file.imports.append(ImportRecord(module, None, None, line, False))
            else:
                for item in _descendants(clause):
                    if item.type == "import_specifier":
                        self.file.imports.append(ImportRecord(module, self.text(_field(item, "name")),
                                                             self.text(_field(item, "alias")) or None, line, True))
                    elif item.type == "namespace_import":
                        alias = next((c for c in item.named_children if c.type == "identifier"), None)
                        self.file.imports.append(ImportRecord(module, None, self.text(alias) or None, line, False))
                    elif item.type == "identifier" and item.parent == clause:
                        self.file.imports.append(ImportRecord(module, "default", self.text(item), line, True))
        elif node.type == "preproc_include":
            path = self.text(_field(node, "path")).strip('"<>')
            if path:
                self.file.imports.append(ImportRecord(path, None, None, line, False))
        elif node.type in {"import_spec", "import_declaration", "use_declaration", "using_directive"}:
            if node.type == "import_declaration" and self.spec.name == "go":
                return  # import_spec child owns this declaration
            value = self.text(_field(node, "path", "argument"))
            if not value:
                value = re.sub(r"^(?:import|using)\s+(?:static\s+)?", "", text).rstrip("; ")
            value = value.strip('"')
            if value:
                self.file.imports.append(ImportRecord(value, None, self.text(_field(node, "name")) or None,
                                                     line, False))


def _file_record(source: bytes, path: str, spec: LanguageSpec, level: str) -> FileRecord:
    return FileRecord(path, f"{spec.name}:{path}", is_multilanguage_test_path(path), False,
                      source.count(b"\n") + (0 if source.endswith(b"\n") else 1),
                      hashlib.sha1(source).hexdigest(), language=spec.name, analysis_level=level)


class StaticResolver:
    def __init__(self, graph: CodeGraph, units: dict[str, ParsedUnit]):
        self.graph, self.units = graph, units
        self.by_scope: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.packages: dict[tuple[str, str], list[str]] = defaultdict(list)
        for path, unit in units.items():
            for fn in unit.parsed.functions:
                self.by_scope[(unit.scopes[fn.qualified_name], fn.name)].append(fn.qualified_name)
                if (fn.class_qn is None and fn.parent_qn is None and unit.package and fn.language == "go"
                        and unit.scopes[fn.qualified_name] == unit.parsed.file.module):
                    self.packages[(self._package(path, unit.package), fn.name)].append(fn.qualified_name)
            for cls in unit.parsed.classes:
                prefix = cls.qualified_name.rsplit(".", 1)[0]
                self.by_scope[(prefix, cls.name)].append(cls.qualified_name)

    @staticmethod
    def _package(path: str, package: str) -> str:
        return f"{posixpath.dirname(path)}:{package}"

    def _one(self, scope: str, name: str) -> str | None:
        candidates = self.by_scope.get((scope, name), [])
        return candidates[0] if len(candidates) == 1 else None

    def import_path(self, file_path: str, specifier: str) -> str | None:
        if not specifier.startswith("."):
            return None  # npm aliases, compiler paths and package imports need project configuration
        base = posixpath.normpath(posixpath.join(posixpath.dirname(file_path), specifier))
        if base.startswith("../"):
            return None
        language = self.graph.files[file_path].language
        suffixes = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
        probes = [base, *(base + ext for ext in suffixes), *(base + "/index" + ext for ext in suffixes)]
        # TypeScript commonly imports a .js specifier while its checked-in source ends in .ts.
        if base.endswith(".js"):
            probes.extend((base[:-3] + ".ts", base[:-3] + ".tsx"))
        candidates = [p for p in probes if p in self.units and
                      (self.graph.files[p].language == language or
                       self.graph.files[p].language in JS_LANGUAGES and language in JS_LANGUAGES)]
        return candidates[0] if len(set(candidates)) == 1 else None

    def binding(self, fn: FunctionDef, name: str) -> str | None:
        if name in fn.nested_defs:
            return fn.nested_defs[name]
        if name in fn.local_names:
            return None
        parent = fn.parent_qn
        while parent:
            enclosing = self.graph.functions[parent]
            if name in enclosing.nested_defs:
                return enclosing.nested_defs[name]
            if name in enclosing.local_names:
                return None
            parent = enclosing.parent_qn
        unit = self.units[fn.file_path]
        file = self.graph.files[fn.file_path]
        if name in file.module_assignments:
            return None
        if fn.language == "go" and unit.package:
            package_candidates = self.packages.get((self._package(fn.file_path, unit.package), name), [])
            if len(package_candidates) > 1:
                return None
        scope = unit.scopes[fn.qualified_name]
        # These languages require an explicit receiver for methods. A bare helper() in a JS,
        # Rust, PHP, or Go method refers to a lexical/package function, not this.helper().
        if scope == fn.class_qn and fn.language in JS_LANGUAGES | {"rust", "php", "go"}:
            scope = file.module
        target = self._one(scope, name)
        if target:
            return target
        target = file.top_level.get(name)
        if target:
            return target[1]
        if name in file.module_assignments:
            return None
        if fn.language == "go" and unit.package:
            candidates = self.packages.get((self._package(fn.file_path, unit.package), name), [])
            if len(candidates) == 1:
                return candidates[0]
        if fn.language in JS_LANGUAGES:
            imports = [imp for imp in file.imports if imp.bound_name == name]
            if len(imports) != 1:
                return None
            imp = imports[0]
            path = self.import_path(fn.file_path, imp.module or "")
            if path is None:
                return None
            if imp.name == "default":
                return self.units[path].default_export
            if imp.name and imp.name != "*":
                imported = self.graph.files[path].top_level.get(imp.name)
                return imported[1] if imported and imp.name in self.units[path].exports else None
            return self.graph.files[path].module
        return None

    def resolve(self, site: CallSite, fn: FunctionDef) -> str | None:
        parts = site.callee.split(".")
        if len(parts) == 1:
            target = self.binding(fn, parts[0])
        elif len(parts) == 2:
            receiver, member = parts
            if receiver == fn.self_name and fn.self_class:
                target = self._one(fn.self_class, member)
            else:
                base = self.binding(fn, receiver)
                if base in self.graph.classes:
                    target = self._one(base, member)
                    if target in self.graph.functions and self.graph.functions[target].kind != "staticmethod":
                        return None
                elif base in self.graph.modules:
                    path = self.graph.modules[base]
                    value = self.graph.files[path].top_level.get(member)
                    target = value[1] if value and member in self.units[path].exports else None
                else:
                    return None
        else:
            return None
        # Constructors and declarations are retained, but only concrete function targets become CALLS.
        return target if target in self.graph.functions else None

    def run(self, progress: Callable[[str], None] | None = None) -> dict:
        counts: Counter[str] = Counter()
        sites = [site for unit in self.units.values() for site in unit.parsed.call_sites]
        for i, site in enumerate(sites):
            if progress and i % 2000 == 0:
                progress(f"resolving multilingual calls {i}/{len(sites)}")
            fn = self.graph.functions[site.caller]
            target = self.resolve(site, fn)
            if target:
                self.graph.add_call(site.caller, target, "static", site.line)
                counts["static"] += 1
            else:
                key = "dynamic" if site.name is None else "unresolved"
                counts[key] += 1
                self.graph.unresolved[site.caller].append(site.name or site.callee)
        for path, unit in self.units.items():
            file = unit.parsed.file
            file.namespace = {name: [kind, qn] for name, (kind, qn) in file.top_level.items()}
            for imp in file.imports:
                imported_path = self.import_path(path, imp.module or "") if file.language in JS_LANGUAGES else None
                if imported_path:
                    self.graph.file_imports[(path, "module", self.graph.files[imported_path].module)] = imp.line
                elif imp.module:
                    self.graph.external_packages[imp.module] = False
                    self.graph.file_imports[(path, "external", imp.module)] = imp.line
            # Base classes must be scoped and unambiguous too; do not resolve globally by name.
            for cls in unit.parsed.classes:
                for base in cls.bases:
                    candidate = file.top_level.get(base)
                    if candidate and candidate[0] == "class" and candidate[1] != cls.qualified_name:
                        self.graph.inherits[(cls.qualified_name, candidate[1])] = "static"
                    else:
                        self.graph.external_bases[cls.qualified_name].append(base)
        return {"call_sites": len(sites), "resolvers": ["static"] if self.units else [],
                "by_resolver": {"static": counts["static"]} if counts["static"] else {},
                "external": 0, "unresolved": counts["unresolved"], "dynamic": counts["dynamic"]}


def append_multilanguage_structure(graph: CodeGraph, repo_root: Path,
                                   progress: Callable[[str], None] | None = None) -> dict:
    """Append non-Python structure after Python resolution; return additional call counters.

    Coverage is also merged into graph.stats. Every detected source has a File node, even when only
    file inventory is supported or a grammar is unavailable. Existing Python nodes are unchanged.
    """
    sources, skipped = discover_source_files(repo_root)
    parsers: dict[str, StructuralParser | None] = {}
    units: dict[str, ParsedUnit] = {}
    warnings: set[str] = set()
    for i, (path, spec) in enumerate(sources):
        if path in graph.files:
            continue
        if progress and i % 200 == 0:
            progress(f"parsing multilingual files {i}/{len(sources)}")
        try:
            source = (repo_root / path).read_bytes()
        except OSError:
            skipped["unreadable_files"] += 1
            continue
        if b"\0" in source:
            continue
        if spec.grammar is None or spec.name == "python":
            # .pyi declarations are not Python implementations and are inventory only.
            file = _file_record(source, path, spec, "file")
            parsed = None
        else:
            if spec.name not in parsers:
                try:
                    parsers[spec.name] = StructuralParser(spec)
                except (ImportError, LookupError, ValueError, RuntimeError) as exc:
                    log.warning("No structural parser for %s: %s", spec.name, exc)
                    warnings.add(f"{spec.name}: grammar unavailable; file inventory only")
                    parsers[spec.name] = None
            parser = parsers[spec.name]
            if parser:
                unit = parser.parse(source, path)
                units[path] = unit
                parsed, file = unit.parsed, unit.parsed.file
            else:
                parsed = None
                file = _file_record(source, path, spec, "file")
        graph.files[path] = file
        graph.modules[file.module] = path
        if parsed:
            graph.classes.update({c.qualified_name: c for c in parsed.classes})
            graph.functions.update({fn.qualified_name: fn for fn in parsed.functions})
            graph.call_sites.extend(parsed.call_sites)
    stats = StaticResolver(graph, units).run(progress)
    files = Counter(f.language for f in graph.files.values())
    functions = Counter(f.language for f in graph.functions.values())
    graph.stats.update({
        "languages": dict(sorted(files.items())), "functions_by_language": dict(sorted(functions.items())),
        "language_coverage": [{"language": lang, "files": count, "functions": functions[lang],
                               "analysis_levels": sorted({f.analysis_level for f in graph.files.values()
                                                          if f.language == lang})}
                              for lang, count in sorted(files.items())],
        "file_only_languages": sorted({f.language for f in graph.files.values() if f.analysis_level == "file"}),
        "coverage_warnings": sorted(warnings), "skipped_source_files": skipped,
    })
    return stats


def build_multilanguage_structure(repo_root: Path, repo_name: str, repo_url: str = "",
                                  commit_sha: str | None = None,
                                  progress: Callable[[str], None] | None = None) -> CodeGraph:
    """Convenience builder for non-Python adapter tests and integrations."""
    graph = CodeGraph(repo_name, repo_url, commit_sha)
    stats = append_multilanguage_structure(graph, repo_root, progress)
    graph.stats.update(stats)
    return graph
