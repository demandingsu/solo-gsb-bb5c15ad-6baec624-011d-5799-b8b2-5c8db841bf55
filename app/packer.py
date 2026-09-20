"""Guillotine-style (edge-to-edge) cutting layout engine.

Model
-----
* Every cut is a straight line spanning the full width or height of the
  sub-rectangle being cut (guillotine / 铡刀 rule).
* Every cut consumes ``kerf`` units of material (the blade/切缝 width).
  The kerf strip sits between the two children of a cut node.
* Pieces may be rotated 90 degrees.
* If the leftover after a cut would be narrower than the kerf it cannot
  yield any usable piece (the blade would eat it entirely), so it is
  treated as trim loss and not represented by a node.

The layout is built with the classic free-rectangle guillotine heuristic:
pieces are sorted largest-first and each piece is placed into the free
rectangle with the best short-side fit, splitting that rectangle with
guillotine cuts.  Because free rectangles are only ever produced by
splitting, the result is always expressible as a binary cut tree.

Locked placements
------------------
Requests may pin some pieces to fixed coordinates.  The locked layout on
the first sheet must itself be guillotine-separable under the same kerf
rules: recursively, a region is either empty, a single piece, or a full
span kerf-wide empty strip separates it into two non-empty groups.
LockedLayoutError is raised when no such decomposition exists; the error
names the pieces of the smallest region that could not be separated.
"""
from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-9

# Bound on the number of decomposition attempts while verifying a locked
# layout.  Decomposition can branch exponentially for pathological
# (non-separable) layouts, so give up deterministically past this many
# nodes instead of stalling the service.
LOCKED_SEARCH_BUDGET = 200_000


class UnfitItemError(ValueError):
    """Raised when an item cannot fit on a fresh stock sheet in any orientation."""

    def __init__(self, item_id: str, width: float, height: float,
                 stock_width: float, stock_height: float) -> None:
        self.item_id = item_id
        super().__init__(
            f"item '{item_id}' ({width} x {height}) does not fit on stock "
            f"{stock_width} x {stock_height} in either orientation"
        )


class CutLimitError(ValueError):
    """Raised when an item cannot be placed within the per-sheet cut limit."""

    def __init__(self, item_id: str, needed: int, limit: int) -> None:
        self.item_id = item_id
        super().__init__(
            f"item '{item_id}' cannot be placed within the max_cuts_per_sheet "
            f"limit of {limit}: even a blank stock sheet requires {needed} "
            "guillotine cuts (remaining offcuts narrower than the kerf aside)"
        )


class LockedLayoutError(ValueError):
    """Raised when pinned placements do not form a kerf-valid guillotine layout."""

    def __init__(self, message: str,
                 stuck_group: list[LockedPiece] | None = None) -> None:
        super().__init__(message)
        self.stuck_group = stuck_group


@dataclass
class Piece:
    """One physical piece to place (an item expanded by its quantity)."""

    item_id: str
    width: float
    height: float


@dataclass
class LockedPiece:
    """A piece pinned to fixed coordinates on the first sheet."""

    piece: Piece
    x: float
    y: float
    width: float
    height: float
    rotated: bool
    ref: int  # 1-based index in the request's locked_placements list


@dataclass
class Node:
    """A node of the binary cut tree.

    kind: "free" (usable offcut, during packing), "waste" (final offcut),
    "item" (a placed piece) or "cut" (internal node with two children).
    """

    kind: str
    x: float
    y: float
    width: float
    height: float
    direction: str | None = None  # cut nodes: "vertical" | "horizontal"
    children: list["Node"] | None = None
    piece: Piece | None = None    # item nodes
    rotated: bool = False         # item nodes
    locked_ref: int | None = None  # item nodes: request index when pinned


@dataclass
class Sheet:
    width: float
    height: float
    kerf: float
    root: Node
    free: list[Node] = field(default_factory=list)
    placements: list[dict] = field(default_factory=list)
    used_area: float = 0.0
    cut_count: int = 0


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------

def _fits(pw: float, ph: float, width: float, height: float) -> bool:
    return pw <= width + EPS and ph <= height + EPS


