"""Compose several verified scientific panels into ONE editable Origin page.

Why this module exists
----------------------
``scientific_renderer`` draws exactly one chart per Origin graph window, so a
reference figure that is genuinely a *matrix* of panels had to be delivered as
N separate windows: the panel-to-panel reading and the shared axes were lost.

Origin can hold several drawing layers in a single graph page, and the engine
already proved the primitive for the UV-Vis Tauc inset
(``layadd type:=inset;`` + ``graph[1]``).  A live probe on the verified
2024b/10.15 baseline adds the two facts this module is built on:

* ``layadd type:=normal;`` creates an addressable **sibling panel layer**
  (``graph[i]``) and ``page.nlayers`` tracks the count;
* each layer keeps its own page-percent geometry, its own axis ranges and its
  own plots, and the engine's TrueType-embedded PDF export still succeeds from
  a multi-layer page.

Composition therefore reuses the *same* per-panel drawing path
(``_build_origin_graph`` with a :class:`PanelContext`) instead of introducing a
second renderer.  Every panel keeps the verified axis/text/plot contract of its
own frozen plan; only geometry, typography scale, legend scope and shared-axis
suppression differ.

Scope, stated plainly
---------------------
* Only templates drawn by ``scientific_renderer`` can be composed.  Evidence
  (``raw_summary``/``forest``/SHAP), categorical and 3D routes are refused with
  an explicit code rather than silently approximated.
* Panels are **combined, never recomputed**: each panel's frozen plan, source
  SHA256 and column mapping are re-verified and the source table stays
  read-only.
* Panel typography is scaled to the panel's physical cell with a documented
  floor, and the whole-page layout is read back from Origin before the run may
  report success.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from origin_sciplot.logging_utils import RunLogger
from origin_sciplot.output_manager import write_json
from origin_sciplot.scientific_visual import AdaptiveOriginStyle
from origin_sciplot.scientific_workflow import (
    ScientificColumnMapping,
    ScientificPreparation,
    apply_scientific_palette_override,
    apply_scientific_text_overrides,
    load_scientific_frame,
    prepare_scientific,
)

from .base_style_contract import pt_to_origin_width_units
from .export_utils import export_graph
from .safe_errors import OriginDrawError
from .scientific_renderer import PanelContext, _build_origin_graph, _figure_style
from .session import OriginSession

PANEL_COMPOSER_VERSION = "1.0"

#: Templates this composer can draw.  Everything else is refused explicitly:
#: the evidence / categorical / 3D renderers build their own page structure and
#: have not been through the multi-layer gate.
COMPOSABLE_TEMPLATE_IDS = frozenset(
    {
        "trend",
        "line_error",
        "scatter",
        "calibration_curve",
        "nyquist",
        "uv_vis",
        "pl_spectrum",
        "bar_error",
        "bland_altman",
    }
)

#: Readable floors.  Below these a print-resolution panel stops being legible,
#: so the composer refuses to shrink further instead of emitting 3 pt text.
MIN_AXIS_TITLE_PT = 5.0
MIN_TICK_LABEL_PT = 4.5
MIN_LEGEND_PT = 4.5
MIN_PLOT_LINE_PT = 0.75
MIN_FRAME_PT = 0.5
MIN_TICK_LENGTH_PT = 1.6
MIN_TYPOGRAPHY_SCALE = 0.22
#: Symbol floors.  A marker is a stroke in Origin's model, so it shrinks with
#: ``line_width_scale`` alongside the line it sits on.  These floors only stop a
#: symbol from vanishing; they are not a target size.  The point floor is low on
#: purpose: on a dense grid the honest answer is a small dot, and the error-bar
#: cap, not the symbol, is what usually makes a point look oversized.
MIN_MARKER_PT = 1.5
MIN_MARKER_SCALE = 0.25

#: Comfortable print targets.  Falling below them is allowed and reported, not
#: silently hidden: a dense grid genuinely forces small type, and the user
#: should see the number instead of discovering it in the proofs.
COMFORT_TICK_LABEL_PT = 5.5
COMFORT_AXIS_TITLE_PT = 6.0

DEFAULT_MARGINS_CM = {"left": 1.9, "right": 0.7, "top": 0.5, "bottom": 1.1}
DEFAULT_GAPS_CM = {"h": 0.75, "v": 0.6}
DEFAULT_LEGEND_BAND_CM = 0.85

#: Page-level row/column header text size.  Fixed rather than derived from the
#: grid style because the header band changes the cell size, which would
#: otherwise make an automatic header size circular.
DEFAULT_HEADER_PT = 7.0
HEADER_LINE_PAD_CM = 0.22
HEADER_ROW_PAD_CM = 0.24

#: Shared-legend geometry, expressed as multiples of the legend font size in cm
#: instead of fixed centimetres.  A page-level legend is not confined to a grid
#: cell, so its type may legitimately be larger than a panel's tick labels; making
#: the swatch and gaps font-relative keeps the row proportional when it is.  At
#: 6 pt these ratios reproduce the original 0.62 cm swatch / 0.12 cm text gap /
#: 0.75 cm item gap exactly.
LEGEND_SWATCH_TEXT_RATIO = 3.0
LEGEND_TEXT_GAP_RATIO = 0.6
LEGEND_ITEM_GAP_RATIO = 3.5

#: A composed page keeps one data workbook for the panels (plus at most one
#: book Origin itself contributes), no matter how many panels it holds.  The
#: gate fails the run if the OPJU starts growing a book per panel again.
MAX_PANEL_PAGE_WORKBOOKS = 2


class PanelCompositionError(Exception):
    """Structured failure carrying a stable code for the CLI report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# spec
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PanelRequest:
    name: str
    plan_file: Path
    row: int
    col: int
    x_title: str | None = None
    y_title: str | None = None
    legend: bool | None = None


@dataclass(frozen=True)
class PanelGridSpec:
    rows: int
    cols: int
    page_width_cm: float
    page_height_cm: float
    panels: tuple[PanelRequest, ...]
    margins_cm: Mapping[str, float]
    gaps_cm: Mapping[str, float]
    legend_mode: str = "none"
    share_x_axis: bool = True
    share_y_axis: bool = True
    typography_scale: float | None = None
    line_width_scale: float | None = None
    #: Marker/symbol size follows ``line_width_scale`` by default, because a
    #: symbol is a stroke and must keep its ratio to the line it annotates.
    #: Leaving it unscaled is what made composed panels look bead-covered.
    marker_scale: float | None = None
    #: Shared page-legend font size.  Defaults to a page-level scale (see
    #: ``resolve_page_legend_typography``) because the legend is a whole-page
    #: key, not a cell-local label.
    legend_pt: float | None = None
    label_band_cm: Mapping[str, float] | None = None
    legend_band_cm: float = DEFAULT_LEGEND_BAND_CM
    raster_width: int = 2400
    #: Optional page-level headers, one entry per column / per row (row order is
    #: top to bottom).  They occupy their own reserved bands so a matrix page
    #: stays identifiable without a separate per-window filename.
    column_titles: tuple[str, ...] | None = None
    row_titles: tuple[str, ...] | None = None
    header_pt: float = DEFAULT_HEADER_PT
    header_bands_cm: Mapping[str, float] | None = None


def _require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PanelCompositionError("panel_spec_invalid", f"{label} must be a number.")
    return float(value)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PanelCompositionError("panel_spec_invalid", f"{label} must be a positive integer.")
    return value


def _numeric_block(
    container: Any,
    defaults: Mapping[str, float],
    label: str,
) -> dict[str, float]:
    result = dict(defaults)
    if container is None:
        return result
    if not isinstance(container, Mapping):
        raise PanelCompositionError("panel_spec_invalid", f"{label} must be an object.")
    for key in defaults:
        if key in container:
            result[key] = _require_number(container[key], f"{label}.{key}")
    return result


