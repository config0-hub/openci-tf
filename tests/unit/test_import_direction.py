# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
import ast
from pathlib import Path
import pytest
LAYERS = {"core": 0, "platform": 1, "domain": 2, "services": 3}
# ratchet: do not add — domain should not perform I/O; shrink this list
DOMAIN_TO_PLATFORM_ALLOWED = frozenset({
    ("domain/accounts/aliases.py", "src.platform.aws.dynamo"),
    ("domain/engine/run_artifact_layout.py", "src.platform.aws.run_registry"),
    ("domain/intent/plan_lookup.py", "src.platform.aws.run_registry"),
    ("domain/intent/plan_lookup.py", "src.platform.aws.s3"),
    ("domain/locks/run_lock.py", "src.platform.aws.dynamo_transactions"),
})
def violations(edges):
    return [(source, target) for source, target in edges if source in LAYERS and target in LAYERS and (source == "core" or LAYERS[target] > LAYERS[source])]
def imported_names(node):
    return ([node.module] if isinstance(node, ast.ImportFrom) and node.module else []) + [name.name for name in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
def test_seeded_upward_import_is_detected(): assert violations([("core", "platform")])
def test_real_tree_only_imports_downward():
    edges = []
    for path in Path("src").rglob("*.py"):
        source = path.relative_to("src").parts[0]
        for node in ast.walk(ast.parse(path.read_text())):
            for name in imported_names(node):
                if name.startswith("src."): edges.append((source, name.split(".")[1]))
    assert not violations(edges), violations(edges)
def test_domain_to_platform_edges_match_ratchet():
    edges = set()
    for path in Path("src/domain").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            for name in imported_names(node):
                if name == "src.platform" or name.startswith("src.platform."): edges.add((path.relative_to("src").as_posix(), name))
    assert edges == DOMAIN_TO_PLATFORM_ALLOWED, {"unexpected": sorted(edges - DOMAIN_TO_PLATFORM_ALLOWED), "stale_allowlist": sorted(DOMAIN_TO_PLATFORM_ALLOWED - edges)}
def test_no_cross_package_relative_imports():
    offenders = [(path.as_posix(), node.level, node.module) for path in Path("src").rglob("*.py") for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom) and node.level >= 2]
    assert not offenders, offenders


# --- module-level import-cycle detection -----------------------------------
#
# The layer tests above only compare top-level package names ("core" vs
# "platform" vs ...), so a cycle *within* one layer's package (e.g. a
# package facade importing its own children, which import the facade back)
# is invisible to them. This builds the real resolved src.* module import
# graph and fails if any strongly connected component has more than one
# module — i.e. any real import cycle, anywhere in the tree.

def _module_name_for_path(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _relative_anchor(current: str, is_package: bool, level: int) -> str:
    """Package a relative import's dots resolve against (PEP 328 rules)."""
    parts = current.split(".")
    if not is_package:
        parts = parts[:-1]
    for _ in range(level - 1):
        if parts:
            parts = parts[:-1]
    return ".".join(parts)


def _import_edge_targets(node, current: str, is_package: bool, known: set) -> set:
    """Modules from `known` that `node` (an Import/ImportFrom) references.

    Resolves relative imports against `current`'s package. `from BASE
    import name` is syntactically identical whether `name` is a
    submodule of BASE or an attribute (function/class/constant) defined
    in BASE's own file, but only the two cases create different edges:
    a submodule reference edges to `BASE.name` (BASE itself need not be
    fully initialized — Python finds it in sys.modules mid-import), an
    attribute reference edges to `BASE` (its content must already
    exist). Treating every "from . import sibling_module" as also
    depending on the anchor package itself produces false-positive
    cycles for exactly the facade/child-submodule split this test
    exists to allow, so BASE is only added as a target when at least
    one imported name fails to resolve as one of its submodules.
    """
    targets = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in known:
                targets.add(alias.name)
        return targets
    if not isinstance(node, ast.ImportFrom):
        return targets
    if node.level > 0:
        anchor = _relative_anchor(current, is_package, node.level)
        base = f"{anchor}.{node.module}" if node.module else anchor
    else:
        base = node.module or ""
    if not base:
        return targets
    has_attribute_reference = False
    for alias in node.names:
        candidate = f"{base}.{alias.name}"
        if candidate in known:
            targets.add(candidate)
        else:
            has_attribute_reference = True
    if has_attribute_reference and base in known:
        targets.add(base)
    return targets


def _module_import_graph() -> dict:
    paths = {_module_name_for_path(path): path for path in Path("src").rglob("*.py")}
    known = set(paths)
    graph = {name: set() for name in known}
    for name, path in paths.items():
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                graph[name] |= _import_edge_targets(node, name, is_package, known) - {name}
    return graph


def _strongly_connected_components(graph: dict) -> list:
    """Tarjan's SCC algorithm over the module import graph."""
    index_counter = [0]
    index: dict = {}
    lowlink: dict = {}
    on_stack: dict = {}
    stack: list = []
    result: list = []

    def strongconnect(v):
        index[v] = lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in graph.get(v, ()):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w):
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.append(w)
                if w == v:
                    break
            result.append(component)

    for v in graph:
        if v not in index:
            strongconnect(v)
    return result


def test_no_module_import_cycles():
    graph = _module_import_graph()
    cycles = [sorted(component) for component in _strongly_connected_components(graph) if len(component) > 1]
    assert not cycles, cycles
