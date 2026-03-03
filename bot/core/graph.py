from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


@dataclass
class GraphEdge:
    src: str
    dst: str
    dexes: Set[str] = field(default_factory=set)
    sizes: List[int] = field(default_factory=list)
    liquidity_usd: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenGraph:
    adjacency: Dict[str, Dict[str, GraphEdge]] = field(default_factory=dict)

    def add_edge(
        self,
        src: str,
        dst: str,
        *,
        dexes: Iterable[str],
        sizes: Iterable[int] = (),
        liquidity_usd: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        s = str(src or "").strip()
        d = str(dst or "").strip()
        if not s or not d:
            return
        node = self.adjacency.setdefault(s, {})
        edge = node.get(d)
        if edge is None:
            edge = GraphEdge(src=s, dst=d)
            node[d] = edge
        edge.dexes |= {str(x).strip().lower() for x in dexes if str(x).strip()}
        edge.sizes = sorted({int(x) for x in list(edge.sizes) + [int(y) for y in sizes if int(y) > 0]})
        edge.liquidity_usd = max(float(edge.liquidity_usd), float(liquidity_usd or 0.0))
        if metadata:
            edge.metadata.update(dict(metadata))

    def neighbors(self, token: str) -> Dict[str, GraphEdge]:
        return dict(self.adjacency.get(str(token or "").strip(), {}))

    def edge(self, src: str, dst: str) -> Optional[GraphEdge]:
        return self.adjacency.get(str(src or "").strip(), {}).get(str(dst or "").strip())

    def tokens(self) -> List[str]:
        out = set(self.adjacency.keys())
        for v in self.adjacency.values():
            out |= set(v.keys())
        return sorted(out)


def build_token_graph(
    universe: Iterable[Dict[str, Any]],
    *,
    enabled_dexes: Iterable[str],
    bidirectional: bool = True,
) -> TokenGraph:
    graph = TokenGraph()
    enabled = {str(x).strip().lower() for x in enabled_dexes if str(x).strip()}
    for row in universe:
        if not isinstance(row, dict):
            continue
        a = str(row.get("token_in") or "").strip()
        b = str(row.get("token_out") or "").strip()
        if not a or not b:
            continue
        sizes = [int(x) for x in row.get("sizes", []) if int(x) > 0]
        liq = float(row.get("liquidity_usd") or 0.0)
        graph.add_edge(a, b, dexes=enabled, sizes=sizes, liquidity_usd=liq, metadata=row)
        if bidirectional:
            graph.add_edge(b, a, dexes=enabled, sizes=sizes, liquidity_usd=liq, metadata=row)
    return graph


def _normalize_cycle(a: str, b: str, c: str) -> Tuple[str, str, str]:
    rots = [(a, b, c), (b, c, a), (c, a, b)]
    return min(rots)


def find_tri_cycles(
    graph: TokenGraph,
    *,
    max_cycles: int = 128,
    max_start_tokens: int = 128,
) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    seen: Set[Tuple[str, str, str]] = set()
    starts = graph.tokens()[: max(1, int(max_start_tokens))]
    for a in starts:
        nbr_a = graph.neighbors(a)
        for b in sorted(nbr_a.keys()):
            if b == a:
                continue
            nbr_b = graph.neighbors(b)
            for c in sorted(nbr_b.keys()):
                if c in {a, b}:
                    continue
                if graph.edge(c, a) is None:
                    continue
                cyc = _normalize_cycle(a, b, c)
                if cyc in seen:
                    continue
                seen.add(cyc)
                out.append(cyc)
                if len(out) >= int(max_cycles):
                    return out
    return out

