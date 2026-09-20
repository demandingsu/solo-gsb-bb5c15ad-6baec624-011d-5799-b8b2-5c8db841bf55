import pytest

from app.packer import (
    CutLimitError,
    LockedLayoutError,
    Node,
    Piece,
    Sheet,
    UnfitItemError,
    _best_fit,
    build_plan,
)
from tests.check_plan import check_plan, count_cut_nodes, iter_leaves


def plan(stock_w, stock_h, kerf, items):
    return build_plan(stock_w, stock_h, kerf, items)


class TestBasicPacking:
    def test_exact_fit_single_piece(self):
        result = plan(100, 100, 3, [("A", 100, 100, 1)])
        assert result["summary"]["sheet_count"] == 1
        sheet = result["sheets"][0]
        assert sheet["utilization"] == 1.0
        assert sheet["placements"] == [
            {"item": "A", "x": 0, "y": 0, "width": 100, "height": 100,
             "rotated": False, "locked": False}
        ]
        assert sheet["cut_tree"]["type"] == "item"

    def test_kerf_is_consumed_between_pieces(self):
        # Two 45-wide pieces on a 100-wide sheet with kerf 5:
        # the second must start at 45 + 5 = 50, not 45.
        result = plan(100, 100, 5, [("A", 45, 100, 2)])
        sheet = result["sheets"][0]
        assert result["summary"]["sheet_count"] == 1
        xs = sorted(p["x"] for p in sheet["placements"])
        assert xs == [0, 50]
        root = sheet["cut_tree"]
        assert root["type"] == "cut" and root["direction"] == "vertical"
        first, second = root["children"]
        assert second["x"] == first["x"] + first["width"] + 5

    def test_kerf_can_force_an_extra_sheet(self):
        # 3 pieces of 35 need 35*3 + 2*4 = 113 > 100 -> two sheets.
        result = plan(100, 100, 4, [("A", 35, 100, 3)])
        assert result["summary"]["sheet_count"] == 2
        check_plan(result)

    def test_zero_kerf_packs_tightly(self):
        result = plan(100, 100, 0, [("A", 50, 100, 2)])
        assert result["summary"]["sheet_count"] == 1
        assert result["sheets"][0]["utilization"] == 1.0

    def test_rotation_when_unrotated_does_not_fit(self):
        result = plan(50, 100, 0, [("A", 60, 40, 1)])
        placement = result["sheets"][0]["placements"][0]
        assert placement["rotated"] is True
        assert placement["width"] == 40
        assert placement["height"] == 60

    def test_unrotated_preferred_when_both_fit(self):
        result = plan(100, 100, 0, [("A", 60, 40, 1)])
        placement = result["sheets"][0]["placements"][0]
        assert placement["rotated"] is False
        assert placement["width"] == 60

    def test_multiple_sheets(self):
        # 8 pieces of 50x50 pack exactly 4 per 100x100 sheet.
        result = plan(100, 100, 0, [("A", 50, 50, 8)])
        assert result["summary"]["sheet_count"] == 2
        assert result["summary"]["utilization"] == 1.0
        check_plan(result)

    def test_multiple_sheets_and_items(self):
        result = plan(100, 100, 0, [("A", 60, 60, 3), ("B", 40, 40, 4)])
        assert result["summary"]["sheet_count"] >= 2
        assert result["summary"]["piece_count"] == 7
        check_plan(result)


class TestUnfitItems:
    def test_item_larger_than_stock_raises(self):
        with pytest.raises(UnfitItemError) as excinfo:
            plan(100, 100, 0, [("big", 101, 50, 1)])
        assert "big" in str(excinfo.value)

    def test_item_unfit_in_both_orientations_raises(self):
        with pytest.raises(UnfitItemError):
            plan(100, 60, 0, [("awkward", 120, 70, 1)])

    def test_fit_check_ignores_kerf_on_fresh_sheet(self):
        # A piece matching the stock exactly must fit even with a large kerf:
        # no cut is needed at the sheet edges.
        result = plan(100, 100, 10, [("A", 100, 100, 1)])
        assert result["summary"]["sheet_count"] == 1


