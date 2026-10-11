"""Language coverage and exact static edges, including cases that must stay unresolved."""

import pytest

from app.graph.languages import detect_language, discover_source_files
from app.graph.model import CodeGraph
from app.graph.multilanguage import append_multilanguage_structure, build_multilanguage_structure

SOURCES = [
    ("sample.js", "export function helper(x) {return x;} export function run(x) {return helper(x);}"),
    ("sample.ts", "export function helper(x: number): number {return x;} export function run(x: number) {return helper(x);}"),
    ("sample.tsx", "export const helper = (x: number) => x; export const run = (x: number) => helper(x);"),
    ("Sample.java", "class Sample { int helper(int x) {return x;} int run(int x) {return helper(x);} }"),
    ("sample.go", "package example\nfunc helper(x int) int {return x}\nfunc run(x int) int {return helper(x)}"),
    ("sample.rs", "fn helper(x: i32) -> i32 {x} fn run(x: i32) -> i32 {helper(x)}"),
    ("sample.c", "int helper(int x) {return x;} int run(int x) {return helper(x);}"),
    ("sample.cpp", "namespace sample { int helper(int x) {return x;} int run(int x) {return helper(x);} }"),
    ("Sample.cs", "class Sample { int helper(int x) {return x;} int run(int x) {return helper(x);} }"),
    ("sample.rb", "def helper(x)\n x\nend\ndef run(x)\n helper(x)\nend\n"),
    ("sample.php", "<?php function helper($x) {return $x;} function run($x) {return helper($x);}"),
    ("sample.kt", "fun helper(x: Int): Int {return x}\nfun run(x: Int): Int {return helper(x)}\n"),
    ("sample.swift", "func helper(x: Int) -> Int {return x}\nfunc run(x: Int) -> Int {return helper(x: x)}\n"),
    ("sample.scala", "object Sample { def helper(x: Int): Int = x; def run(x: Int): Int = helper(x) }"),
    ("sample.lua", "local function helper(x) return x end\nfunction run(x) return helper(x) end"),
    ("sample.sh", "helper() { echo \"$1\"; }\nrun() { helper \"$1\"; }"),
]


@pytest.mark.parametrize("path,source", SOURCES)
def test_structural_language_and_precise_call(tmp_path, path, source):
    (tmp_path / path).write_text(source)
    graph = build_multilanguage_structure(tmp_path, "example")
    functions = {fn.name: fn for fn in graph.functions.values()}
    assert {"helper", "run"} <= set(functions), path
    assert functions["run"].language == detect_language(path).name
    assert functions["run"].code and functions["run"].start_line >= 1
    edge = graph.calls[(functions["run"].qualified_name, functions["helper"].qualified_name)]
    assert edge.resolved_by == "static"
    assert graph.files[path].analysis_level == "structural"
    assert graph.stats["languages"][detect_language(path).name] == 1


def test_explicit_js_imports_nested_scope_and_tests(tmp_path):
    (tmp_path / "utils.ts").write_text("export function helper(x: number) {return x;}\n")
    (tmp_path / "main.ts").write_text(
        "import {helper as renamed} from './utils.js';\n"
        "export function run(x: number) { function inner() {return renamed(x);} return inner(); }\n"
    )
    (tmp_path / "main.test.ts").write_text(
        "import {run} from './main';\nexport function test_run() {run(1);}\n"
    )
    graph = build_multilanguage_structure(tmp_path, "example")
    assert ("typescript:main.ts.run.inner", "typescript:utils.ts.helper") in graph.calls
    assert ("typescript:main.ts.run", "typescript:main.ts.run.inner") in graph.calls
    assert ("typescript:main.test.ts.test_run", "typescript:main.ts.run") in graph.calls
    assert graph.functions["typescript:main.test.ts.test_run"].is_test
    assert graph.file_imports[("main.ts", "module", "typescript:utils.ts")] == 1


def test_literal_commonjs_imports_exports_and_arrow_methods(tmp_path):
    (tmp_path / "utils.cjs").write_text("function helper() {return 1;} module.exports = {helper};")
    (tmp_path / "main.cjs").write_text(
        "const {helper: renamed} = require('./utils.cjs'); const utils = require('./utils.cjs');"
        "class Example { run = () => {renamed(); utils.helper(); this.other();}; other() {} }"
    )
    graph = build_multilanguage_structure(tmp_path, "example")
    assert set(graph.calls) == {("javascript:main.cjs.Example.run", "javascript:utils.cjs.helper"),
                                ("javascript:main.cjs.Example.run", "javascript:main.cjs.Example.other")}