def parse_panel_spec(payload: Mapping[str, Any], *, base_dir: Path) -> PanelGridSpec:
    """Validate a panel-grid spec without touching Origin."""

    if not isinstance(payload, Mapping):
        raise PanelCompositionError("panel_spec_invalid", "The panel spec must be an object.")
    grid = payload.get("grid")
    if not isinstance(grid, Mapping):
        raise PanelCompositionError("panel_spec_invalid", "The panel spec needs a grid object.")
    rows = _positive_int(grid.get("rows"), "grid.rows")
    cols = _positive_int(grid.get("cols"), "grid.cols")

    raw_panels = payload.get("panels")
    if not isinstance(raw_panels, Sequence) or isinstance(raw_panels, (str, bytes)):
        raise PanelCompositionError("panel_spec_invalid", "The panel spec needs a panels list.")
    if not raw_panels:
        raise PanelCompositionError("panel_spec_invalid", "The panels list is empty.")

    panels: list[PanelRequest] = []
    seen_cells: set[tuple[int, int]] = set()
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_panels):
        if not isinstance(raw, Mapping):
            raise PanelCompositionError(
                "panel_spec_invalid", f"panels[{index}] must be an object."
            )
        name = str(raw.get("name") or "").strip()
        if not name:
            raise PanelCompositionError("panel_spec_invalid", f"panels[{index}] needs a name.")
        if name in seen_names:
            raise PanelCompositionError(
                "panel_spec_duplicate_name", f"Duplicate panel name: {name}"
            )
        seen_names.add(name)
        plan_raw = raw.get("plan")
        if not plan_raw:
            raise PanelCompositionError(
                "panel_spec_invalid", f"panels[{index}] needs a render-plan path."
            )
        plan_file = Path(str(plan_raw))
        if not plan_file.is_absolute():
            plan_file = (base_dir / plan_file).resolve()
        if not plan_file.is_file():
            raise PanelCompositionError("panel_plan_missing", f"Render plan not found: {plan_file}")
        row = _positive_int(raw.get("row"), f"panels[{index}].row")
        col = _positive_int(raw.get("col"), f"panels[{index}].col")
        if row > rows or col > cols:
            raise PanelCompositionError(
                "panel_spec_invalid",
                f"panels[{index}] cell ({row},{col}) is outside the {rows}x{cols} grid.",
            )
        if (row, col) in seen_cells:
            raise PanelCompositionError(
                "panel_spec_duplicate_cell", f"Two panels share grid cell ({row},{col})."
            )
        seen_cells.add((row, col))
        legend_raw = raw.get("legend")
        panels.append(
            PanelRequest(
                name=name,
                plan_file=plan_file,
                row=row,
                col=col,
                x_title=(None if raw.get("x_title") is None else str(raw["x_title"])),
                y_title=(None if raw.get("y_title") is None else str(raw["y_title"])),
                legend=(None if legend_raw is None else bool(legend_raw)),
            )
        )

    page = payload.get("page_size_cm")
    if page is None:
        # Grid-derived default: ~5.9 cm per column and ~5.0 cm per row, which
        # keeps the automatic typography scale near the readable floor instead
        # of silently emitting unreadable panel text.
        page_width = max(
            16.0, cols * 5.9 + DEFAULT_MARGINS_CM["left"] + DEFAULT_MARGINS_CM["right"]
        )
        page_height = max(
            12.0, rows * 5.0 + DEFAULT_MARGINS_CM["top"] + DEFAULT_MARGINS_CM["bottom"]
        )
    elif isinstance(page, Sequence) and not isinstance(page, (str, bytes)) and len(page) == 2:
        page_width = _require_number(page[0], "page_size_cm[0]")
        page_height = _require_number(page[1], "page_size_cm[1]")
    else:
        raise PanelCompositionError(
            "panel_spec_invalid", "page_size_cm must be [width_cm, height_cm]."
        )
    if page_width <= 0 or page_height <= 0:
        raise PanelCompositionError("panel_spec_invalid", "page_size_cm must be positive.")

    legend_mode = str(payload.get("legend") or "none").strip().lower()
    if legend_mode not in {"none", "per_panel", "page"}:
        raise PanelCompositionError(
            "panel_spec_invalid", "legend must be 'none', 'per_panel' or 'page'."
        )
    legend_band_cm = _require_number(
        payload.get("legend_band_cm", DEFAULT_LEGEND_BAND_CM), "legend_band_cm"
    )
    if legend_mode == "page" and legend_band_cm <= 0:
        raise PanelCompositionError(
            "panel_spec_invalid", "legend_band_cm must be positive for a page legend."
        )

    typography_scale = payload.get("typography_scale")
    if typography_scale is not None:
        typography_scale = _require_number(typography_scale, "typography_scale")
        if typography_scale <= 0:
            raise PanelCompositionError(
                "panel_spec_invalid", "typography_scale must be positive."
            )
    line_width_scale = payload.get("line_width_scale")
    if line_width_scale is not None:
        line_width_scale = _require_number(line_width_scale, "line_width_scale")
        if line_width_scale <= 0:
            raise PanelCompositionError(
                "panel_spec_invalid", "line_width_scale must be positive."
            )
    marker_scale = payload.get("marker_scale")
    if marker_scale is not None:
        marker_scale = _require_number(marker_scale, "marker_scale")
        if marker_scale <= 0:
            raise PanelCompositionError(
                "panel_spec_invalid", "marker_scale must be positive."
            )
    legend_pt = payload.get("legend_pt")
    if legend_pt is not None:
        legend_pt = _require_number(legend_pt, "legend_pt")
        if legend_pt <= 0:
            raise PanelCompositionError(
                "panel_spec_invalid", "legend_pt must be positive."
            )

    def _titles(key: str, expected: int) -> tuple[str, ...] | None:
        raw = payload.get(key)
        if raw is None:
            return None
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise PanelCompositionError("panel_spec_invalid", f"{key} must be a list of strings.")
        titles = tuple(str(item) for item in raw)
        if len(titles) != expected:
            raise PanelCompositionError(
                "panel_spec_invalid",
                f"{key} needs exactly {expected} entries for this grid, got {len(titles)}.",
            )
        return titles

    column_titles = _titles("column_titles", cols)
    row_titles = _titles("row_titles", rows)
    header_pt = _require_number(payload.get("header_pt", DEFAULT_HEADER_PT), "header_pt")
    if header_pt <= 0:
        raise PanelCompositionError("panel_spec_invalid", "header_pt must be positive.")

    return PanelGridSpec(
        rows=rows,
        cols=cols,
        page_width_cm=page_width,
        page_height_cm=page_height,
        panels=tuple(panels),
        margins_cm=_numeric_block(payload.get("margins_cm"), DEFAULT_MARGINS_CM, "margins_cm"),
        gaps_cm=_numeric_block(payload.get("gaps_cm"), DEFAULT_GAPS_CM, "gaps_cm"),
        legend_mode=legend_mode,
        share_x_axis=bool(payload.get("share_x_axis", True)),
        share_y_axis=bool(payload.get("share_y_axis", True)),
        typography_scale=typography_scale,
        line_width_scale=line_width_scale,
        marker_scale=marker_scale,
        legend_pt=legend_pt,
        label_band_cm=(
            None
            if payload.get("label_band_cm") is None
            else _numeric_block(
                payload.get("label_band_cm"), {"left": 0.0, "bottom": 0.0}, "label_band_cm"
            )
        ),
        legend_band_cm=legend_band_cm,
        raster_width=int(payload.get("raster_width") or 2400),
        column_titles=column_titles,
        row_titles=row_titles,
        header_pt=header_pt,
        header_bands_cm=(
            None
            if payload.get("header_bands_cm") is None
            else _numeric_block(
                payload.get("header_bands_cm"), {"top": 0.0, "left": 0.0}, "header_bands_cm"
            )
        ),
    )


# --------------------------------------------------------------------------
# layout + typography maths (pure; no Origin involved, unit-testable)
# --------------------------------------------------------------------------


def _estimate_text_cm(text: str, pt: float) -> float:
    """Rough Arial width for reserving a header band before Origin is open."""

    return len(text) * pt * (2.54 / 72.0) * 0.58


def header_bands_cm(spec: PanelGridSpec) -> dict[str, float]:
    """Reserve space outside the grid for page-level column / row titles.

    The bands are taken from the grid's usable area (not from the outer page
    margin) so an explicit ``margins_cm`` stays the page-margin contract and the
    titles get their own reserved strip.
    """

    explicit = spec.header_bands_cm
    top = 0.0
    left = 0.0
    if spec.column_titles:
        top = (
            float(explicit.get("top", 0.0))
            if explicit is not None and float(explicit.get("top", 0.0)) > 0
            else spec.header_pt * (2.54 / 72.0) + HEADER_LINE_PAD_CM
        )
    if spec.row_titles:
        left = (
            float(explicit.get("left", 0.0))
            if explicit is not None and float(explicit.get("left", 0.0)) > 0
            else max(_estimate_text_cm(text, spec.header_pt) for text in spec.row_titles)
            + HEADER_ROW_PAD_CM
        )
    return {"top": top, "left": left}


def cell_size_cm(spec: PanelGridSpec) -> tuple[float, float]:
    """Return the nominal grid cell size in centimetres."""

    gap_h = spec.gaps_cm.get("h", 0.0)
    gap_v = spec.gaps_cm.get("v", 0.0)
    bands = header_bands_cm(spec)
    usable_w = (
        spec.page_width_cm
        - spec.margins_cm["left"]
        - spec.margins_cm["right"]
        - bands["left"]
        - (spec.cols - 1) * gap_h
    )
    usable_h = (
        spec.page_height_cm
        - spec.margins_cm["top"]
        - spec.margins_cm["bottom"]
        - bands["top"]
        - (spec.rows - 1) * gap_v
    )
    if spec.legend_mode == "page":
        # A shared legend takes a real band below the grid, so the panels must
        # give that space up rather than overlap it.
        usable_h -= spec.legend_band_cm + gap_v
    if usable_w <= 0 or usable_h <= 0:
        raise PanelCompositionError(
            "panel_layout_impossible",
            "Margins and gaps leave no room for the requested panel grid.",
        )
    return usable_w / spec.cols, usable_h / spec.rows


