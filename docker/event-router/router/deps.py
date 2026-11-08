"""The dependency graph from inventory/dependencies.yml (section 4.7).

Only names and `needs` edges are used here; probes are run by the executor on
the host, never by the router.
"""

import yaml

# Every router container lives on /persistent_data (Docker's data-root), so it
# is implied for every container; dependencies.yml lists it only where it
# matters for the reader.
IMPLIED_ROOT = "persistent_data"
SITE = "site"   # maintenance scope that covers every node


class Graph:
    def __init__(self, nodes):
        self.nodes = nodes   # name -> dict (kind, needs, probe, ...)
        self._by_container = {}
        for name, node in nodes.items():
            probe = node.get("probe") or {}
            if isinstance(probe, dict) and "container" in probe:
                self._by_container[str(probe["container"])] = name

    @classmethod
    def load(cls, path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data):
        nodes = {}
        for name, node in (data.get("nodes") or {}).items():
            node = dict(node or {})
            node["needs"] = list(node.get("needs") or [])
            nodes[str(name)] = node
        return cls(nodes)

    def errors(self):
        out = []
        for name, node in self.nodes.items():
            for dep in node["needs"]:
                if dep not in self.nodes:
                    out.append(f"node {name}: needs unknown node {dep}")
        return out

    def node_for_container(self, container):
        """The node whose probe is this container, else a pseudo-node."""
        if container in self._by_container:
            return self._by_container[container]
        if container in self.nodes:
            return container
        return f"container:{container}"

    def needs(self, node):
        if node in self.nodes:
            return set(self.nodes[node]["needs"])
        if node.startswith("container:") and IMPLIED_ROOT in self.nodes:
            return {IMPLIED_ROOT}
        return set()

    def ancestors(self, node):
        """Transitive `needs` closure, without the node itself."""
        seen, stack = set(), list(self.needs(node))
        if self._is_container(node) and IMPLIED_ROOT in self.nodes and node != IMPLIED_ROOT:
            stack.append(IMPLIED_ROOT)
        while stack:
            n = stack.pop()
            if n in seen or n == node:
                continue
            seen.add(n)
            stack.extend(self.needs(n))
        return seen

    def in_scope(self, node, scope):
        """True if node is in a maintenance scope: a scope node itself, or a
        dependent of one (13.2), or the scope is the whole site."""
        scope = set(scope)
        if SITE in scope or node in scope:
            return True
        return bool(self.ancestors(node) & scope)

    def _is_container(self, node):
        if node.startswith("container:"):
            return True
        return (self.nodes.get(node) or {}).get("kind") == "container"