def _contains(a: Node, b: Node) -> bool:
    """True if rectangle a fully contains rectangle b."""
    return (
        a.x <= b.x + EPS
        and a.y <= b.y + EPS
        and a.x + a.width >= b.x + b.width - EPS
        and a.y + a.height >= b.y + b.height - EPS
    )


def _orientations(piece: Piece):
    """Yield (width, height) pairs, unrotated first."""
    yield piece.width, piece.height
    if piece.width != piece.height:
        yield piece.height, piece.width


def _corner_fill_ok(pw: float, ph: float, width: float, height: float,
                    kerf: float) -> bool:
    """Whether a piece at the leaf's origin yields a kerf-valid tree.

    Two regimes work: the piece reaches the leaf's far edge (slack ~ 0,
    item-only leaf), or at least a kerf plus a non-negative offcut remains
    (cut with a sibling leaf).  A positive sliver narrower than the kerf is
    impossible: the tree derives the cut position from the first child's
    reported width, so trim cannot sit between the piece edge and the kerf.
    """
    slack_r = width - pw
    slack_b = height - ph
    ok_r = slack_r <= EPS or slack_r >= kerf - EPS
    ok_b = slack_b <= EPS or slack_b >= kerf - EPS
    return ok_r and ok_b


def _added_cuts(pw: float, ph: float, width: float, height: float,
                kerf: float) -> int:
    """Cuts _fill introduces when corner-filling a piece in this rectangle.

    A slack that vanishes (piece reaches the far edge) needs no cut; each
    axis with a usable offcut beyond the kerf needs one, except the
    both-axes case where the corner piece needs two nested cuts.  Trim
    slivers below the kerf consume no cut node (edge trim).
    """
    has_right = width - pw - kerf > EPS
    has_bottom = height - ph - kerf > EPS
    return int(has_right) + int(has_bottom)


# ---------------------------------------------------------------------------
# tree building
# ---------------------------------------------------------------------------

def _become_cut(node: Node, direction: str, children: list[Node]) -> None:
    node.kind = "cut"
    node.direction = direction
    node.children = children
    node.piece = None
    node.rotated = False
    node.locked_ref = None


def _become_item(node: Node, item: Node) -> None:
    node.kind = "item"
    node.width = item.width
    node.height = item.height
    node.piece = item.piece
    node.rotated = item.rotated
    node.locked_ref = item.locked_ref
    node.direction = None
    node.children = None


def _item_node(x: float, y: float, pw: float, ph: float,
               piece: Piece, rotated: bool,
               locked_ref: int | None = None) -> Node:
    return Node(kind="item", x=x, y=y, width=pw, height=ph, piece=piece,
                rotated=rotated, locked_ref=locked_ref)


def _fill(node: Node, piece: Piece, pw: float, ph: float, kerf: float,
          rotated: bool | None = None, locked_ref: int | None = None
          ) -> tuple[list[Node], int]:
    """Place a piece (pw x ph) at the top-left corner of free leaf ``node``.

    Mutates ``node`` into an item leaf or a cut subtree and returns
    (new free rectangles created by the split, number of cut nodes added).
    Every split consumes ``kerf`` material; remainders not wider than the
    kerf are discarded as trim loss.  ``rotated`` defaults to being derived
    from the piece's base dimensions.
    """
    x, y, w, h = node.x, node.y, node.width, node.height
    if rotated is None:
        rotated = not (pw == piece.width and ph == piece.height)
    item = _item_node(x, y, pw, ph, piece, rotated, locked_ref)

    right_w = w - pw - kerf    # usable width right of a vertical cut
    bottom_h = h - ph - kerf   # usable height below a horizontal cut
    has_right = right_w > EPS
    has_bottom = bottom_h > EPS

    if not has_right and not has_bottom:
        # The piece consumes the whole rectangle (slivers <= kerf are trim).
        _become_item(node, item)
        return [], 0

    if has_right and has_bottom:
        # Both offcuts are usable.  Two guillotine split orders are possible;
        # keep the one whose full-span offcut is larger.
        if right_w * h >= w * bottom_h:
            # Vertical cut first: right strip spans the full height, then a
            # horizontal cut separates the piece from the offcut below it.
            right = Node("free", x + pw + kerf, y, right_w, h)
            bottom = Node("free", x, y + ph + kerf, pw, bottom_h)
            strip = Node("cut", x, y, pw, h, direction="horizontal",
                         children=[item, bottom])
            _become_cut(node, "vertical", [strip, right])
            return [right, bottom], 2
        # Horizontal cut first, mirrored.
        right = Node("free", x + pw + kerf, y, right_w, ph)
        bottom = Node("free", x, y + ph + kerf, w, bottom_h)
        strip = Node("cut", x, y, w, ph, direction="vertical",
                     children=[item, right])
        _become_cut(node, "horizontal", [strip, bottom])
        return [bottom, right], 2

    if has_right:
        right = Node("free", x + pw + kerf, y, right_w, h)
        _become_cut(node, "vertical", [item, right])
        return [right], 1

    bottom = Node("free", x, y + ph + kerf, w, bottom_h)
    _become_cut(node, "horizontal", [item, bottom])
    return [bottom], 1