def legend_band_box(spec: PanelGridSpec) -> dict[str, float] | None:
    """Page-percent box of the shared legend band, or ``None``."""

    if spec.legend_mode != "page":
        return None
    bands = header_bands_cm(spec)
    cell_w, cell_h = cell_size_cm(spec)
    gap_v = spec.gaps_cm.get("v", 0.0)
    top_cm = (
        spec.margins_cm["top"]
        + bands["top"]
        + spec.rows * cell_h
        + (spec.rows - 1) * gap_v
        + gap_v
    )
    left_cm = spec.margins_cm["left"] + bands["left"]
    width_cm = (
        spec.page_width_cm
        - spec.margins_cm["left"]
        - spec.margins_cm["right"]
        - bands["left"]
    )
    return {
        "left": 100.0 * left_cm / spec.page_width_cm,
        "top": 100.0 * top_cm / spec.page_height_cm,
        "width": 100.0 * width_cm / spec.page_width_cm,
        "height": 100.0 * spec.legend_band_cm / spec.page_height_cm,
        "band_cm": [width_cm, spec.legend_band_cm],
    }


def header_title_positions(spec: PanelGridSpec) -> dict[str, Any]:
    """Page-percent centres for each column title and row title."""

    bands = header_bands_cm(spec)
    cell_w, cell_h = cell_size_cm(spec)
    gap_h = spec.gaps_cm.get("h", 0.0)
    gap_v = spec.gaps_cm.get("v", 0.0)
    grid_left = spec.margins_cm["left"] + bands["left"]
    grid_top = spec.margins_cm["top"] + bands["top"]

    columns = [
        {
            "index": col,
            "text": spec.column_titles[col - 1],
            "centre_x_cm": grid_left + (col - 1) * (cell_w + gap_h) + cell_w / 2.0,
            "centre_y_cm": spec.margins_cm["top"] + bands["top"] / 2.0,
        }
        for col in range(1, spec.cols + 1)
    ] if spec.column_titles else []
    rows = [
        {
            "index": row,
            "text": spec.row_titles[row - 1],
            "centre_x_cm": spec.margins_cm["left"] + bands["left"] / 2.0,
            "centre_y_cm": grid_top + (row - 1) * (cell_h + gap_v) + cell_h / 2.0,
        }
        for row in range(1, spec.rows + 1)
    ] if spec.row_titles else []
    return {"columns": columns, "rows": rows, "bands_cm": bands}


def resolve_typography_scale(spec: PanelGridSpec, standalone: AdaptiveOriginStyle) -> float:
    """Scale a standalone adaptive style down to one composed panel cell.

    The standalone style is tuned for its own whole page, so a panel that is a
    single grid cell needs proportionally smaller text and strokes.  The ratio
    is taken against the standalone *page*, which preserves the text-to-plot
    proportion the verified single chart had.
    """

    if spec.typography_scale is not None:
        return spec.typography_scale
    cell_w, cell_h = cell_size_cm(spec)
    ratio = min(
        cell_w / max(standalone.page_width_cm, 1e-6),
        cell_h / max(standalone.page_height_cm, 1e-6),
    )
    return max(MIN_TYPOGRAPHY_SCALE, min(1.0, ratio))


def resolve_marker_scale(spec: PanelGridSpec, line_width_scale: float) -> float:
    """Return the factor applied to a panel's marker/symbol size.

    The standalone style carries a marker size tuned against its own line width.
    A composed panel shrinks the line by ``line_width_scale``; if the symbol kept
    its absolute point size the pair would drift apart and the panel would look
    beaded.  Scaling the symbol by the same factor preserves the verified
    ratio, and the floor only prevents a symbol from disappearing.
    """

    if spec.marker_scale is not None:
        return max(MIN_MARKER_SCALE, float(spec.marker_scale))
    return max(MIN_MARKER_SCALE, float(line_width_scale))


def resolve_page_legend_typography(
    spec: PanelGridSpec,
    reference: AdaptiveOriginStyle,
    panel_style: AdaptiveOriginStyle,
) -> dict[str, float | str]:
    """Return the shared page-legend font size and the evidence behind it.

    A page-level legend spans the whole page, so scaling it by the *cell* ratio
    (which exists to make text fit inside a small panel) understates it -- the
    measured complaint was a 6 pt legend on a 20 cm page.  The legend is instead
    placed on a scale between the two honest bounds:

    * the cell scale, which is what its neighbours in the panels get; and
    * the page scale, ``min(page / standalone page)``, which is what the same
      legend would be if the whole standalone figure had been scaled as a unit.

    The geometric mean of the two keeps it larger than the panel type while
    never exceeding the standalone legend, and it stays monotone in both.
    """

    if spec.legend_pt is not None:
        return {
            "legend_pt": float(_snap_half_pt(spec.legend_pt)),
            "legend_scale": float(spec.legend_pt) / max(reference.legend_size_pt, 1e-6),
            "panel_scale": 0.0,
            "page_scale": 0.0,
            "source": "spec.legend_pt",
        }
    cell_scale = float(panel_style.legend_size_pt) / max(reference.legend_size_pt, 1e-6)
    page_scale = min(
        spec.page_width_cm / max(reference.page_width_cm, 1e-6),
        spec.page_height_cm / max(reference.page_height_cm, 1e-6),
        1.0,
    )
    legend_scale = max(MIN_TYPOGRAPHY_SCALE, min(1.0, (cell_scale * page_scale) ** 0.5))
    size_pt = _snap_half_pt(reference.legend_size_pt * legend_scale)
    # Never smaller than the panel's own legend type, never larger than the
    # verified standalone legend.
    size_pt = max(float(MIN_LEGEND_PT), float(panel_style.legend_size_pt), size_pt)
    size_pt = min(float(reference.legend_size_pt), size_pt)
    return {
        "legend_pt": float(size_pt),
        "legend_scale": legend_scale,
        "panel_scale": cell_scale,
        "page_scale": page_scale,
        "source": "auto(page-scale)",
    }


def _snap_half_pt(value: float) -> float:
    """Snap a font size to Origin's 0.5 pt storage step.

    Probe evidence (verified 2024b/10.15): Origin quantizes every font size --
    ``axis.label.pt`` and every text object's ``fsize`` -- to 0.5 pt.  A
    fractional request such as 5.78 pt reads back as 6.0 pt, which fails the
    engine's 0.05 pt text gate.  The standalone adaptive styles always landed on
    integers, which is why this only surfaces for scaled panels.  Snapping here
    keeps the readback gate meaningful instead of relaxing it.
    """

    return round(float(value) * 2.0) / 2.0


def _round_stroke_pt(value: float) -> float:
    """Strokes store at 0.01 pt; rounding keeps the geometry gate exact."""

    return round(float(value), 2)


def scale_style(
    style: AdaptiveOriginStyle,
    *,
    typography_scale: float,
    line_width_scale: float,
    page_height_cm: float,
) -> AdaptiveOriginStyle:
    """Return the panel-scoped copy of a standalone adaptive style."""

    standalone_lift_cm = style.page_height_cm * style.x_title_upshift_page_percent / 100.0
    lift_cm = standalone_lift_cm * typography_scale
    return replace(
        style,
        page_width_cm=max(style.page_width_cm * typography_scale, 1.0),
        page_height_cm=max(style.page_height_cm * typography_scale, 1.0),
        axis_title_size_pt=max(
            MIN_AXIS_TITLE_PT, _snap_half_pt(style.axis_title_size_pt * typography_scale)
        ),
        tick_label_size_pt=max(
            MIN_TICK_LABEL_PT, _snap_half_pt(style.tick_label_size_pt * typography_scale)
        ),
        legend_size_pt=max(MIN_LEGEND_PT, _snap_half_pt(style.legend_size_pt * typography_scale)),
        plot_line_width_pt=_round_stroke_pt(max(MIN_PLOT_LINE_PT, style.plot_line_width_pt * line_width_scale)),
        frame_line_width_pt=_round_stroke_pt(max(MIN_FRAME_PT, style.frame_line_width_pt * line_width_scale)),
        error_bar_width_pt=_round_stroke_pt(max(MIN_FRAME_PT, style.error_bar_width_pt * line_width_scale)),
        bar_border_width_pt=_round_stroke_pt(max(MIN_FRAME_PT, style.bar_border_width_pt * line_width_scale)),
        major_tick_length_pt=_round_stroke_pt(
            max(MIN_TICK_LENGTH_PT, style.major_tick_length_pt * line_width_scale)
        ),
        minor_tick_length_pt=_round_stroke_pt(
            max(MIN_TICK_LENGTH_PT * 0.6, style.minor_tick_length_pt * line_width_scale)
        ),
        inset_axis_title_size_pt=max(
            MIN_AXIS_TITLE_PT, _snap_half_pt(style.inset_axis_title_size_pt * typography_scale)
        ),
        inset_tick_label_size_pt=max(
            MIN_TICK_LABEL_PT, _snap_half_pt(style.inset_tick_label_size_pt * typography_scale)
        ),
        x_title_upshift_page_percent=(lift_cm / max(page_height_cm, 1e-6)) * 100.0,
    )


