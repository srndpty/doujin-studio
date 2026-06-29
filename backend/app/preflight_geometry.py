from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import Panel

_GUTTER_EPS = 1e-6


__all__ = ["bbox_overlaps", "overlap_area", "panel_between", "unit_box_iou"]


def overlap_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    dx = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    dy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return dx * dy


def bbox_overlaps(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    x_overlap = min(ax0 + aw, bx0 + bw) - max(ax0, bx0)
    y_overlap = min(ay0 + ah, by0 + bh) - max(ay0, by0)
    return x_overlap > _GUTTER_EPS and y_overlap > _GUTTER_EPS


def panel_between(
    panels: list[Panel],
    i: int,
    j: int,
    gap_span: tuple[float, float],
    band: tuple[float, float],
    horizontal: bool,
) -> bool:
    """隙間(gap_span)と共有バンド(band)の間に別のコマが入っているか。"""
    lo, hi = gap_span
    band_lo, band_hi = band
    for k, panel in enumerate(panels):
        if k == i or k == j:
            continue
        cx0, cy0, cw, ch = panel.bbox
        cx1, cy1 = cx0 + cw, cy0 + ch
        if horizontal:
            gap_lo_c, gap_hi_c, cross_lo, cross_hi = cx0, cx1, cy0, cy1
        else:
            gap_lo_c, gap_hi_c, cross_lo, cross_hi = cy0, cy1, cx0, cx1
        shares_band = min(cross_hi, band_hi) - max(cross_lo, band_lo) > _GUTTER_EPS
        in_gap = gap_lo_c < hi - _GUTTER_EPS and gap_hi_c > lo + _GUTTER_EPS
        if shares_band and in_gap:
            return True
    return False


def unit_box_iou(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    ix0 = max(lx, rx)
    iy0 = max(ly, ry)
    ix1 = min(lx + lw, rx + rw)
    iy1 = min(ly + lh, ry + rh)
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if intersection <= 0:
        return 0.0
    union = lw * lh + rw * rh - intersection
    return intersection / union if union > 0 else 0.0


_overlap_area = overlap_area
_bbox_overlaps = bbox_overlaps
_panel_between = panel_between
_unit_box_iou = unit_box_iou