def _prune(free: list[Node], limit: int = 200) -> None:
    """Drop free rectangles fully contained in another (keeps packing fast).

    This never affects the guillotine property: it only removes redundant
    placement candidates.  Skipped for very large lists to bound the O(n^2)
    cost.
    """
    if len(free) > limit:
        return
    kept = []
    for i, rect in enumerate(free):
        dropped = False
        for j, other in enumerate(free):
            if i == j:
                continue
            # Drop rect if strictly inside another, or a duplicate of an
            # earlier one.
            if _contains(other, rect) and (not _contains(rect, other) or j < i):
                dropped = True
                break
        if not dropped:
            kept.append(rect)
    free[:] = kept


def _best_fit(sheets: list[Sheet], piece: Piece, max_cuts: int | None = None):
    """Find the best placement for a piece across all open sheets.

    Returns (sheet_index, free_rect_index, pw, ph, added_cuts) or None.
    Scoring is best-short-side-fit, then best-area-fit; candidates with the
    same fit are ranked by the number of new guillotine cuts they would add.
    Rectangles whose resulting tree would exceed ``max_cuts`` are rejected.
    """
    best = None
    for si, sheet in enumerate(sheets):
        for fi, rect in enumerate(sheet.free):
            for pw, ph in _orientations(piece):
                if not _fits(pw, ph, rect.width, rect.height):
                    continue
                if not _corner_fill_ok(pw, ph, rect.width, rect.height,
                                       sheet.kerf):
                    # Fitting here would leave an unrepresentable trim
                    # sliver between the piece and the kerf; skip it.
                    continue
                added = _added_cuts(pw, ph, rect.width, rect.height,
                                    sheet.kerf)
                if max_cuts is not None and sheet.cut_count + added > max_cuts:
                    # This offcut would push the sheet past its cut budget.
                    continue
                short_side = min(rect.width - pw, rect.height - ph)
                area_left = rect.width * rect.height - pw * ph
                score = (short_side, area_left, added)
                if best is None or score < best[0]:
                    best = (score, si, fi, pw, ph, added)
    if best is None:
        return None
    _, si, fi, pw, ph, added = best
    return si, fi, pw, ph, added


def _place(sheet: Sheet, free_idx: int, piece: Piece, pw: float, ph: float,
           added: int) -> None:
    rect = sheet.free.pop(free_idx)
    new_rects, cuts = _fill(rect, piece, pw, ph, sheet.kerf)
    assert cuts == added
    sheet.free.extend(new_rects)
    _prune(sheet.free)
    sheet.cut_count += cuts
    sheet.used_area += pw * ph
    sheet.placements.append({
        "item": piece.item_id,
        "x": _num(rect.x),
        "y": _num(rect.y),
        "width": _num(pw),
        "height": _num(ph),
        "rotated": not (pw == piece.width and ph == piece.height),
        "locked": False,
    })


# ---------------------------------------------------------------------------
# locked layout: guillotine decomposition of pinned pieces
# ---------------------------------------------------------------------------

def _locked_labels(group: list[LockedPiece]) -> list[str]:
    """Distinct request references of a stuck group, input order preserved."""
    labels = []
    for lp in group:
        label = f"#{lp.ref} item '{lp.piece.item_id}'"
        if label not in labels:
            labels.append(label)
    return labels