def _label_band_cm(style: AdaptiveOriginStyle, spec: PanelGridSpec) -> dict[str, float]:
    """Reserve physically justified room for tick labels and axis titles."""

    if spec.label_band_cm is not None:
        return {
            "left": float(spec.label_band_cm.get("left", 0.0)) or _band_from_style(style)[0],
            "bottom": float(spec.label_band_cm.get("bottom", 0.0)) or _band_from_style(style)[1],
        }
    left, bottom = _band_from_style(style)
    return {"left": left, "bottom": bottom}


def _band_from_style(style: AdaptiveOriginStyle) -> tuple[float, float]:
    cm_per_pt = 2.54 / 72.0
    tick_cm = style.tick_label_size_pt * cm_per_pt
    title_cm = style.axis_title_size_pt * cm_per_pt
    # 0.10 cm tick marks + ~4 characters of tick text + small gaps.
    left = 0.10 + 4.0 * 0.5 * tick_cm + 0.06 + title_cm + 0.12
    bottom = 0.10 + tick_cm + 0.06 + title_cm + 0.08
    return left, bottom


def panel_layer_boxes(
    spec: PanelGridSpec,
    style: AdaptiveOriginStyle,
) -> dict[str, dict[str, Any]]:
    """Return each panel's drawing box in page percent plus cm diagnostics."""

    bands = header_bands_cm(spec)
    cell_w, cell_h = cell_size_cm(spec)
    band = _label_band_cm(style, spec)
    gap_h = spec.gaps_cm.get("h", 0.0)
    gap_v = spec.gaps_cm.get("v", 0.0)
    boxes: dict[str, dict[str, Any]] = {}
    for panel in spec.panels:
        cell_left = (
            spec.margins_cm["left"]
            + bands["left"]
            + (panel.col - 1) * (cell_w + gap_h)
        )
        cell_top = (
            spec.margins_cm["top"]
            + bands["top"]
            + (panel.row - 1) * (cell_h + gap_v)
        )
        left_cm = cell_left + band["left"]
        top_cm = cell_top + 0.04
        width_cm = cell_w - band["left"] - 0.12
        height_cm = cell_h - band["bottom"] - 0.04
        if width_cm <= 0.5 or height_cm <= 0.5:
            raise PanelCompositionError(
                "panel_layout_impossible",
                (
                    f"Panel {panel.name} keeps only {width_cm:.2f} x {height_cm:.2f} cm of "
                    "drawing box after reserving label space. Enlarge page_size_cm or "
                    "reduce the grid."
                ),
            )
        boxes[panel.name] = {
            "left": 100.0 * left_cm / spec.page_width_cm,
            "top": 100.0 * top_cm / spec.page_height_cm,
            "width": 100.0 * width_cm / spec.page_width_cm,
            "height": 100.0 * height_cm / spec.page_height_cm,
            "cell_cm": [cell_w, cell_h],
            "layer_cm": [width_cm, height_cm],
            "cell_origin_cm": [cell_left, cell_top],
        }
    return boxes


def quantize_boxes_to_pixels(
    spec: PanelGridSpec,
    boxes: Mapping[str, Mapping[str, Any]],
    page_width_units: float,
    page_height_units: float,
) -> dict[str, dict[str, Any]]:
    """Snap every panel edge onto Origin's integer page-unit grid.

    Origin stores a layer's page-percent geometry in whole page units (pixels
    at the page's own resolution; 600 px/in on the verified baseline). A raw
    cm-derived percent such as ``top = 8.4375`` is therefore rounded to the
    nearest unit. On a short page that unit step can exceed the 0.03 %
    geometry gate even though the visible error is a few tens of microns, so
    the *intent* is first rounded onto the same grid. The strict readback gate
    then stays meaningful instead of being loosened.
    """

    if page_width_units <= 0 or page_height_units <= 0:
        raise PanelCompositionError(
            "panel_spec_invalid",
            "Origin reported a non-positive page size in page units.",
        )
    snapped: dict[str, dict[str, Any]] = {}
    for name, box in boxes.items():
        left_px = round(float(box["left"]) / 100.0 * page_width_units)
        right_px = round(
            (float(box["left"]) + float(box["width"])) / 100.0 * page_width_units
        )
        top_px = round(float(box["top"]) / 100.0 * page_height_units)
        bottom_px = round(
            (float(box["top"]) + float(box["height"])) / 100.0 * page_height_units
        )
        width_px = max(1, right_px - left_px)
        height_px = max(1, bottom_px - top_px)
        new_box = dict(box)
        new_box["left"] = 100.0 * left_px / page_width_units
        new_box["top"] = 100.0 * top_px / page_height_units
        new_box["width"] = 100.0 * width_px / page_width_units
        new_box["height"] = 100.0 * height_px / page_height_units
        new_box["layer_cm"] = [
            new_box["width"] / 100.0 * spec.page_width_cm,
            new_box["height"] / 100.0 * spec.page_height_cm,
        ]
        snapped[name] = new_box
    return snapped


def boxes_overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return (
        a["left"] < b["left"] + b["width"]
        and b["left"] < a["left"] + a["width"]
        and a["top"] < b["top"] + b["height"]
        and b["top"] < a["top"] + a["height"]
    )


