// VeriGraph code-graph schema (Neo4j 5.15+). Applied by GraphStore.ensure_schema().
//
// Every node carries `build_id` (one graph per pinned commit) and a `uid` = "<build_id>:<kind>:<key>".
// Neo4j Community has a single database, so builds of different repos/commits share it, separated by build_id.
//
// (:Repo)-[:CONTAINS]->(:Module)-[:CONTAINS]->(:File)
// (:File)-[:DEFINES]->(:Class)-[:DEFINES]->(:Function)          methods are Functions
// (:File)-[:DEFINES]->(:Function)
// (:Function)-[:DEFINES]->(:Function|Class)                      nested definitions
// (:Function)-[:CALLS {line, lines, resolved_by, ambiguity}]->(:Function)   scip | jedi | import | name
// (:File)-[:IMPORTS {line}]->(:Module)
// (:File)-[:IMPORTS {line}]->(:ExternalPackage)
// (:Class)-[:INHERITS {resolved_by, position}]->(:Class)
// (:Function {is_test: true})-[:TESTS {resolved_by}]->(:Function)
// (:Author)-[:AUTHORED]->(:Commit)-[:MODIFIES {lines_added, lines_deleted}]->(:Function)
// (:PullRequest)-[:INCLUDES]->(:Commit)
// (:PullRequest)-[:FIXES]->(:Issue)
// (:Function)-[:IMPLEMENTS {confidence, model}]->(:Feature)       LLM layer

CREATE CONSTRAINT repo_uid IF NOT EXISTS FOR (n:Repo) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT module_uid IF NOT EXISTS FOR (n:Module) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT file_uid IF NOT EXISTS FOR (n:File) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT class_uid IF NOT EXISTS FOR (n:Class) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT function_uid IF NOT EXISTS FOR (n:Function) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT external_uid IF NOT EXISTS FOR (n:ExternalPackage) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT author_uid IF NOT EXISTS FOR (n:Author) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT commit_uid IF NOT EXISTS FOR (n:Commit) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT pr_uid IF NOT EXISTS FOR (n:PullRequest) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT issue_uid IF NOT EXISTS FOR (n:Issue) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT feature_uid IF NOT EXISTS FOR (n:Feature) REQUIRE n.uid IS UNIQUE;

CREATE INDEX repo_build IF NOT EXISTS FOR (n:Repo) ON (n.build_id);
CREATE INDEX module_build IF NOT EXISTS FOR (n:Module) ON (n.build_id, n.name);
CREATE INDEX file_build IF NOT EXISTS FOR (n:File) ON (n.build_id, n.path);
CREATE INDEX class_build_name IF NOT EXISTS FOR (n:Class) ON (n.build_id, n.name);
CREATE INDEX class_build_qn IF NOT EXISTS FOR (n:Class) ON (n.build_id, n.qualified_name);
CREATE INDEX function_build_name IF NOT EXISTS FOR (n:Function) ON (n.build_id, n.name);
CREATE INDEX function_build_qn IF NOT EXISTS FOR (n:Function) ON (n.build_id, n.qualified_name);
CREATE INDEX external_build IF NOT EXISTS FOR (n:ExternalPackage) ON (n.build_id);
CREATE INDEX author_build IF NOT EXISTS FOR (n:Author) ON (n.build_id);
CREATE INDEX commit_build IF NOT EXISTS FOR (n:Commit) ON (n.build_id, n.sha);
CREATE INDEX pr_build IF NOT EXISTS FOR (n:PullRequest) ON (n.build_id, n.number);
CREATE INDEX issue_build IF NOT EXISTS FOR (n:Issue) ON (n.build_id, n.number);
CREATE INDEX feature_build IF NOT EXISTS FOR (n:Feature) ON (n.build_id);

CREATE FULLTEXT INDEX symbol_search IF NOT EXISTS FOR (n:Function|Class) ON EACH [n.name, n.qualified_name];