def _isolate_locked(node: Node, lp: LockedPiece, kerf: float
                    ) -> tuple[list[Node], int]:
    """Fill a free region containing exactly one pinned piece.

    Peels free strips off the top/left edges (each cut consumes ``kerf``)
    until the piece is flush with the region's origin, then splits the
    right/bottom offcuts with the usual corner fill.  Returns
    (new free rectangles, number of cut nodes added).

    A gap of exactly 0 needs no cut; a gap larger than ``kerf`` leaves a
    free strip (gap minus kerf).  A gap in between is impossible: the blade
    is wider than the clearance but a sliver that small cannot stay as an
    origin-side leaf, so the pinned layout is not guillotine-separable.
    """
    free_rects: list[Node] = []
    cuts_added = 0
    top_gap = lp.y - node.y
    left_gap = lp.x - node.x
    if not (left_gap <= EPS or left_gap > kerf + EPS) \
            or not (top_gap <= EPS or top_gap > kerf + EPS):
        side = "left" if 0 < left_gap <= kerf + EPS else "top"
        raise LockedLayoutError(
            f"locked placement #{lp.ref} item '{lp.piece.item_id}' leaves "
            f"only {_num(left_gap if side == 'left' else top_gap)} units of "
            f"clearance on its {side} side, less than the kerf ({kerf}): no "
            "guillotine cut can separate it without cutting into the piece",
            stuck_group=[lp],
        )

    # Peel order mirrors _fill's heuristic: larger usable strip first.
    # The cut at (origin + gap - kerf) separates a free strip of width
    # (gap - kerf) from the kerf [origin+gap-kerf, origin+gap); the inner
    # region begins at the piece's edge (origin + gap) and keeps the
    # region's far edge, so slack past the piece is handled by _fill.
    if left_gap > EPS and top_gap > EPS \
            and (left_gap - kerf) * node.height < (top_gap - kerf) * node.width:
        # Horizontal (top) peel first.
        strip_w = top_gap - kerf
        inner = Node("free", node.x, node.y + top_gap,
                     node.width, node.height - top_gap)
        strip = Node("free", node.x, node.y, node.width, strip_w)
        _become_cut(node, "horizontal", [strip, inner])
        free_rects.append(strip)
        cuts_added += 1
        node = inner
        top_gap = 0.0

    while left_gap > EPS:
        strip_w = left_gap - kerf
        inner = Node("free", node.x + left_gap, node.y,
                     node.width - left_gap, node.height)
        strip = Node("free", node.x, node.y, strip_w, node.height)
        _become_cut(node, "vertical", [strip, inner])
        free_rects.append(strip)
        cuts_added += 1
        node = inner
        left_gap = 0.0

    while top_gap > EPS:
        strip_w = top_gap - kerf
        inner = Node("free", node.x, node.y + top_gap,
                     node.width, node.height - top_gap)
        strip = Node("free", node.x, node.y, node.width, strip_w)
        _become_cut(node, "horizontal", [strip, inner])
        free_rects.append(strip)
        cuts_added += 1
        node = inner
        top_gap = 0.0

    more_rects, more_cuts = _fill(
        node, lp.piece, lp.width, lp.height, kerf,
        rotated=lp.rotated, locked_ref=lp.ref,
    )
    free_rects.extend(more_rects)
    return free_rects, cuts_added + more_cuts


def _valid_region(node: Node, group: list[LockedPiece]) -> bool:
    """Every pinned piece of the group must lie inside the region."""
    rx2, ry2 = node.x + node.width, node.y + node.height
    for lp in group:
        if (lp.x < node.x - EPS or lp.y < node.y - EPS
                or lp.x + lp.width > rx2 + EPS
                or lp.y + lp.height > ry2 + EPS):
            return False
    return True