def test_no_global_name_guess_unknown_receiver_shadowed_name_or_callback_edge(tmp_path):
    (tmp_path / "other.js").write_text("export function helper() {return 1;}\n")
    (tmp_path / "main.js").write_text(
        "function local() {return 1;}\n"
        "function missing() {helper();}\n"
        "function shadowed(local) {local();}\n"
        "function receiver(obj) {obj.local();}\n"
        "function callback(items) {items.map(() => local());}\n"
        "function single_parameter(items) {items.map(x => local());}\n"
        "function named_expression(items) {items.map(function nested(x) {local();}); nested();}\n"
        "function object_property() {const x = {onlyObject: () => local()}; onlyObject();}\n"
        "function yes() {local();}\n"
    )
    graph = build_multilanguage_structure(tmp_path, "example")
    assert set(graph.calls) == {("javascript:main.js.yes", "javascript:main.js.local")}
    assert "helper" in graph.unresolved["javascript:main.js.missing"]
    assert "local" in graph.unresolved["javascript:main.js.shadowed"]


@pytest.mark.parametrize("path,source", [
    ("Sample.java", "class Sample {void helper() {} void run(){ items.map(x -> helper()); }}"),
    ("Sample.cs", "class Sample {void helper() {} void run(){items.Select(x => helper());}}"),
    ("sample.php", "<?php function helper() {} function run(){ $f = function() {helper();}; }"),
    ("sample.rs", "fn helper() {} fn run(){ let f = |x| helper(); }"),
    ("sample.cpp", "void helper() {} void run(){auto f = [](){helper();};}"),
    ("sample.go", "package app\nfunc helper() {}\nfunc run(){ f := func() {helper()} }"),
    ("sample.rb", "def helper\nend\ndef run\n items.each do |x|\n helper()\n end\nend"),
    ("sample.kt", "fun helper() {}\nfun run(){ items.map { x -> helper() } }"),
    ("sample.swift", "func helper() {}\nfunc run(){ items.map { x in helper() } }"),
    ("sample.lua", "function helper() end\nfunction run() local f = function() helper() end end"),
])
def test_anonymous_closure_calls_are_not_attributed_to_enclosing_function(tmp_path, path, source):
    (tmp_path / path).write_text(source)
    graph = build_multilanguage_structure(tmp_path, "example")
    assert not graph.calls


def test_reassigned_module_function_is_not_resolved(tmp_path):
    (tmp_path / "main.js").write_text("function helper() {}\nhelper = external; function run() {helper();}")
    graph = build_multilanguage_structure(tmp_path, "example")
    assert not graph.calls


def test_overloads_and_unexported_imports_are_not_gold(tmp_path):
    (tmp_path / "Sample.java").write_text(
        "class Sample { int helper(int x) {return x;} String helper(String x) {return x;}"
        "int run() {return helper(1);} }"
    )
    (tmp_path / "private.js").write_text("function secret() {return 1;}")
    (tmp_path / "main.js").write_text("import {secret} from './private'; function run() {secret();}")
    graph = build_multilanguage_structure(tmp_path, "example")
    assert len([fn for fn in graph.functions.values() if fn.name == "helper"]) == 2
    assert not graph.calls


def test_go_functions_share_only_declared_package(tmp_path):
    (tmp_path / "a.go").write_text("package example\nfunc helper() {}\n")
    (tmp_path / "b.go").write_text("package example\nfunc run() { helper() }\n")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "c.go").write_text("package example\nfunc outsider() {helper()}\n")
    graph = build_multilanguage_structure(tmp_path, "example")
    assert set(graph.calls) == {("go:b.go.run", "go:a.go.helper")}


def test_go_unknown_receiver_type_is_never_treated_as_free_function(tmp_path):
    (tmp_path / "a.go").write_text("package app\ntype Example struct {}")
    (tmp_path / "b.go").write_text("package app\nfunc (x *Example) helper() {}\nfunc run() {helper()}\n")
    graph = build_multilanguage_structure(tmp_path, "example")
    assert len(graph.functions) == 2
    assert not graph.calls


