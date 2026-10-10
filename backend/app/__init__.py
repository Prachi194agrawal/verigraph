"""VeriGraph for Code: a code knowledge graph that is used to test and grade LLMs on a codebase."""

__version__ = "0.1.0"

# Stored on every graph build. Bump it whenever parser or resolver output changes,
# because frozen question sets are only valid against graphs from the same version.
PARSER_VERSION = "tree-sitter-python/1"