def _decompose_locked(node: Node, group: list[LockedPiece],
                      kerf: float, budget: list[int]
                      ) -> tuple[list[Node], int]:
    """Recursively turn free ``node`` into a cut tree over ``group``.

    Returns (usable free leaves created by the decomposition, number of cut
    nodes added).  Raises LockedLayoutError if the pinned layout cannot be
    produced by full-span guillotine cuts separated by ``kerf``-wide strips.
    """
    budget[0] -= 1
    if budget[0] <= 0:
        raise LockedLayoutError(
            "locked placements are too complex to verify: could not derive a "
            "guillotine cut tree within the search budget"
        )
    if not group:
        return [node], 0
    if len(group) == 1:
        return _isolate_locked(node, group[0], kerf)

    x, y, w, h = node.x, node.y, node.width, node.height

    # Candidate cut positions: a full-span, kerf-wide empty strip must
    # separate the two groups.  Around any piece edge ``e`` the strip's near
    # edge is tried at e - kerf (strip ending at the edge), e (strip starting
    # at the edge) and e + kerf.  These three positions cover every feasible
    # way of allocating the slack on either side of the groups; e + kerf in
    # particular keeps the offcut past a tight group inside the origin-side
    # child as a usable free leaf instead of leaving it as trim.
    candidates = []

    def _add_candidate(direction: str, cut_at: float, groups) -> None:
        """groups is (first, second) already split at the kerf strip."""
        first_group, second_group = groups
        if not first_group or not second_group:
            return
        if direction == "vertical":
            if cut_at < x + EPS or cut_at + kerf > x + w - EPS:
                return
            a = cut_at - x
            if a <= EPS or w - a - kerf <= EPS:
                return
            key = ("v", round(cut_at, 9))
            if any(c[0] == key for c in candidates):
                return
            candidates.append((key, "vertical", cut_at, a,
                               w - a - kerf, first_group, second_group))
        else:
            if cut_at < y + EPS or cut_at + kerf > y + h - EPS:
                return
            a = cut_at - y
            if a <= EPS or h - a - kerf <= EPS:
                return
            key = ("h", round(cut_at, 9))
            if any(c[0] == key for c in candidates):
                return
            candidates.append((key, "horizontal", cut_at, a,
                               h - a - kerf, first_group, second_group))

    def _split_vertical(cut_at: float):
        left, right = [], []
        for lp in group:
            if lp.x + lp.width <= cut_at + EPS:
                left.append(lp)
            elif lp.x >= cut_at + kerf - EPS:
                right.append(lp)
            else:
                return None
        return left, right

    def _split_horizontal(cut_at: float):
        top, bottom = [], []
        for lp in group:
            if lp.y + lp.height <= cut_at + EPS:
                top.append(lp)
            elif lp.y >= cut_at + kerf - EPS:
                bottom.append(lp)
            else:
                return None
        return top, bottom

    x_edges = sorted({round(lp.x, 9) for lp in group}
                     | {round(lp.x + lp.width, 9) for lp in group})
    for edge in x_edges:
        for cut_at in (edge, edge - kerf, edge + kerf):
            split = _split_vertical(cut_at)
            if split is not None:
                _add_candidate("vertical", cut_at, split)

    y_edges = sorted({round(lp.y, 9) for lp in group}
                     | {round(lp.y + lp.height, 9) for lp in group})
    for edge in y_edges:
        for cut_at in (edge, edge - kerf, edge + kerf):
            split = _split_horizontal(cut_at)
            if split is not None:
                _add_candidate("horizontal", cut_at, split)

    # Far-edge cuts first: making the origin-side child large keeps the
    # slack between a tight group and the stock edge inside that child as a
    # usable free leaf, instead of discarding it as trim behind the tight
    # child.  Ties: fewer pieces in the first child, then axis, then edge.
    candidates.sort(key=lambda c: (-(c[3] * (h if c[1] == "vertical" else w)),
                                   len(c[5]), c[1], c[3]))

    smallest_stuck: list[LockedPiece] | None = None

    def _consider(exc: LockedLayoutError) -> None:
        nonlocal smallest_stuck
        stuck = exc.stuck_group if exc.stuck_group is not None else group
        if smallest_stuck is None or len(stuck) < len(smallest_stuck):
            smallest_stuck = stuck

    for _, direction, _edge, a, b, first_group, second_group in candidates:
        if direction == "vertical":
            first = Node("free", x, y, a, h)
            second = Node("free", x + a + kerf, y, b, h)
        else:
            first = Node("free", x, y, w, a)
            second = Node("free", x, y + a + kerf, w, b)
        if not _valid_region(first, first_group) \
                or not _valid_region(second, second_group):
            continue
        try:
            free_first, cuts_first = _decompose_locked(
                first, first_group, kerf, budget)
            free_second, cuts_second = _decompose_locked(
                second, second_group, kerf, budget)
        except LockedLayoutError as exc:
            _consider(exc)
            continue
        _become_cut(node, direction, [first, second])
        return free_first + free_second, 1 + cuts_first + cuts_second

    stuck_group = smallest_stuck if smallest_stuck is not None else group
    labels = ", ".join(_locked_labels(stuck_group))
    raise LockedLayoutError(
        "locked placements cannot be produced by guillotine cuts with "
        f"kerf {kerf}: no full-span cut (leaving a {kerf}-wide kerf strip) "
        f"separates the conflicting placements ({labels}); adjust their "
        "coordinates or the kerf",
        stuck_group=stuck_group,
    )


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def _num(value: float) -> float:
    """Round away floating point dust from coordinate arithmetic."""
    return round(value, 6)