def validate_layout(spec: PanelGridSpec, boxes: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Whole-page gates that do not need Origin."""

    problems: list[str] = []
    names = list(boxes)
    for index, first in enumerate(names):
        box = boxes[first]
        if box["left"] < 0 or box["top"] < 0:
            problems.append(f"{first}: drawing box starts outside the page")
        if box["left"] + box["width"] > 100.0001 or box["top"] + box["height"] > 100.0001:
            problems.append(f"{first}: drawing box runs past the page edge")
        for second in names[index + 1 :]:
            if boxes_overlap(box, boxes[second]):
                problems.append(f"{first} and {second}: drawing boxes overlap")
    return problems


# --------------------------------------------------------------------------
# per-panel preparation
# --------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class PreparedPanel:
    """A panel's frozen preparation plus the provenance needed to audit it."""

    request: PanelRequest
    preparation: ScientificPreparation
    standalone_style: AdaptiveOriginStyle
    provenance: dict[str, Any]


def prepare_panel(panel: PanelRequest) -> PreparedPanel:
    """Rebuild one panel's preparation from its own frozen plan.

    Nothing is recomputed: the plan's column mapping is replayed, the source
    SHA256 must still match the plan, and ambiguous roles are refused.
    """

    try:
        plan = json.loads(panel.plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PanelCompositionError(
            "panel_plan_unreadable", f"{panel.name}: render plan could not be read ({exc})."
        ) from exc
    if not isinstance(plan, Mapping):
        raise PanelCompositionError(
            "panel_plan_unreadable", f"{panel.name}: render plan is not an object."
        )
    template = plan.get("template")
    source = plan.get("source")
    if not isinstance(template, Mapping) or not isinstance(source, Mapping):
        raise PanelCompositionError(
            "panel_plan_invalid", f"{panel.name}: render plan lacks template/source blocks."
        )
    template_id = str(template.get("id") or "")
    if template_id not in COMPOSABLE_TEMPLATE_IDS:
        raise PanelCompositionError(
            "panel_template_unsupported",
            (
                f"{panel.name}: template '{template_id}' is not composable yet. Only the "
                "shared scientific chart renderer has passed the multi-layer gate."
            ),
        )
    source_path = Path(str(source.get("path") or ""))
    if not source_path.is_file():
        raise PanelCompositionError(
            "panel_source_missing", f"{panel.name}: source table not found: {source_path}"
        )
    recorded_sha = str(source.get("sha256") or "").strip().lower()
    actual_sha = _sha256(source_path)
    if recorded_sha and recorded_sha != actual_sha:
        raise PanelCompositionError(
            "panel_source_changed",
            (
                f"{panel.name}: the source table no longer matches the frozen plan "
                f"(recorded {recorded_sha[:12]}..., actual {actual_sha[:12]}...)."
            ),
        )

    mapping_raw = template.get("worker_mapping")
    mapping = None
    if isinstance(mapping_raw, Mapping):
        assignments = mapping_raw.get("assignments")
        if not isinstance(assignments, Mapping):
            raise PanelCompositionError(
                "panel_plan_invalid", f"{panel.name}: worker_mapping.assignments is invalid."
            )
        mapping = ScientificColumnMapping(
            assignments=tuple((str(column), str(role)) for column, role in assignments.items()),
            plot_mode=(str(mapping_raw["plot_mode"]) if mapping_raw.get("plot_mode") else None),
        )

    try:
        preparation = prepare_scientific(source_path, template_id, column_mapping=mapping)
    except Exception as exc:  # noqa: BLE001 - re-raised with a panel-scoped code
        raise PanelCompositionError(
            "panel_preparation_failed", f"{panel.name}: {type(exc).__name__}: {exc}"
        ) from exc
    if preparation.requires_confirmation:
        raise PanelCompositionError(
            "panel_mapping_ambiguous",
            (
                f"{panel.name}: column roles are still ambiguous "
                f"({', '.join(preparation.confirmation_reasons) or 'unspecified'})."
            ),
        )

    contract = plan.get("figure_contract")
    contract = contract if isinstance(contract, Mapping) else {}
    overrides = contract.get("axis_title_overrides")
    overrides = overrides if isinstance(overrides, Mapping) else {}
    x_title = panel.x_title if panel.x_title is not None else overrides.get("x_title")
    y_title = panel.y_title if panel.y_title is not None else overrides.get("y_title")
    if x_title is not None or y_title is not None:
        preparation = apply_scientific_text_overrides(
            preparation, x_title=x_title, y_title=y_title
        )
    palette = contract.get("palette")
    palette_id = palette.get("palette_id") if isinstance(palette, Mapping) else None
    if palette_id:
        preparation = apply_scientific_palette_override(preparation, palette_id=str(palette_id))

    if preparation.plot_spec.inset_series:
        raise PanelCompositionError(
            "panel_inset_unsupported",
            f"{panel.name}: a UV-Vis inset cannot share a composited page.",
        )

    standalone_style = _figure_style(preparation)
    provenance = {
        "name": panel.name,
        "row": panel.row,
        "col": panel.col,
        "plan_file": str(panel.plan_file),
        "template_id": template_id,
        "source_path": str(source_path),
        "source_sha256": actual_sha,
        "source_sha256_matches_plan": bool(recorded_sha) and recorded_sha == actual_sha,
        "worker_mapping": (dict(mapping_raw) if isinstance(mapping_raw, Mapping) else None),
        "x_title": preparation.plot_spec.x_title,
        "y_title": preparation.plot_spec.y_title,
        "standalone_style": standalone_style.to_dict(),
    }
    return PreparedPanel(
        request=panel,
        preparation=preparation,
        standalone_style=standalone_style,
        provenance=provenance,
    )


def apply_panel_style(
    prepared: PreparedPanel,
    *,
    typography_scale: float,
    line_width_scale: float,
    page_height_cm: float,
    marker_scale: float | None = None,
) -> ScientificPreparation:
    """Project a panel's own standalone style onto the composed grid.

    Marker size is scaled alongside the stroke width.  It lives on the display
    plan rather than the adaptive style, so it is replaced here too; leaving it
    at its standalone point size is what made each composed panel look covered
    in oversized beads.
    """

    panel_style = scale_style(
        prepared.standalone_style,
        typography_scale=typography_scale,
        line_width_scale=line_width_scale,
        page_height_cm=page_height_cm,
    )
    spec = prepared.preparation.plot_spec
    resolved_marker_scale = (
        max(MIN_MARKER_SCALE, float(line_width_scale))
        if marker_scale is None
        else max(MIN_MARKER_SCALE, float(marker_scale))
    )
    standalone_marker = float(spec.display_plan.marker_size_pt)
    # A zero marker size is a deliberate "this series draws no symbol" (fit
    # lines, plain trends), so it must stay zero instead of being floored.
    panel_marker = (
        0.0
        if standalone_marker <= 0.0
        else max(MIN_MARKER_PT, standalone_marker * resolved_marker_scale)
    )
    return replace(
        prepared.preparation,
        plot_spec=replace(
            spec,
            display_plan=replace(
                spec.display_plan,
                marker_size_pt=panel_marker,
                figure_style=panel_style,
            ),
        ),
        plan_digest=(
            f"{prepared.preparation.plan_digest}:panel"
            f"@typo{typography_scale:.4f}/stroke{line_width_scale:.4f}"
            f"/marker{resolved_marker_scale:.4f}"
        ),
    )


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------


def _panel_context(
    spec: PanelGridSpec,
    panel: PanelRequest,
    box: Mapping[str, Any],
) -> PanelContext:
    hide_x = spec.share_x_axis and spec.rows > 1 and panel.row != spec.rows
    hide_y = spec.share_y_axis and spec.cols > 1 and panel.col != 1
    hidden_titles: set[str] = set()
    if hide_x:
        hidden_titles.add("x_title")
    if hide_y:
        hidden_titles.add("y_title")
    legend = panel.legend if panel.legend is not None else spec.legend_mode == "per_panel"
    return PanelContext(
        name=panel.name,
        row=panel.row,
        col=panel.col,
        layer_geometry={
            "left": float(box["left"]),
            "top": float(box["top"]),
            "width": float(box["width"]),
            "height": float(box["height"]),
        },
        page_size_cm=(spec.page_width_cm, spec.page_height_cm),
        legend=bool(legend),
        hidden_title_keys=frozenset(hidden_titles),
        hide_x_tick_labels=hide_x,
        hide_y_tick_labels=hide_y,
    )


class _PanelOutput:
    """Stand-in for ``RunOutput``: a composed panel never saves its own OPJU."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.result_opju = output_dir / "result.opju"


def _legend_series(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Visible legend-worthy series: reference/phase-tick helpers are excluded."""

    return [
        item
        for item in items
        if not item.get("is_reference") and not item.get("is_phase_tick")
    ]


def shared_legend_entries(
    panel_series: Mapping[str, list[Mapping[str, Any]]],
    ordered: Sequence[PanelRequest],
) -> list[dict[str, str]]:
    """Legend entries for the grid, taken from what Origin actually drew.

    Each entry reuses the *rendered* colour of the first panel's series, so a
    page legend can never disagree with the panels it describes.
    """

    source = _legend_series(panel_series[ordered[0].name])
    entries: list[dict[str, str]] = []
    for item in source:
        label = re.sub(r"\s+", " ", str(item.get("label") or "").replace("\u00d7", "x")).strip()
        label = label or "Series"
        error_column = item.get("error_column")
        if error_column:
            match = re.search(r"(?i)(SEM|SD|SE|ERR|ERROR)$", str(error_column))
            kind = match.group(1).upper() if match else "error"
            label = f"{label} (+/-{kind})"
        entries.append({"label": label, "color": str(item.get("color") or "#000000")})
    return entries


def assert_shared_series(
    panel_series: Mapping[str, list[Mapping[str, Any]]],
    ordered: Sequence[PanelRequest],
) -> None:
    """A page legend is only honest if every panel drew the same series set."""

    def identity(items: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
        return [
            (str(item.get("label")), str(item.get("color")), str(item.get("error_column")))
            for item in _legend_series(items)
        ]

    reference = identity(panel_series[ordered[0].name])
    for panel in ordered:
        current = identity(panel_series[panel.name])
        if current != reference:
            raise PanelCompositionError(
                "panel_shared_legend_mismatch",
                (
                    f"{panel.name} does not draw the same series/colour set as "
                    f"{ordered[0].name} ({[item[0] for item in current]} vs "
                    f"{[item[0] for item in reference]}); a shared page legend would be wrong."
                ),
            )


def _draw_shared_legend(
    op: Any,
    graph: Any,
    layer_index: int,
    box: Mapping[str, float],
    items: Sequence[Mapping[str, str]],
    colors: Sequence[str],
    *,
    typography_style: AdaptiveOriginStyle,
    page_width_units: float,
    page_height_units: float,
    legend_pt: float | None = None,
    sheet: Any | None = None,
) -> dict[str, Any]:
    """Draw one horizontal legend row in a dedicated page band.

    A 4-row legend does not fit inside a ~5 cm grid cell without covering the
    data, so a composed page gets its own legend layer instead: hidden axes, a
    normalised 0..1 coordinate system, one short colour swatch drawn as an
    ordinary data plot per series, and a plain text object per entry.  Every
    piece stays editable in the OPJU.
    """

    layer = graph[layer_index]
    layer.set_int("unit", 1)
    layer.set_float("left", float(box["left"]))
    layer.set_float("top", float(box["top"]))
    layer.set_float("width", float(box["width"]))
    layer.set_float("height", float(box["height"]))
    layer.set_int("clip", 0)
    for axis_name in ("x", "x2", "y", "y2"):
        layer.set_int(f"{axis_name}.showAxes", 0)
        layer.set_int(f"{axis_name}.ticks", 0)
        layer.set_int(f"{axis_name}.minorTicks", 0)
        layer.set_int(f"{axis_name}.showLabels", 0)
        layer.set_int(f"{axis_name}.showlabel", 0)
        layer.set_int(f"{axis_name}.label.show", 0)
    # A freshly added layer carries Origin's placeholder axis titles, which
    # render as stray "A"/"B" glyphs.  They are separate text objects from the
    # axis-visibility switches above, so hide them explicitly.
    title_state: dict[str, int] = {}
    for name in ("xb", "xt", "yl", "yr"):
        label = layer.label(name)
        if label is None:
            continue
        label.set_int("show", 0)
        title_state[name] = 0
    op.lt_exec("doc -uw;")

    band_left = page_width_units * float(box["left"]) / 100.0
    band_width = page_width_units * float(box["width"]) / 100.0
    band_top = page_height_units * float(box["top"]) / 100.0
    band_height = page_height_units * float(box["height"]) / 100.0
    centre_y = band_top + band_height / 2.0

    units_per_cm = page_width_units * (float(box["width"]) / 100.0) / float(box["band_cm"][0])
    legend_size_pt = (
        float(typography_style.legend_size_pt) if legend_pt is None else float(legend_pt)
    )
    font_cm = legend_size_pt * (2.54 / 72.0)

    labels: list[Any] = []
    widths: list[float] = []
    for item in items:
        label = layer.add_label(str(item["label"]), 0.0, 0.0)
        if label is None:
            raise OriginDrawError("Origin could not create a shared-legend entry.")
        label.set_int("attach", 1)
        label.set_int("show", 1)
        label.set_float("fsize", legend_size_pt)
        label.set_int("font", int(round(float(op.lt_float(f"font({typography_style.font_family})")))))
        label.set_int("color", 1)
        labels.append(label)
    op.lt_exec("doc -uw;")
    for label in labels:
        widths.append(float(label.get_float("width")))
    label_height = max(float(label.get_float("height")) for label in labels)

    swatch = LEGEND_SWATCH_TEXT_RATIO * font_cm * units_per_cm
    text_gap = LEGEND_TEXT_GAP_RATIO * font_cm * units_per_cm
    item_gap = LEGEND_ITEM_GAP_RATIO * font_cm * units_per_cm
    total = sum(swatch + text_gap + width for width in widths) + item_gap * (len(labels) - 1)
    cursor = band_left + band_width / 2.0 - total / 2.0

    placements: list[dict[str, float]] = []
    for index, (label, width) in enumerate(zip(labels, widths, strict=True)):
        swatch_x0 = cursor
        swatch_x1 = cursor + swatch
        label.set_float("left", swatch_x1 + text_gap)
        label.set_float("top", centre_y - label_height / 2.0)
        placements.append({"x0": swatch_x0, "x1": swatch_x1, "label": width})
        cursor = swatch_x1 + text_gap + width + item_gap
    op.lt_exec("doc -uw;")

    if sheet is None:
        sheet = op.new_sheet("w", "Panel Legend")
    if sheet is None:
        raise OriginDrawError("Origin could not create the shared-legend worksheet.")
    for index in range(len(items)):
        sheet.from_list(2 * index, [0.0, 0.0])
        sheet.from_list(2 * index + 1, [0.0, 0.0])
    sheet.cols_axis()

    def to_data_x(page_x: float) -> float:
        return (page_x - band_left) / max(band_width, 1e-6)

    def to_data_y(page_y: float) -> float:
        return (band_top + band_height - page_y) / max(band_height, 1e-6)

    swatch_y = to_data_y(centre_y)
    for index, placement in enumerate(placements):
        sheet.from_list(2 * index, [to_data_x(placement["x0"]), to_data_x(placement["x1"])])
        sheet.from_list(2 * index + 1, [swatch_y, swatch_y])
    op.lt_exec("doc -uw;")

    plots = []
    for index in range(len(items)):
        x_col = chr(ord("A") + 2 * index)
        y_col = chr(ord("B") + 2 * index)
        plot = layer.add_plot(sheet, y_col, x_col, type="l")
        if plot is None:
            raise OriginDrawError("Origin could not add a shared-legend swatch.")
        plot.color = str(colors[index])
        # The swatch is a key, not data: keep it a touch heavier than the panel
        # lines and let it grow with the legend type so it stays visible.
        plot.set_cmd(
            "-w "
            f"{pt_to_origin_width_units(max(typography_style.plot_line_width_pt * 1.5, legend_size_pt * 0.18))}"
        )
        plots.append(plot)

    layer.axis("x").set_limits(0.0, 1.0, 0.2)
    layer.axis("y").set_limits(0.0, 1.0, 0.2)
    op.lt_exec("doc -uw;")

    boxes = [
        {
            "label": str(items[index]["label"]),
            "left": float(labels[index].get_float("left")),
            "top": float(labels[index].get_float("top")),
            "width": widths[index],
            "height": label_height,
        }
        for index in range(len(items))
    ]
    overlaps = []
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            first, second = boxes[a], boxes[b]
            if (
                first["left"] < second["left"] + second["width"]
                and second["left"] < first["left"] + first["width"]
                and first["top"] < second["top"] + second["height"]
                and second["top"] < first["top"] + first["height"]
            ):
                overlaps.append([first["label"], second["label"]])
    inside = all(
        box["left"] >= 0
        and box["top"] >= 0
        and box["left"] + box["width"] <= page_width_units
        and box["top"] + box["height"] <= page_height_units
        for box in boxes
    )
    return {
        "layer_index": layer_index,
        "band_percent": {key: float(box[key]) for key in ("left", "top", "width", "height")},
        "band_cm": list(box["band_cm"]),
        "font_pt": legend_size_pt,
        "swatch_cm": swatch / max(units_per_cm, 1e-6),
        "item_gap_cm": item_gap / max(units_per_cm, 1e-6),
        "items": boxes,
        "swatch_colors": [str(colors[index]) for index in range(len(items))],
        "swatch_plot_ranges": [plot.lt_range() for plot in plots],
        "label_overlaps": overlaps,
        "labels_inside_page": inside,
        "axis_range_readback": {
            "x_from": float(layer.get_float("x.from")),
            "x_to": float(layer.get_float("x.to")),
            "y_from": float(layer.get_float("y.from")),
            "y_to": float(layer.get_float("y.to")),
        },
        "axes_hidden": all(
            int(layer.get_int(f"{axis}.showAxes")) == 0 for axis in ("x", "x2", "y", "y2")
        ),
        "axis_titles_hidden": title_state,
        "calculation_performed": False,
    }


def _draw_page_headers(
    op: Any,
    graph: Any,
    layer_index: int,
    *,
    spec: PanelGridSpec,
    typography_style: AdaptiveOriginStyle,
    page_width_units: float,
    page_height_units: float,
) -> dict[str, Any]:
    """Label the matrix outside the grid: column titles on top, row titles left.

    Without these a composed matrix is unidentifiable, because every cell looks
    alike and the row/column identity would live only in the window filename.
    The titles sit in their own reserved bands inside a dedicated hidden-axis
    layer, so they remain ordinary editable text objects in the OPJU.
    """

    positions = header_title_positions(spec)
    layer = graph[layer_index]
    layer.set_int("unit", 1)
    layer.set_float("left", 0.0)
    layer.set_float("top", 0.0)
    layer.set_float("width", 100.0)
    layer.set_float("height", 100.0)
    layer.set_int("clip", 0)
    for axis_name in ("x", "x2", "y", "y2"):
        layer.set_int(f"{axis_name}.showAxes", 0)
        layer.set_int(f"{axis_name}.ticks", 0)
        layer.set_int(f"{axis_name}.minorTicks", 0)
        layer.set_int(f"{axis_name}.showLabels", 0)
        layer.set_int(f"{axis_name}.showlabel", 0)
        layer.set_int(f"{axis_name}.label.show", 0)
    title_state: dict[str, int] = {}
    for name in ("xb", "xt", "yl", "yr"):
        label = layer.label(name)
        if label is None:
            continue
        label.set_int("show", 0)
        title_state[name] = 0
    op.lt_exec("doc -uw;")

    font_code = int(round(float(op.lt_float(f"font({typography_style.font_family})"))))

    labels: list[Any] = []
    entries: list[Mapping[str, Any]] = []
    for role, group in (("column", positions["columns"]), ("row", positions["rows"])):
        for entry in group:
            label = layer.add_label(str(entry["text"]), 0.0, 0.0)
            if label is None:
                raise OriginDrawError(f"Origin could not create a {role} title.")
            label.set_int("attach", 1)
            label.set_int("show", 1)
            label.set_float("fsize", spec.header_pt)
            label.set_int("font", font_code)
            label.set_int("color", 1)
            labels.append(label)
            entries.append({**entry, "role": role})
    op.lt_exec("doc -uw;")

    measured = [
        (float(label.get_float("width")), float(label.get_float("height")))
        for label in labels
    ]
    for entry, label, (width, height) in zip(entries, labels, measured, strict=True):
        centre_x = page_width_units * float(entry["centre_x_cm"]) / spec.page_width_cm
        centre_y = page_height_units * float(entry["centre_y_cm"]) / spec.page_height_cm
        label.set_float("left", centre_x - width / 2.0)
        label.set_float("top", centre_y - height / 2.0)
    op.lt_exec("doc -uw;")

    # Read back after placement: Origin can reflow page-attached text objects.
    final: list[dict[str, Any]] = []
    for label, entry in zip(labels, entries, strict=True):
        final.append(
            {
                "role": str(entry["role"]),
                "text": str(entry["text"]),
                "index": int(entry["index"]),
                "left": float(label.get_float("left")),
                "top": float(label.get_float("top")),
                "width": float(label.get_float("width")),
                "height": float(label.get_float("height")),
                "fsize": float(label.get_float("fsize")),
            }
        )

    overlaps: list[list[str]] = []
    for a in range(len(final)):
        for b in range(a + 1, len(final)):
            first, second = final[a], final[b]
            if (
                first["left"] < second["left"] + second["width"]
                and second["left"] < first["left"] + first["width"]
                and first["top"] < second["top"] + second["height"]
                and second["top"] < first["top"] + first["height"]
            ):
                overlaps.append([first["text"], second["text"]])
    inside = all(
        item["left"] >= 0
        and item["top"] >= 0
        and item["left"] + item["width"] <= page_width_units
        and item["top"] + item["height"] <= page_height_units
        for item in final
    )
    return {
        "layer_index": layer_index,
        "bands_cm": dict(positions["bands_cm"]),
        "font_pt": spec.header_pt,
        "items": final,
        "label_overlaps": overlaps,
        "labels_inside_page": inside,
        "axes_hidden": all(
            int(layer.get_int(f"{axis}.showAxes")) == 0 for axis in ("x", "x2", "y", "y2")
        ),
        "axis_titles_hidden": title_state,
        "calculation_performed": False,
    }


def _add_sibling_layer(op: Any, graph: Any, purpose: str) -> None:
    """Append one sibling layer to the composed page, or fail loudly.

    ``layadd`` operates on the **active window**, and building a legend needs a
    worksheet for the swatch data -- creating it silently moves activation off
    the graph.  Probing that sequence showed ``layadd`` returning False and
    ``page.nlayers`` reporting the worksheet instead, which is why the page-header
    layer was rejected.  Re-activating the graph here removes the whole class of
    failure rather than the one symptom.
    """

    graph.activate()
    op.lt_exec("doc -uw;")
    if not op.lt_exec("layadd type:=normal;"):
        raise OriginDrawError(f"Origin rejected the {purpose} layer.")


def compose_panel_page(
    spec: PanelGridSpec,
    output_dir: Path,
    *,
    keep_origin_open: bool = True,
    close_origin: bool = False,
) -> dict[str, Any]:
    """Render the whole grid into one editable Origin page."""

    output_dir.mkdir(parents=True, exist_ok=True)
    keep_open = keep_origin_open and not close_origin
    ordered = sorted(spec.panels, key=lambda item: (item.row, item.col))

    # 1. Prepare every panel before Origin starts: the plan/source gates and
    #    the style probe must fail fast, and Origin should never be launched
    #    for a composition that cannot be laid out.
    prepared = {panel.name: prepare_panel(panel) for panel in ordered}
    widest = max(item.standalone_style.axis_title_size_pt for item in prepared.values())
    tallest = max(item.standalone_style.tick_label_size_pt for item in prepared.values())
    reference_style = prepared[ordered[0].name].standalone_style
    typography_scale = resolve_typography_scale(spec, reference_style)
    line_scale = spec.line_width_scale if spec.line_width_scale is not None else typography_scale
    marker_scale = resolve_marker_scale(spec, line_scale)
    grid_style = scale_style(
        replace(reference_style, axis_title_size_pt=widest, tick_label_size_pt=tallest),
        typography_scale=typography_scale,
        line_width_scale=line_scale,
        page_height_cm=spec.page_height_cm,
    )
    legend_typography = resolve_page_legend_typography(spec, reference_style, grid_style)
    boxes = panel_layer_boxes(spec, grid_style)
    problems = validate_layout(spec, boxes)
    if problems:
        raise PanelCompositionError("panel_layout_invalid", "; ".join(problems))

    readability = {
        "tick_label_size_pt": grid_style.tick_label_size_pt,
        "axis_title_size_pt": grid_style.axis_title_size_pt,
        "legend_size_pt": (
            legend_typography["legend_pt"]
            if spec.legend_mode == "page"
            else grid_style.legend_size_pt
        ),
        "legend_scale": legend_typography["legend_scale"],
        "marker_scale": marker_scale,
        "plot_line_width_pt": grid_style.plot_line_width_pt,
        "comfort_tick_label_pt": COMFORT_TICK_LABEL_PT,
        "comfort_axis_title_pt": COMFORT_AXIS_TITLE_PT,
        "below_comfort": [
            name
            for name, value, floor in (
                ("tick_label_size_pt", grid_style.tick_label_size_pt, COMFORT_TICK_LABEL_PT),
                ("axis_title_size_pt", grid_style.axis_title_size_pt, COMFORT_AXIS_TITLE_PT),
            )
            if value < floor
        ],
    }
    readability["note"] = (
        "Panel type is scaled from each chart's own adaptive style to the grid cell. "
        "A dense grid legitimately forces small type; enlarge page_size_cm, reduce the "
        "grid, or set typography_scale explicitly to change it. The shared page legend "
        "and the markers are scaled separately: legend_type follows a page-level scale "
        "(legend_pt overrides it), markers follow line_width_scale (marker_scale "
        "overrides it) because a symbol is a stroke."
    )

    report: dict[str, Any] = {
        "panel_composer_version": PANEL_COMPOSER_VERSION,
        "page_size_cm": [spec.page_width_cm, spec.page_height_cm],
        "grid": {"rows": spec.rows, "cols": spec.cols},
        "cell_size_cm": list(cell_size_cm(spec)),
        "typography_scale": typography_scale,
        "line_width_scale": line_scale,
        "marker_scale": marker_scale,
        "legend_typography": legend_typography,
        "typography_readability": readability,
        "legend_mode": spec.legend_mode,
        "share_x_axis": spec.share_x_axis,
        "share_y_axis": spec.share_y_axis,
        "grid_style": grid_style.to_dict(),
        "layout_problems": problems,
        "panels": [],
    }

    logger = RunLogger(output_dir / "panel_run.log", output_dir)

    with OriginSession(keep_open=keep_open) as session:
        op = session.op
        if op is None or session.environment is None:
            raise OriginDrawError("Origin session was not initialized")
        report["origin_environment"] = {"backend": "Origin", **session.environment.to_dict()}

        graph = op.new_graph("PANEL Figure", template="Line")
        if graph is None:
            raise OriginDrawError("Origin could not create the panel page.")
        graph.activate()
        graph.obj.LT_execute(
            "page.updatetoprinter=0;page.kar=0;"
            f"page.width=({spec.page_width_cm / 2.54:g})*page.resx;"
            f"page.height=({spec.page_height_cm / 2.54:g})*page.resy;doc -uw;"
        )
        graph.set_int("background", op.ocolor("#FFFFFF"))

        # Snap every panel box onto Origin's integer page-unit grid now that the
        # real page resolution is known. A cm-derived percentage that does not
        # land on that grid would be rounded by Origin on write and then fail
        # the strict per-layer geometry gate. Rounding the intent here keeps the
        # write and the readback expectations identical.
        boxes = quantize_boxes_to_pixels(
            spec,
            boxes,
            float(op.lt_float("page.width")),
            float(op.lt_float("page.height")),
        )
        problems = validate_layout(spec, boxes)
        if problems:
            raise PanelCompositionError("panel_layout_invalid", "; ".join(problems))

        for _ in range(1, len(ordered)):
            _add_sibling_layer(op, graph, "sibling panel")
        op.lt_exec("doc -uw;")
        report["layer_count_readback"] = int(op.lt_int("page.nlayers"))
        if report["layer_count_readback"] != len(ordered):
            raise OriginDrawError(
                f"Origin created {report['layer_count_readback']} layers for "
                f"{len(ordered)} panels."
            )
        report["layer_names"] = [str(graph[i].name) for i in range(len(graph))]

        # 1b. One workbook for the whole page.
        #     ``op.new_sheet`` allocates a *brand-new workbook* on every call, so
        #     the per-panel default left a twelve-panel figure carrying thirteen
        #     single-sheet books.  Creating the book and every panel's sheet up
        #     front keeps the project to one data book; doing it before the
        #     drawing loop also means no worksheet is ever created while the
        #     graph has to stay the active window (``layadd`` and ``page.*`` are
        #     both resolved against the active window).
        input_book = op.new_book("w", "Panel Input")
        if input_book is None:
            raise OriginDrawError("Origin could not create the shared panel workbook.")
        panel_sheets: dict[str, Any] = {}
        for position, panel in enumerate(ordered):
            sheet_name = f"{prepared[panel.name].preparation.template_id.upper()} Input {panel.name}"
            if position == 0:
                # Reuse the sheet the new book already ships with rather than
                # adding a second one, so the project keeps no orphan "Sheet1".
                sheet = input_book[0]
                sheet.name = sheet_name
            else:
                sheet = input_book.add_sheet(sheet_name, active=False)
            if sheet is None:
                raise OriginDrawError(f"Origin could not prepare the panel sheet {sheet_name}.")
            panel_sheets[panel.name] = sheet
        graph.activate()
        op.lt_exec("doc -uw;")

        # 2. Draw. Panel i is layer i; each panel re-runs its own verified
        #    single-chart path, only scoped to that layer and to the panel style.
        panel_series: dict[str, list[Mapping[str, Any]]] = {}
        for index, panel in enumerate(ordered):
            layer = graph[index]
            graph.activate()
            layer.activate()
            item = prepared[panel.name]
            panel_preparation = apply_panel_style(
                item,
                typography_scale=typography_scale,
                line_width_scale=line_scale,
                page_height_cm=spec.page_height_cm,
                marker_scale=marker_scale,
            )
            context = _panel_context(spec, panel, boxes[panel.name])
            frame = load_scientific_frame(
                panel_preparation.source_path, panel_preparation
            )
            _, panel_report = _build_origin_graph(
                op,
                frame,
                _PanelOutput(output_dir),
                panel_preparation,
                page=graph,
                layer=layer,
                panel=context,
                worksheet=panel_sheets[panel.name],
            )
            provenance = dict(item.provenance)
            provenance["panel_style"] = panel_preparation.plot_spec.display_plan.figure_style.to_dict()
            provenance["layer_index"] = index
            provenance["layer_name"] = str(layer.name)
            provenance["geometry_percent"] = {
                key: boxes[panel.name][key] for key in ("left", "top", "width", "height")
            }
            provenance["layer_cm"] = boxes[panel.name]["layer_cm"]
            provenance["hidden_title_keys"] = sorted(context.hidden_title_keys)
            provenance["x_tick_labels_hidden"] = context.hide_x_tick_labels
            provenance["y_tick_labels_hidden"] = context.hide_y_tick_labels
            provenance["standalone_marker_size_pt"] = float(
                item.preparation.plot_spec.display_plan.marker_size_pt
            )
            provenance["marker_size_pt"] = float(
                panel_preparation.plot_spec.display_plan.marker_size_pt
            )
            provenance["legend_shown"] = bool(context.legend)
            axis_state = panel_report.get("origin_axis_state") or {}
            provenance["axis_readback"] = {
                key: axis_state.get(key)
                for key in (
                    "x.from",
                    "x.to",
                    "x.inc",
                    "y.from",
                    "y.to",
                    "y.inc",
                    "x.showLabels",
                    "y.showLabels",
                    "x.label.pt",
                    "y.label.pt",
                    "x.thickness",
                    "y.thickness",
                )
            }
            plot_state = panel_report.get("origin_plot_state") or {}
            provenance["plot_readback"] = {
                "line_widths": plot_state.get("line_widths"),
                "error_bar_widths": plot_state.get("error_bar_widths"),
                "error_bar_cap_widths": plot_state.get("error_bar_cap_widths"),
                "colors": plot_state.get("colors"),
                "series_count": len(plot_state.get("colors") or {}),
            }
            provenance["text_readback"] = {
                key: value
                for key, value in (panel_report.get("origin_text_state") or {}).items()
                if key.endswith(".fsize") or key.endswith(".font_code")
            }
            provenance["series_readback"] = [
                {
                    "label": item.get("label"),
                    "color": item.get("color"),
                    "error_column": item.get("error_column"),
                    "plot_type": item.get("plot_type"),
                }
                for item in (panel_report.get("origin_series_columns") or [])
            ]
            panel_series[panel.name] = provenance["series_readback"]
            report["panels"].append(provenance)

        # 2b. Optional page-level shared legend.  A four-row legend does not fit
        #     inside a grid cell without covering the data, so it gets its own
        #     band and layer below the panels.
        if spec.legend_mode == "page":
            assert_shared_series(panel_series, ordered)
            entries = shared_legend_entries(panel_series, ordered)
            band = legend_band_box(spec)
            if band is None:  # pragma: no cover - guarded by legend_mode
                raise PanelCompositionError(
                    "panel_layout_invalid", "A page legend needs a legend band."
                )
            legend_sheet = input_book.add_sheet("Panel Legend", active=False)
            if legend_sheet is None:
                raise OriginDrawError("Origin could not add the shared-legend sheet.")
            # The swatch table needs one X/Y pair per legend entry; grow the
            # sheet before writing so no column is created mid-draw.
            legend_sheet.cols = 2 * len(entries)
            graph.activate()
            op.lt_exec("doc -uw;")
            _add_sibling_layer(op, graph, "shared-legend")
            op.lt_exec("doc -uw;")
            report["legend"] = _draw_shared_legend(
                op,
                graph,
                len(ordered),
                band,
                entries,
                [entry["color"] for entry in entries],
                typography_style=grid_style,
                page_width_units=float(op.lt_float("page.width")),
                page_height_units=float(op.lt_float("page.height")),
                legend_pt=float(legend_typography["legend_pt"]),
                sheet=legend_sheet,
            )
        else:
            report["legend"] = None

        # 2c. Optional page-level column / row headers.  They live in their own
        #     layer so a matrix page keeps its row/column identity in the OPJU
        #     instead of only in the window filename.
        next_layer = len(ordered) + (1 if spec.legend_mode == "page" else 0)
        if spec.column_titles or spec.row_titles:
            _add_sibling_layer(op, graph, "page-header")
            op.lt_exec("doc -uw;")
            report["headers"] = _draw_page_headers(
                op,
                graph,
                next_layer,
                spec=spec,
                typography_style=grid_style,
                page_width_units=float(op.lt_float("page.width")),
                page_height_units=float(op.lt_float("page.height")),
            )
        else:
            report["headers"] = None

        graph.activate()
        op.lt_exec("doc -uw;")

        # 3. Whole-page readback: every layer must still be exactly where the
        #    layout said, so the composed page is verified rather than assumed.
        page_readback: list[dict[str, Any]] = []
        for index, panel in enumerate(ordered):
            layer = graph[index]
            left = float(layer.get_float("left"))
            top = float(layer.get_float("top"))
            width = float(layer.get_float("width"))
            height = float(layer.get_float("height"))
            expected = boxes[panel.name]
            page_readback.append(
                {
                    "name": panel.name,
                    "layer_index": index,
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "expected": {key: expected[key] for key in ("left", "top", "width", "height")},
                    "max_delta_percent": max(
                        abs(left - expected["left"]),
                        abs(top - expected["top"]),
                        abs(width - expected["width"]),
                        abs(height - expected["height"]),
                    ),
                    "inside_page": (
                        left >= 0
                        and top >= 0
                        and left + width <= 100.05
                        and top + height <= 100.05
                    ),
                }
            )
        report["panels_page_readback"] = page_readback
        report["page_units"] = {
            "width": float(op.lt_float("page.width")),
            "height": float(op.lt_float("page.height")),
        }
        report["max_layer_delta_percent"] = max(
            item["max_delta_percent"] for item in page_readback
        )
        report["all_layers_inside_page"] = all(item["inside_page"] for item in page_readback)

        # The composed project must stay navigable: one data book for the whole
        # page, not one book per panel.  Read the real project back rather than
        # trusting the construction order.
        workbooks: list[dict[str, Any]] = []
        for book in op.pages("w"):
            try:
                sheets = [str(sheet.name) for sheet in book]
            except Exception:  # noqa: BLE001 - readback only, never fatal
                sheets = []
            workbooks.append({"name": str(book.name), "sheets": sheets})
        report["workbooks"] = workbooks
        report["workbook_count"] = len(workbooks)
        report["workbooks_ok"] = bool(
            workbooks and len(workbooks) <= MAX_PANEL_PAGE_WORKBOOKS
        )

        report["exports"] = export_graph(
            op,
            graph,
            output_dir / "result.png",
            output_dir / "result.pdf",
            output_dir / "result.tif",
            raster_width=spec.raster_width,
        )
        report["pdf_has_embedded_fonts"] = bool(
            b"/FontFile" in (output_dir / "result.pdf").read_bytes()
        )
        op.save(str(output_dir / "result.opju"))
        legend = report.get("legend")
        legend_ok = True
        if legend is not None:
            legend_ok = bool(
                legend["labels_inside_page"]
                and not legend["label_overlaps"]
                and legend["axes_hidden"]
            )
        report["legend_ok"] = legend_ok
        headers = report.get("headers")
        headers_ok = True
        if headers is not None:
            headers_ok = bool(
                headers["labels_inside_page"]
                and not headers["label_overlaps"]
                and headers["axes_hidden"]
            )
        report["headers_ok"] = headers_ok
        report["programmatic_pass"] = bool(
            report["all_layers_inside_page"]
            and report["max_layer_delta_percent"] <= 0.05
            and report["exports"].get("png")
            and report["exports"].get("pdf")
            and report["exports"].get("tif")
            and report["pdf_has_embedded_fonts"]
            and not report["layout_problems"]
            and legend_ok
            and headers_ok
            and report["workbooks_ok"]
        )

    logger.write(
        f"Composed {len(report['panels'])} panels on "
        f"{spec.page_width_cm:g}x{spec.page_height_cm:g} cm; "
        f"pass={report['programmatic_pass']}"
    )
    write_json(output_dir / "panel_verify_report.json", report)
    return report


__all__ = [
    "COMPOSABLE_TEMPLATE_IDS",
    "PanelCompositionError",
    "PanelGridSpec",
    "PanelRequest",
    "PreparedPanel",
    "apply_panel_style",
    "boxes_overlap",
    "cell_size_cm",
    "compose_panel_page",
    "header_bands_cm",
    "header_title_positions",
    "panel_layer_boxes",
    "parse_panel_spec",
    "prepare_panel",
    "quantize_boxes_to_pixels",
    "resolve_marker_scale",
    "resolve_page_legend_typography",
    "resolve_typography_scale",
    "scale_style",
    "validate_layout",
]