def test_rust_inline_test_module_has_distinct_lexical_scope(tmp_path):
    (tmp_path / "lib.rs").write_text(
        "fn helper() {}\n#[cfg(test)] mod tests {\n"
        "fn helper() {}\n#[test] fn test_example() { helper(); }\n}\n"
    )
    graph = build_multilanguage_structure(tmp_path, "example")
    assert graph.functions["rust:lib.rs.tests.test_example"].is_test
    assert set(graph.calls) == {("rust:lib.rs.tests.test_example", "rust:lib.rs.tests.helper")}


def test_declaring_class_method_and_static_class_member(tmp_path):
    (tmp_path / "main.ts").write_text(
        "class Sample {helper() {return 1;} static util() {return 1;}"
        " run() {this.helper(); Sample.util(); Sample.helper();}}"
    )
    graph = build_multilanguage_structure(tmp_path, "example")
    assert set(graph.calls) == {("typescript:main.ts.Sample.run", "typescript:main.ts.Sample.helper"),
                                ("typescript:main.ts.Sample.run", "typescript:main.ts.Sample.util")}


@pytest.mark.parametrize("path,source", [
    ("main.js", "class Example { helper() {} run() { helper(); } }"),
    ("main.rs", "struct Example {} impl Example {fn helper(&self) {} fn run(&self) {helper();}}"),
    ("main.php", "<?php class Example {function helper() {} function run() {helper();}}"),
    ("main.go", "package app\ntype Example struct {}\nfunc (x *Example) helper() {}\nfunc (x *Example) run() {helper()}"),
])
def test_languages_requiring_receivers_never_infer_bare_method_calls(tmp_path, path, source):
    (tmp_path / path).write_text(source)
    graph = build_multilanguage_structure(tmp_path, "example")
    assert len(graph.functions) == 2
    assert not graph.calls


def test_file_only_coverage_and_bounded_discovery(tmp_path):
    (tmp_path / "app.dart").write_text("void main() {}")
    (tmp_path / "App.vue").write_text("<template><p>Hello</p></template>")
    (tmp_path / "README.md").write_text("# Example")
    (tmp_path / "large.ts").write_bytes(b"x" * 1_000_001)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "vendor.js").write_text("function vendor() {}")
    (tmp_path / "linked.js").symlink_to(tmp_path / "app.dart")
    sources, skipped = discover_source_files(tmp_path)
    assert {path for path, _ in sources} == {"app.dart", "App.vue", "README.md"}
    assert skipped["large_files"] == 1 and skipped["symlinks"] == 1
    graph = build_multilanguage_structure(tmp_path, "example")
    assert graph.files["app.dart"].analysis_level == "file"
    assert graph.stats["file_only_languages"] == ["dart", "markdown", "vue"]
    assert not graph.functions


def test_mixed_append_preserves_python_graph(tmp_path):
    from app.graph.builder import parse_repository

    (tmp_path / "sample.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "sample.js").write_text("export function helper() {return 1;}")
    graph = parse_repository(tmp_path, "example", "", None)
    python_fn = graph.functions["sample.helper"]
    append_multilanguage_structure(graph, tmp_path)
    assert graph.functions["sample.helper"] is python_fn
    assert python_fn.language == "python"
    assert "javascript:sample.js.helper" in graph.functions
    assert graph.stats["functions_by_language"] == {"javascript": 1, "python": 1}


def test_missing_grammar_is_visible_file_inventory(tmp_path, monkeypatch):
    import app.graph.multilanguage as adapter

    def unavailable(spec):
        raise ImportError("grammar pack missing")

    monkeypatch.setattr(adapter, "StructuralParser", unavailable)
    (tmp_path / "sample.ts").write_text("export function run() {}")
    graph = CodeGraph("example", "", None)
    append_multilanguage_structure(graph, tmp_path)
    assert graph.files["sample.ts"].analysis_level == "file"
    assert graph.stats["coverage_warnings"] == ["typescript: grammar unavailable; file inventory only"]
    assert not graph.functions


def test_unfamiliar_text_language_has_inventory_without_binary_or_lockfiles(tmp_path):
    (tmp_path / "example.customlang").write_text("custom declaration example {}")
    (tmp_path / "binary.customlang").write_bytes(b"\x89binary\0\xff")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}')
    graph = build_multilanguage_structure(tmp_path, "example")
    assert set(graph.files) == {"example.customlang"}
    assert graph.files["example.customlang"].language == "other"
    assert graph.files["example.customlang"].analysis_level == "file"
    assert not graph.functions