def _finalize(node: Node) -> None:
    """Turn remaining free leaves into waste leaves."""
    if node.kind == "free":
        node.kind = "waste"
        return
    if node.kind == "cut":
        for child in node.children:
            _finalize(child)


def _count_cuts(node: Node) -> int:
    """Number of cut nodes in a finished tree."""
    if node.kind != "cut":
        return 0
    return 1 + sum(_count_cuts(child) for child in node.children)


def _node_dict(node: Node) -> dict:
    base = {
        "x": _num(node.x),
        "y": _num(node.y),
        "width": _num(node.width),
        "height": _num(node.height),
    }
    if node.kind == "cut":
        return {
            "type": "cut",
            "direction": node.direction,
            **base,
            "children": [_node_dict(child) for child in node.children],
        }
    if node.kind == "item":
        return {
            "type": "item",
            "item": node.piece.item_id,
            "rotated": node.rotated,
            **base,
        }
    return {"type": "waste", **base}


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def build_plan(stock_width: float, stock_height: float, kerf: float,
               items: list[tuple[str, float, float, int]],
               locked: list[tuple[str, float, float, bool, int]] | None = None,
               max_cuts: int | None = None
               ) -> dict:
    """Pack ``items`` (id, width, height, quantity) onto stock sheets.

    ``locked`` optionally pins pieces on the first sheet; each tuple is
    (item_id, x, y, rotated, request_index).  Locked pieces count towards
    their item's quantity.  ``max_cuts`` optionally caps the number of cut
    nodes allowed on any single sheet (the locked layout on the first sheet
    included).  Returns a dict matching the PlanResponse schema.
    Raises UnfitItemError if any item cannot fit on a fresh sheet,
    CutLimitError if it cannot fit on a blank sheet within the cut budget,
    or LockedLayoutError if the pinned layout is invalid.
    """
    catalog: dict[str, Piece] = {}
    for item_id, width, height, quantity in items:
        if not (_fits(width, height, stock_width, stock_height)
                or _fits(height, width, stock_width, stock_height)):
            raise UnfitItemError(item_id, width, height, stock_width, stock_height)
        catalog[item_id] = Piece(item_id, width, height)

    locked_pieces: list[LockedPiece] = []
    for item_id, x, y, rotated, ref in locked or []:
        x, y = float(x), float(y)
        piece = catalog[item_id]
        pw, ph = (piece.height, piece.width) if rotated else (
            piece.width, piece.height)
        if (x < -EPS or y < -EPS
                or x + pw > stock_width + EPS
                or y + ph > stock_height + EPS):
            raise LockedLayoutError(
                f"locked placement #{ref} item '{item_id}' at ({_num(x)}, "
                f"{_num(y)}) ({_num(pw)} x {_num(ph)}) is outside the stock "
                f"{stock_width} x {stock_height}"
            )
        locked_pieces.append(LockedPiece(
            piece=piece, x=x, y=y, width=pw, height=ph,
            rotated=bool(rotated), ref=ref,
        ))

    # Pairwise overlaps are rejected explicitly with both references named.
    for i in range(len(locked_pieces)):
        a = locked_pieces[i]
        for j in range(i + 1, len(locked_pieces)):
            b = locked_pieces[j]
            overlap_x = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
            overlap_y = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
            if overlap_x > EPS and overlap_y > EPS:
                raise LockedLayoutError(
                    f"locked placements #{a.ref} item '{a.piece.item_id}' and "
                    f"#{b.ref} item '{b.piece.item_id}' overlap"
                )

    # Remaining quantities after the locked ones are consumed.
    remaining: list[Piece] = []
    for item_id, width, height, quantity in items:
        locked_count = sum(1 for lp in locked_pieces
                           if lp.piece.item_id == item_id)
        remaining.extend(Piece(item_id, width, height)
                         for _ in range(quantity - locked_count))

    sheets: list[Sheet] = []
    total_pieces = len(locked_pieces) + len(remaining)

    if locked_pieces:
        root = Node(kind="free", x=0.0, y=0.0,
                    width=stock_width, height=stock_height)
        sheet = Sheet(width=stock_width, height=stock_height, kerf=kerf,
                      root=root)
        sheets.append(sheet)
        free_rects, locked_cuts = _decompose_locked(
            root, locked_pieces, kerf, [LOCKED_SEARCH_BUDGET])
        if max_cuts is not None and locked_cuts > max_cuts:
            raise LockedLayoutError(
                f"locked layout on the first sheet requires {locked_cuts} "
                f"guillotine cuts but max_cuts_per_sheet is {max_cuts}: the "
                "locked placements cannot be retained within the cut budget"
            )
        sheet.cut_count = locked_cuts
        sheet.free.extend(free_rects)
        _prune(sheet.free)
        for lp in sorted(locked_pieces, key=lambda p: (p.y, p.x)):
            sheet.used_area += lp.width * lp.height
            sheet.placements.append({
                "item": lp.piece.item_id,
                "x": _num(lp.x),
                "y": _num(lp.y),
                "width": _num(lp.width),
                "height": _num(lp.height),
                "rotated": lp.rotated,
                "locked": True,
            })

    # Largest first: longer max side, then larger area.
    remaining.sort(key=lambda p: (max(p.width, p.height), p.width * p.height),
                   reverse=True)

    for piece in remaining:
        found = _best_fit(sheets, piece, max_cuts)
        if found is not None:
            si, fi, pw, ph, added = found
            _place(sheets[si], fi, piece, pw, ph, added)
            continue

        # No usable offcut within the budget: open a new sheet.  Geometric
        # fit was checked up front; among the fitting orientations prefer
        # the one introducing the fewest cuts, then unrotated (the
        # generator yields unrotated first and the sort is stable).
        root = Node(kind="free", x=0.0, y=0.0,
                    width=stock_width, height=stock_height)
        sheet = Sheet(width=stock_width, height=stock_height, kerf=kerf,
                      root=root, free=[root])
        options = [(pw, ph) for pw, ph in _orientations(piece)
                   if _fits(pw, ph, stock_width, stock_height)]
        options.sort(key=lambda op: _added_cuts(
            op[0], op[1], stock_width, stock_height, kerf))
        pw, ph = options[0]
        needed = _added_cuts(pw, ph, stock_width, stock_height, kerf)
        if max_cuts is not None and needed > max_cuts:
            raise CutLimitError(piece.item_id, needed, max_cuts)
        sheets.append(sheet)
        _place(sheet, 0, piece, pw, ph, needed)

    sheet_area = stock_width * stock_height
    result_sheets = []
    total_used = 0.0
    for index, sheet in enumerate(sheets):
        _finalize(sheet.root)
        cut_count = _count_cuts(sheet.root)
        total_used += sheet.used_area
        result_sheets.append({
            "index": index,
            "width": _num(stock_width),
            "height": _num(stock_height),
            "utilization": round(sheet.used_area / sheet_area, 6),
            "placements": sheet.placements,
            "cut_tree": _node_dict(sheet.root),
            "cut_count": cut_count,
        })

    return {
        "stock": {"width": _num(stock_width), "height": _num(stock_height)},
        "kerf": _num(kerf),
        "sheets": result_sheets,
        "summary": {
            "sheet_count": len(sheets),
            "piece_count": total_pieces,
            "used_area": _num(total_used),
            "stock_area": _num(len(sheets) * sheet_area),
            "utilization": round(total_used / (len(sheets) * sheet_area), 6),
        },
    }
