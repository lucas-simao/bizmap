from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Confidence(StrEnum):
    EXACT = "exact"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


CONFIDENCE_RANK = {
    Confidence.EXACT: 3,
    Confidence.INFERRED: 2,
    Confidence.UNKNOWN: 1,
}


NODE_TYPES = ("entrypoint", "function", "table", "column", "db_trigger")
EDGE_TYPES = ("calls", "reads", "writes", "triggers")


def parse_confidence(value: str | Confidence | None) -> Confidence:
    if isinstance(value, Confidence):
        return value
    if not value:
        return Confidence.INFERRED
    try:
        return Confidence(str(value).lower())
    except ValueError:
        return Confidence.UNKNOWN


def better_confidence(a: Confidence, b: Confidence) -> Confidence:
    return a if CONFIDENCE_RANK[a] >= CONFIDENCE_RANK[b] else b


@dataclass
class Evidence:
    file: str
    line: int
    symbol: str

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "symbol": self.symbol}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Evidence | None:
        if not data:
            return None
        return cls(
            file=str(data.get("file", "")),
            line=int(data.get("line") or 0),
            symbol=str(data.get("symbol", "")),
        )


@dataclass
class Node:
    id: str
    type: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {"id": self.id, "type": self.type}
        out.update(self.attrs)
        return out


@dataclass
class Edge:
    id: str
    type: str
    from_id: str
    to_id: str
    confidence: Confidence = Confidence.EXACT
    evidence: Evidence | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "from": self.from_id,
            "to": self.to_id,
            "confidence": self.confidence.value,
        }
        if self.evidence:
            out["evidence"] = self.evidence.to_dict()
        out.update(self.attrs)
        return out


def edge_id(etype: str, from_id: str, to_id: str) -> str:
    return f"{etype}:{from_id}->{to_id}"


class Graph:
    """Lineage graph: nodes + edges + reconstructed schema."""

    def __init__(self) -> None:
        self.version = "0.1.3"
        self.generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.schema: dict[str, Any] = {"tables": {}}
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}

    def add_node(self, node_id: str, node_type: str, **attrs: Any) -> Node:
        existing = self.nodes.get(node_id)
        if existing:
            for key, value in attrs.items():
                if value in (None, "", [], {}):
                    continue
                current = existing.attrs.get(key)
                if current in (None, "", [], {}):
                    existing.attrs[key] = value
            return existing
        node = Node(id=node_id, type=node_type, attrs=attrs)
        self.nodes[node_id] = node
        return node

    def add_edge(
        self,
        etype: str,
        from_id: str,
        to_id: str,
        *,
        confidence: Confidence | str = Confidence.EXACT,
        evidence: Evidence | None = None,
        **attrs: Any,
    ) -> Edge:
        conf = parse_confidence(confidence)
        eid = edge_id(etype, from_id, to_id)
        existing = self.edges.get(eid)
        if existing:
            existing.confidence = better_confidence(existing.confidence, conf)
            if evidence and not existing.evidence:
                existing.evidence = evidence
            for key, value in attrs.items():
                if value not in (None, "", [], {}):
                    existing.attrs.setdefault(key, value)
            return existing
        edge = Edge(
            id=eid,
            type=etype,
            from_id=from_id,
            to_id=to_id,
            confidence=conf,
            evidence=evidence,
            attrs=attrs,
        )
        self.edges[eid] = edge
        return edge

    def get(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def incoming(self, node_id: str, etype: str | None = None) -> list[Edge]:
        return [
            e
            for e in self.edges.values()
            if e.to_id == node_id and (etype is None or e.type == etype)
        ]

    def outgoing(self, node_id: str, etype: str | None = None) -> list[Edge]:
        return [
            e
            for e in self.edges.values()
            if e.from_id == node_id and (etype is None or e.type == etype)
        ]

    def to_dict(self) -> dict[str, Any]:
        nodes = sorted(self.nodes.values(), key=lambda n: (n.type, n.id))
        edges = sorted(self.edges.values(), key=lambda e: (e.type, e.from_id, e.to_id))
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "schema": self.schema,
            "nodes": [n.to_dict() for n in nodes],
            "edges": [e.to_dict() for e in edges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Graph:
        graph = cls()
        graph.version = str(data.get("version") or "0.1.3")
        graph.generated_at = str(data.get("generated_at") or graph.generated_at)
        graph.schema = data.get("schema") or {"tables": {}}
        for raw in data.get("nodes") or []:
            node_id = raw["id"]
            node_type = raw["type"]
            attrs = {k: v for k, v in raw.items() if k not in {"id", "type"}}
            graph.nodes[node_id] = Node(id=node_id, type=node_type, attrs=attrs)
        for raw in data.get("edges") or []:
            etype = raw["type"]
            from_id = raw["from"]
            to_id = raw["to"]
            eid = raw.get("id") or edge_id(etype, from_id, to_id)
            graph.edges[eid] = Edge(
                id=eid,
                type=etype,
                from_id=from_id,
                to_id=to_id,
                confidence=parse_confidence(raw.get("confidence")),
                evidence=Evidence.from_dict(raw.get("evidence")),
                attrs={
                    k: v
                    for k, v in raw.items()
                    if k
                    not in {"id", "type", "from", "to", "confidence", "evidence"}
                },
            )
        return graph


def table_id(name: str) -> str:
    return f"table:{name.lower()}"


def column_id(table: str, column: str) -> str:
    return f"column:{table.lower()}.{column.lower()}"


def function_id(qualname: str) -> str:
    return f"function:{qualname}"


def entrypoint_id(kind: str, key: str) -> str:
    return f"entrypoint:{kind}:{key}"


def trigger_id(name: str) -> str:
    return f"db_trigger:{name.lower()}"
