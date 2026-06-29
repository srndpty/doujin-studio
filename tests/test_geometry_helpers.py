from __future__ import annotations

from backend.app.preflight_geometry import bbox_overlaps, overlap_area, unit_box_iou
from backend.app.schema_geometry import has_self_intersection, polygon_area, segments_intersect


def test_bbox_overlaps_treats_touching_edges_as_non_overlap() -> None:
    assert bbox_overlaps((0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5)) is False
    assert bbox_overlaps((0.0, 0.0, 0.5, 0.5), (0.5 - 1e-4, 0.0, 0.5, 0.5)) is True


def test_overlap_area_and_iou_handle_float_boundaries() -> None:
    assert overlap_area((0, 0, 10, 10), (10, 0, 20, 10)) == 0
    assert unit_box_iou((0.0, 0.0, 0.5, 0.5), (0.25, 0.25, 0.5, 0.5)) > 0


def test_polygon_area_and_self_intersection_detection() -> None:
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    bow_tie = [(0.0, 0.0), (1.0, 1.0), (1.0, 0.0), (0.0, 1.0)]
    assert polygon_area(square) == 1.0
    assert has_self_intersection(square) is False
    assert has_self_intersection(bow_tie) is True


def test_segments_intersect_includes_collinear_touch() -> None:
    assert segments_intersect((0.0, 0.0), (1.0, 0.0), (1.0, 0.0), (2.0, 0.0)) is True
