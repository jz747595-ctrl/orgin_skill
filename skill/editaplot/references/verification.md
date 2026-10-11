# Origin delivery verification

Do not report success from a rendered preview or PNG alone.

## Live connection smoke

Doctor discovery is never connection evidence. Before the first render in an environment, or after
its Python, Origin, or Automation target changes, run the real smoke through an EditaPlot-owned
isolated instance. The smoke must complete:

- Automation activation and initialization;
- actual Origin version and executable-path readback;
- a minimal editable workbook and graph;
- non-empty OPJU, PNG, PDF, and TIF outputs in an isolated test directory;
- minimum axis-object readback.

A smoke failure is a staged technical failure, not a reason to ask the user to open Origin manually.
A successful smoke proves the current connection and minimum core capabilities only; it does not
prove that every template is available. Apply the selected template's capability decision next.

## Mandatory files

- non-empty editable `result.opju`;
- non-empty `result.png`;
- non-empty `result.pdf` with usable text/fonts;
- non-empty `result.tif`;
- validation and provenance reports;
- `origin_verify_report.json` with page/graph/axis/style readback.

## Programmatic checks

- Confirm every reported artifact resolves inside the run output directory.
- Confirm the input copy hash matches the inspected source hash.
- Confirm `data_understanding.status` is `confirmed`, its source hash matches the render source,
  every source column is classified exactly once, no item remains `uncertain`, and every visible
  element binds only primary/secondary source data or an explicitly approved allow-listed helper.
- For a reference-driven plan, confirm the image/spec hashes, essential mark bindings, applied and
  rejected style tokens, and that no reference pixels, OCR text, code, source values, author marks,
  logos, or watermarks entered the plan or output folder.
- Confirm page dimensions and registered layer geometry. The geometry report must include
  `geometry_readback_source`, `layer_unit`, the canonical left/top/width/height values, the
  correctly mapped `layer -x` values, raw bridge values when available, and
  `bridge_geometry_consistent`. Accept a stale bridge only when the two native LabTalk paths agree
  and still match the registered contract; otherwise fail closed.
- Confirm required axes, titles, label tables, scale direction, tick visibility, text sizes and font codes,
  plot widths, frame widths, and special plot labels where applicable.
- Confirm the X and Y tick-label font codes are identical to the registered profile after any
  dataset-backed label binding. Confirm Y-axis typography independently; do not infer it from X.
- Confirm legends, direct labels, notes, heatmap cell text, and colorbar labels have their own
  point-size and font readback whenever those objects exist.
- Treat missing readback as a rendering failure even if exports exist.
- For Rietveld figures, confirm Observed/Calculated/optional Background/Difference source bindings,
  that Publication `Diff` uses identity display values, and that every phase-tick plot reads its X
  range from the original sparse Phase column while only its Y lane is a helper. Confirm the
  documented point-sized vertical-bar symbol kind `10` when phase ticks exist. Symbol `58` is a
  full-height X-position line and must not be accepted as a Rietveld reflection tick.

## Multi-panel checks

For a `panel` composition, apply every single-figure check per panel and add:

- Confirm `layer_count_readback` equals the number of requested panels and that `layer_names` are the
  expected sibling layers.
- Confirm `max_layer_delta_percent` stays under the `0.05` page-percent gate and
  `all_layers_inside_page` is true.
- Confirm each panel's `source_sha256_matches_plan` is true; a mismatch is a hard failure, not a
  warning.
- Confirm each panel's `hidden_title_keys`, `x_tick_labels_hidden`, and `y_tick_labels_hidden` match
  the shared-axis intent, and that a shared axis really shows its title and tick labels.
- Confirm the shared-legend readback when `legend` is `page`: `labels_inside_page`, empty
  `label_overlaps`, hidden legend-layer axes and placeholder titles, and `swatch_colors` equal to the
  panels' actual rendered series colors.
- Confirm every visual element was scaled, not just the text: compare each panel's `marker_size_pt`
  with its `standalone_marker_size_pt` and confirm the ratio matches `marker_scale`, and that the
  marker-to-line-width ratio still matches the standalone chart. An unchanged marker size on a smaller
  panel is a defect even when every numeric gate passes.
- Confirm each panel's `plot_readback.error_bar_cap_widths` carries the expected `cap_width_pt` (it is
  read back from `plotN.symbol.size`; the `get` command has no `-erwc`). A cap left at Origin's default
  `8` pt is the reason a correctly resized marker still reads as an oversized point — check it before
  shrinking the marker any further.
- Confirm `workbooks_ok` is true and `workbook_count` is small (≤ 2). `workbooks[].sheets` should list
  one input sheet per panel plus `Panel Legend` inside a single data book; a book per panel means the
  project has regrown the `new_sheet` defect and is no longer navigable.
- Confirm `typography_readability`; report any item `below_comfort` to the user instead of hiding it.
- For a shared page legend, confirm `legend_typography.legend_pt` is on the page scale rather than the
  cell scale, that `legend.font_pt` matches it, and that `legend.swatch_cm` scaled with the font.
- Confirm `pdf_has_embedded_fonts` and `programmatic_pass` are true, then open the PNG/TIF and inspect
  the whole grid for clipping, band misalignment, and residual placeholder text.

## Human visual checks

- Open the PNG or TIF and inspect axes, titles, X/Y font consistency, labels, colors, line weights,
  colorbar separation, clipping, and unexpected objects.
- Check that scientific direction and transforms match the figure contract.
- Check that the figure remains readable at its intended physical size.
- Allow legend overlap only when the legend remains editable and no data meaning is lost.
- Record visual QA as pending until a human or Codex with image inspection has reviewed an export.