class TestTreeConsistency:
    def test_mixed_batch_tree_is_consistent(self):
        result = plan(
            900, 600, 3,
            [("flyer", 210, 148, 8), ("card", 90, 55, 20), ("tag", 120, 80, 5)],
        )
        check_plan(result)

    def test_waste_leaves_have_positive_size(self):
        result = plan(100, 100, 5, [("A", 30, 30, 3)])
        check_plan(result)
        for sheet in result["sheets"]:
            for leaf in iter_leaves(sheet["cut_tree"]):
                if leaf["type"] == "waste":
                    assert leaf["width"] > 0
                    assert leaf["height"] > 0

    def test_trim_sliver_narrower_than_kerf_is_allowed(self):
        # 300-wide piece on 302-wide stock with kerf 3: the 2mm sliver
        # cannot host anything (the blade would eat it) -> trim loss,
        # the piece must still be placed.
        result = plan(302, 100, 3, [("A", 300, 100, 1)])
        assert result["summary"]["sheet_count"] == 1
        check_plan(result)

    def test_decimal_dimensions(self):
        result = plan(297.5, 210.0, 1.5, [("A", 99.1, 70.3, 6)])
        check_plan(result)

    def test_utilization_never_exceeds_one(self):
        result = plan(200, 100, 2, [("A", 45, 45, 30)])
        check_plan(result)
        for sheet in result["sheets"]:
            assert 0 < sheet["utilization"] <= 1.0


# ---------------------------------------------------------------------------
# locked placements
# ---------------------------------------------------------------------------

def _locked(items, pinned, stock_w=100, stock_h=100, kerf=0, max_cuts=None):
    refs = [(iid, x, y, rot, i + 1)
            for i, (iid, x, y, rot) in enumerate(pinned)]
    return build_plan(stock_w, stock_h, kerf, items, locked=refs,
                      max_cuts_per_sheet=max_cuts)


