"""
bktree.py — Burkhard-Keller tree for metric-space nearest-neighbour queries.

Used by the near-duplicate pass to answer "all pHashes within Hamming distance
d of x" with ZERO false negatives, visiting only ~O(log N) nodes per query.

Hamming distance on 64-bit ints satisfies the triangle inequality, so it is a
valid metric for a BK-tree. This replaces an earlier sorted-sliding-window
heuristic that silently missed near-dupes whose high-order pHash bits differed
(e.g. 0x0…0 and 0x8…0 are Hamming-1 yet sort to opposite ends of the list).

A single-item query against an already-built tree is cheap, so the tree doubles
as an incremental index: build once, then query one new key at a time.
"""

from __future__ import annotations

from typing import Callable


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two integers."""
    return bin(a ^ b).count("1")


class _Node:
    __slots__ = ("key", "children")

    def __init__(self, key: int):
        self.key = key
        # edge distance -> child node
        self.children: dict[int, "_Node"] = {}


class BKTree:
    """Burkhard-Keller tree over an integer keyspace with a metric distance.

    Default distance is Hamming distance on 64-bit ints. Keys are assumed
    distinct; re-adding an existing key (distance 0) is a no-op.
    """

    def __init__(self, distance: Callable[[int, int], int] = hamming_distance):
        self._distance = distance
        self._root: _Node | None = None

    def add(self, key: int) -> None:
        """Insert a single key. O(tree height) distance computations."""
        if self._root is None:
            self._root = _Node(key)
            return
        node = self._root
        while True:
            d = self._distance(key, node.key)
            if d == 0:
                return  # already present; keys are distinct
            child = node.children.get(d)
            if child is None:
                node.children[d] = _Node(key)
                return
            node = child

    def add_many(self, keys) -> None:
        for key in keys:
            self.add(key)

    def query(self, key: int, max_distance: int) -> list[tuple[int, int]]:
        """Return all (stored_key, distance) within max_distance of `key`.

        Uses the triangle inequality to prune: a child reachable by an edge of
        length e from a node at distance d can only hold keys in
        [d - max_distance, d + max_distance], so other branches are skipped.
        """
        if self._root is None:
            return []
        results: list[tuple[int, int]] = []
        stack = [self._root]
        while stack:
            node = stack.pop()
            d = self._distance(key, node.key)
            if d <= max_distance:
                results.append((node.key, d))
            lo = d - max_distance
            hi = d + max_distance
            for edge, child in node.children.items():
                if lo <= edge <= hi:
                    stack.append(child)
        return results
