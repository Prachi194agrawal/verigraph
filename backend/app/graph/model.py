"""In-memory code graph.

The parser, resolvers and history miner fill a `CodeGraph`; `store.GraphStore.write_graph` then writes it
to Neo4j in one pass. Keeping the graph in memory first lets the whole structure layer be unit-tested
on fixture repos without a database.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

# Higher is more precise. Edge properties keep the best resolver that produced the edge.
RESOLVER_RANK = {"scip": 4, "jedi": 3, "import": 2, "name": 1}


@dataclass
class Param:
    name: str
    kind: str  # positional_only | positional | var_positional | keyword_only | var_keyword
    has_default: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImportRecord:
    module: str | None  # absolute module imported from; None for a relative import above the top package
    name: str | None  # member for `from m import name` ("*" for wildcard); None for `import m`
    alias: str | None
    line: int
    is_from: bool
    level: int = 0  # leading dots of a relative import

    @property
    def bound_name(self) -> str | None:
        """The local name this import binds (`import a.b` binds `a`)."""
        if self.alias:
            return self.alias
        if self.is_from:
            return self.name
        return self.module.split(".")[0] if self.module else None


@dataclass
class CallSite:
    caller: str  # qualified name of the function that contains the call
    file_path: str
    callee: str  # source text of the called expression, e.g. "self.send"
    name: str | None  # last identifier ("send"); None for dynamic calls such as handlers[key]()
    line: int  # 1-based line of `name`
    col: int  # 0-based character column of `name`
    n_positional: int = 0
    keywords: list[str] = field(default_factory=list)
    star_args: bool = False
    star_kwargs: bool = False
    receiver_call: str | None = None  # "Session" for Session().get(...), "super" for super().run(...)


@dataclass
class FunctionDef:
    qualified_name: str
    name: str
    module: str
    file_path: str
    class_qn: str | None  # set for methods
    parent_qn: str | None  # enclosing function for nested functions
    signature: str
    params: list[Param]
    decorators: list[str]
    start_line: int  # first decorator line, if decorated
    end_line: int
    name_line: int  # line of the `def` name; used to map resolver results back to nodes
    docstring: str | None
    code: str
    is_async: bool = False
    is_nested: bool = False
    is_test: bool = False
    kind: str = "function"  # function | method | staticmethod | classmethod | property
    pytest_id: str | None = None
    self_class: str | None = None  # class that `self`/`cls` refers to inside this function
    self_name: str | None = None  # usually "self" or "cls"
    local_names: set[str] = field(default_factory=set)  # params and assigned names
    global_names: set[str] = field(default_factory=set)
    local_imports: list[ImportRecord] = field(default_factory=list)
    nested_defs: dict[str, str] = field(default_factory=dict)  # name -> qualified name
    assigned_calls: dict[str, list[str]] = field(default_factory=dict)  # x = Foo(...) -> {"x": ["Foo"]}
    return_exprs: list[str] = field(default_factory=list)  # callee of each `return Foo(...)`; "?" if unknown
    returns: str | None = None  # class it always returns an instance of, when inferable

    @property
    def is_method(self) -> bool:
        return self.class_qn is not None


@dataclass
class ClassDef:
    qualified_name: str
    name: str
    module: str
    file_path: str
    bases: list[str]  # base-class expressions as written, e.g. "adapters.BaseAdapter"
    decorators: list[str]
    start_line: int
    end_line: int
    name_line: int
    docstring: str | None
    is_nested: bool = False
    is_test: bool = False
    methods: dict[str, str] = field(default_factory=dict)  # name -> function qualified name
    attributes: set[str] = field(default_factory=set)  # class-level and self.<name> assignments


@dataclass
class FileRecord:
    path: str  # repository-relative POSIX path
    module: str
    is_test: bool
    is_package: bool
    loc: int
    sha1: str
    parse_errors: int = 0
    imports: list[ImportRecord] = field(default_factory=list)
    top_level: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (kind, qualified name)
    module_assignments: set[str] = field(default_factory=set)
    namespace: dict[str, list[str]] = field(default_factory=dict)  # resolved top-level bindings


@dataclass
class CallEdge:
    caller: str
    callee: str
    resolved_by: str
    lines: list[int] = field(default_factory=list)
    ambiguity: int = 1  # number of candidates when resolved by bare name

    def add(self, resolved_by: str, line: int, ambiguity: int) -> None:
        if RESOLVER_RANK[resolved_by] > RESOLVER_RANK[self.resolved_by]:
            self.resolved_by = resolved_by
            self.ambiguity = ambiguity
        if line not in self.lines:
            self.lines.append(line)


@dataclass
class Commit:
    sha: str
    title: str
    message: str
    author_name: str
    author_email: str
    authored_at: str  # ISO-8601 UTC
    committed_at: str
    order: int  # 0 = newest commit mined
    is_merge: bool = False
    pr_number: int | None = None


@dataclass
class PullRequest:
    number: int
    title: str | None = None
    body: str | None = None
    author: str | None = None
    merged_at: str | None = None
    url: str | None = None
    merge_commit_sha: str | None = None
    source: str = "git"  # git (from commit messages) | github (from the API)
    commits: set[str] = field(default_factory=set)
    fixes: set[int] = field(default_factory=set)


@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str | None = None
    created_at: str | None = None
    url: str | None = None
    labels: list[str] = field(default_factory=list)


@dataclass
class CodeGraph:
    repo_name: str
    repo_url: str
    commit_sha: str | None
    files: dict[str, FileRecord] = field(default_factory=dict)
    modules: dict[str, str] = field(default_factory=dict)  # module name -> file path
    source_roots: list[str] = field(default_factory=list)
    classes: dict[str, ClassDef] = field(default_factory=dict)
    functions: dict[str, FunctionDef] = field(default_factory=dict)
    call_sites: list[CallSite] = field(default_factory=list)
    calls: dict[tuple[str, str], CallEdge] = field(default_factory=dict)
    inherits: dict[tuple[str, str], str] = field(default_factory=dict)  # (child, parent) -> resolved_by
    external_bases: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    file_imports: dict[tuple[str, str, str], int] = field(default_factory=dict)  # (path, kind, target) -> line
    external_packages: dict[str, bool] = field(default_factory=dict)  # name -> is_stdlib
    tests: dict[tuple[str, str], str] = field(default_factory=dict)  # (test, function) -> resolved_by
    unresolved: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    # History layer
    commits: dict[str, Commit] = field(default_factory=dict)
    modifies: dict[tuple[str, str], dict] = field(default_factory=dict)  # (sha, function) -> props
    pull_requests: dict[int, PullRequest] = field(default_factory=dict)
    issues: dict[int, Issue] = field(default_factory=dict)
    author_logins: dict[str, str] = field(default_factory=dict)  # email -> GitHub login
    stats: dict = field(default_factory=dict)

    def add_call(self, caller: str, callee: str, resolved_by: str, line: int, ambiguity: int = 1) -> None:
        edge = self.calls.get((caller, callee))
        if edge is None:
            self.calls[(caller, callee)] = CallEdge(caller, callee, resolved_by, [line], ambiguity)
        else:
            edge.add(resolved_by, line, ambiguity)