class TestLockedPlacements:
    def test_single_locked_piece_marked_and_counted(self):
        result = _locked([("A", 40, 40, 3)],
                         [("A", 0, 0, False)], kerf=0)
        check_plan(result)
        assert result["summary"]["piece_count"] == 3
        locked = [p for s in result["sheets"] for p in s["placements"]
                  if p["locked"]]
        assert len(locked) == 1
        assert locked[0]["item"] == "A"

    def test_locked_coordinates_are_preserved(self):
        result = _locked([("A", 40, 40, 1)],
                         [("A", 30, 20, False)], stock_w=100, stock_h=100)
        check_plan(result)
        p = result["sheets"][0]["placements"][0]
        assert (p["x"], p["y"], p["width"], p["height"]) == (30, 20, 40, 40)
        assert p["locked"] is True

    def test_rotated_locked_piece(self):
        result = _locked([("A", 60, 40, 1)], [("A", 40, 0, True)])
        check_plan(result)
        p = result["sheets"][0]["placements"][0]
        assert p["rotated"] is True
        assert (p["width"], p["height"]) == (40, 60)

    def test_two_locked_pieces_exact_kerf_gap(self):
        # 45 + kerf 5 + 45 == 95 <= 100: the cut sits exactly between them.
        result = _locked(
            [("A", 45, 40, 3)],
            [("A", 0, 0, False), ("A", 50, 0, False)], kerf=5)
        check_plan(result)
        assert result["summary"]["piece_count"] == 3
        root = result["sheets"][0]["cut_tree"]
        assert root["type"] == "cut" and root["direction"] == "vertical"
        xs = sorted(p["x"] for p in result["sheets"][0]["placements"]
                    if p["locked"])
        assert xs == [0, 50]

    def test_locked_grid_with_exact_kerf(self):
        pinned = [("A", 0, 0, False), ("A", 50, 0, False),
                  ("A", 0, 50, False), ("A", 50, 50, False)]
        result = _locked([("A", 40, 40, 4)], pinned, kerf=10)
        check_plan(result)
        assert result["summary"]["sheet_count"] == 1

    def test_remaining_pieces_fill_usable_offcuts(self):
        # Three locked corners leave the bottom-right 40x40 region free.
        pinned = [("A", 0, 0, False), ("A", 50, 0, False),
                  ("A", 0, 50, False)]
        result = _locked([("A", 40, 40, 4)], pinned, kerf=10)
        check_plan(result)
        assert result["summary"]["sheet_count"] == 1
        assert result["summary"]["piece_count"] == 4
        unlocked = [p for s in result["sheets"] for p in s["placements"]
                    if not p["locked"]]
        assert len(unlocked) == 1
        assert (unlocked[0]["x"], unlocked[0]["y"]) == (50, 50)

    def test_remaining_pieces_overflow_to_new_sheets(self):
        pinned = [("A", 0, 0, False), ("A", 50, 0, False)]
        result = _locked([("A", 45, 100, 4)], pinned,
                         stock_w=100, stock_h=100, kerf=5)
        check_plan(result)
        assert result["summary"]["sheet_count"] >= 2
        assert result["summary"]["piece_count"] == 4
        # Locked pieces stay on sheet 0.
        assert all(p["locked"] for p in result["sheets"][0]["placements"])

    def test_interior_piece_peels_free_strips(self):
        result = _locked([("A", 100, 80, 1)],
                         [("A", 150, 100, False)],
                         stock_w=300, stock_h=200, kerf=3)
        check_plan(result)
        leaves = list(iter_leaves(result["sheets"][0]["cut_tree"]))
        assert any(leaf["type"] == "item" for leaf in leaves)
        waste = [leaf for leaf in leaves if leaf["type"] == "waste"]
        assert waste  # peeled strips end as waste

    def test_locked_strip_is_reused_when_piece_fits_cleanly(self):
        # 20-unit left gap with kerf 3 yields a 17-wide free strip.
        result = _locked([("A", 40, 40, 1), ("B", 17, 100, 1)],
                         [("A", 20, 20, False)], kerf=3)
        check_plan(result)
        assert result["summary"]["sheet_count"] == 1

    def test_sub_kerf_sliver_blocks_reuse(self):
        # A 15-wide piece leaves 2 units in the 17-wide strip: narrower than
        # the kerf, so the piece must go on a new sheet instead.
        result = _locked([("A", 40, 40, 1), ("B", 15, 100, 1)],
                         [("A", 20, 20, False)], kerf=3)
        check_plan(result)
        assert result["summary"]["sheet_count"] == 2

    def test_touching_pieces_with_kerf_are_rejected(self):
        with pytest.raises(LockedLayoutError) as exc:
            _locked([("A", 45, 40, 2)],
                    [("A", 0, 0, False), ("A", 45, 0, False)], kerf=3)
        message = str(exc.value)
        assert "#1" in message and "#2" in message

    def test_gap_smaller_than_kerf_is_rejected(self):
        with pytest.raises(LockedLayoutError):
            _locked([("A", 45, 40, 2)],
                    [("A", 0, 0, False), ("A", 48, 0, False)], kerf=5)

    def test_origin_clearance_smaller_than_kerf_rejected(self):
        with pytest.raises(LockedLayoutError) as exc:
            _locked([("A", 40, 40, 1)], [("A", 2, 0, False)], kerf=5)
        assert "#1" in str(exc.value)

    def test_non_guillotine_windmill_rejected(self):
        # Classic non-slicing layout: four interlocking blades.
        coords = [(0, 0, 80, 120), (80, 0, 120, 80),
                  (160, 80, 40, 120), (0, 120, 160, 80)]
        items = [(f"p{i}", w, h, 1) for i, (x, y, w, h) in enumerate(coords, 1)]
        pinned = [(f"p{i}", x, y, False)
                  for i, (x, y, w, h) in enumerate(coords, 1)]
        with pytest.raises(LockedLayoutError) as exc:
            _locked(items, pinned, stock_w=200, stock_h=200, kerf=0)
        # Conflict report names the stuck placements.
        assert "p1" in str(exc.value)

    def test_overlapping_locked_pieces_rejected(self):
        with pytest.raises(LockedLayoutError) as exc:
            _locked([("A", 40, 40, 2)],
                    [("A", 0, 0, False), ("A", 30, 30, False)])
        assert "#1" in str(exc.value) and "#2" in str(exc.value)

    def test_out_of_bounds_locked_piece_rejected(self):
        with pytest.raises(LockedLayoutError):
            _locked([("A", 40, 40, 1)], [("A", 70, 70, False)])

    def test_rotated_piece_out_of_bounds_rejected(self):
        with pytest.raises(LockedLayoutError):
            _locked([("A", 60, 40, 1)], [("A", 50, 0, True)],
                    stock_w=100, stock_h=50)

    def test_summary_covers_locked_and_remaining(self):
        pinned = [("flyer", 0, 0, False), ("card", 0, 151, False)]
        result = _locked(
            [("flyer", 210, 148, 8), ("card", 90, 55, 20)], pinned,
            stock_w=900, stock_h=600, kerf=3)
        check_plan(result)
        assert result["summary"]["piece_count"] == 28
        used = sum(p["width"] * p["height"]
                   for s in result["sheets"] for p in s["placements"])
        assert abs(result["summary"]["used_area"] - round(used, 6)) <= 1e-4

    def test_large_locked_grid_decomposes(self):
        n = 12
        pinned = [("A", x * 11, y * 11, False)
                  for y in range(n) for x in range(n)]
        result = _locked([("A", 10, 10, n * n)], pinned,
                         stock_w=n * 11, stock_h=n * 11, kerf=1)
        check_plan(result)
        assert result["summary"]["piece_count"] == n * n


# ---------------------------------------------------------------------------
# per-sheet cut limit
# ---------------------------------------------------------------------------

class TestCutLimit:
    def test_cut_count_reported_without_limit(self):
        result = plan(100, 100, 5, [("A", 45, 100, 2)])
        assert result["sheets"][0]["cut_count"] == 1

    def test_exact_fit_reports_zero_cuts(self):
        result = plan(100, 100, 3, [("A", 100, 100, 1)])
        assert result["sheets"][0]["cut_count"] == 0

    def test_cut_count_matches_cut_tree(self):
        result = plan(900, 600, 3,
                      [("flyer", 210, 148, 8), ("card", 90, 55, 20)])
        check_plan(result)
        for sheet in result["sheets"]:
            assert sheet["cut_count"] == count_cut_nodes(sheet["cut_tree"])

    def test_limit_splits_across_sheets(self):
        # A 50x100 piece on a 100-wide sheet costs one cut; the second
        # fills the offcut exactly (no cut).  With a limit of 1 only two
        # pieces share a sheet.
        result = build_plan(100, 100, 0, [("A", 50, 100, 4)],
                            max_cuts_per_sheet=1)
        check_plan(result)
        assert result["summary"]["sheet_count"] == 2
        assert all(s["cut_count"] <= 1 for s in result["sheets"])

    def test_item_too_demanding_for_limit_rejected(self):
        # A 40x40 piece on a blank 100x100 sheet (kerf 3) needs two cuts.
        with pytest.raises(CutLimitError) as excinfo:
            build_plan(100, 100, 3, [("A", 40, 40, 1)], max_cuts_per_sheet=1)
        assert "A" in str(excinfo.value)

    def test_item_at_exact_limit_accepted(self):
        result = build_plan(100, 100, 3, [("A", 40, 40, 1)],
                            max_cuts_per_sheet=2)
        check_plan(result)
        assert result["sheets"][0]["cut_count"] == 2

    def test_locked_tree_over_limit_rejected(self):
        # The locked pair needs three cuts (one split + one per piece).
        pinned = [("A", 0, 0, False), ("A", 50, 0, False)]
        with pytest.raises(CutLimitError):
            _locked([("A", 45, 40, 2)], pinned, kerf=5, max_cuts=2)

    def test_locked_tree_at_limit_accepted(self):
        pinned = [("A", 0, 0, False), ("A", 50, 0, False)]
        result = _locked([("A", 45, 40, 2)], pinned, kerf=5, max_cuts=3)
        check_plan(result)
        assert result["sheets"][0]["cut_count"] == 3

    def test_locked_cuts_consume_sheet_budget(self):
        # The locked tree uses all 3 cuts, so the remaining piece cannot
        # split any offcut of the first sheet and overflows to a new one.
        pinned = [("A", 0, 0, False), ("A", 50, 0, False)]
        result = _locked([("A", 45, 40, 3)], pinned, kerf=5, max_cuts=3)
        check_plan(result)
        assert result["summary"]["sheet_count"] == 2
        assert result["sheets"][0]["cut_count"] == 3
        assert result["sheets"][1]["cut_count"] == 2


def _sheet_with_free(rects, kerf, cuts=0):
    sheet = Sheet(width=100.0, height=100.0, kerf=kerf,
                  root=Node(kind="free", x=0.0, y=0.0,
                            width=100.0, height=100.0))
    sheet.free = [Node(kind="free", x=0.0, y=float(10 * i),
                       width=w, height=h)
                  for i, (w, h) in enumerate(rects)]
    sheet.cuts = cuts
    return sheet


class TestBestFitCutLimit:
    def test_equal_fit_prefers_fewer_added_cuts(self):
        # Both rectangles score the same for a 20x10 piece (short side 0,
        # leftover area 60) but the split costs 1 cut vs 0.
        sheet = _sheet_with_free([(26, 10), (20, 13)], kerf=3)
        found = _best_fit([sheet], Piece("A", 20, 10), max_cuts=10)
        assert found is not None
        assert found[1] == 1

    def test_equal_fit_without_limit_keeps_legacy_order(self):
        sheet = _sheet_with_free([(26, 10), (20, 13)], kerf=3)
        found = _best_fit([sheet], Piece("A", 20, 10))
        assert found is not None
        assert found[1] == 0

    def test_rects_exceeding_the_limit_are_skipped(self):
        # The split adds one cut; the sheet is already at the limit.
        sheet = _sheet_with_free([(26, 10)], kerf=3, cuts=1)
        piece = Piece("A", 20, 10)
        assert _best_fit([sheet], piece, max_cuts=1) is None
        assert _best_fit([sheet], piece, max_cuts=2) is not None
