# orgin_skill · an EditaPlot distribution build

This is a **personal distribution copy**: a snapshot of the open-source project
[EditaPlot](https://github.com/hang-jin/editaplot) (Apache-2.0) at `0172103`, plus local changes made
after that snapshot. It is **not an upstream release** — the link above is the original project.

Its purpose is narrow: hand the whole repository to another agent (or a colleague) so that one
`git clone` is enough to install it, self-check, and draw.

## One line to give an agent

> Clone this repository and read `docs/installation.md`. Install with
> `.\editaplot.cmd setup --target "<your skill directory>\editaplot"`. If your platform has no
> skill-directory concept, skip `setup` and call `editaplot.cmd` from the repository root instead.
> Then run `doctor` → `doctor --repair` → `origin-smoke`.
> **Never reduce the repository to the `skill/editaplot` subfolder** — the drawing engine is `runtime/`.

## Install

Requirements: a **physical Windows 10/11 x64** computer, **64-bit CPython 3.10–3.12**, and a local
**Origin / OriginPro 2021–2026b** (2024b / 10.15 is the fully verified baseline).

```powershell
git clone https://github.com/jz747595-ctrl/orgin_skill.git
cd orgin_skill
.\editaplot.cmd doctor
.\editaplot.cmd doctor --repair
.\editaplot.cmd origin-smoke --output-dir "$env:TEMP\editaplot-smoke"
```

On Codex, `setup` installs into the current user's `.codex/skills`; every other platform can point
`--target` at its own skill directory. `setup` only places the skill and records where `runtime/`
lives; it never draws. **`origin-smoke` is the only step that proves a machine can actually draw**,
and it needs Origin installed locally.

macOS, Linux, WSL, Wine/CrossOver, Parallels and other virtual machines are unsupported. Origin is
never installed or modified for you.

## What differs from upstream

This copy changes **layout only**. No data contract is touched — nothing is recomputed, refitted,
interpolated or normalized:

1. **Multi-panel composition** (`editaplot.cmd panel`) joins several already-verified single charts
   into **one editable multi-layer Origin page**: a shared-axis grid, optional row/column titles,
   and an optional page-level shared legend. The composer replays frozen render plans only; a source
   whose hash drifted is refused with `panel_source_changed` instead of being silently redrawn.
2. **Error-bar caps now follow the marker.** Origin caps every error bar at a constant 8 pt, so a
   correctly shrunk marker still read as an oversized bar. The cap is now set to
   `1.3 × marker size` and verified by reading the value back from `plotN.symbol.size`.
3. **One data workbook per composed page** instead of one workbook per panel, so the merged project
   stays navigable.

Everything else is documentation: four skill documents were re-synced, `docs/installation.md` gained
the generic-platform route above, and `CHANGELOG.md` records this build.

## Current verification status (stated plainly)

| Item | Status |
|---|---|
| The repository's own public-release audit | Passes: 0 errors, 0 warnings (680 files) |
| Fresh clone → `doctor --repair` | Passes; dependencies install at their locked versions |
| `doctor` | `ready_for_render = true`, no blockers |
| A real `origin-smoke` run | Passes; OPJU / PNG / PDF / TIF all produced |

- **The verified chart combinations are limited**: multi-panel was exercised on a few combinations,
  not the full template matrix.
- The full test suite runs in the repository's CI on every push; check the **Actions** tab for it.
- Measured on: Windows 11 x64, Origin 2024b, CPython 3.12.9.

## Style choices you make outrank the default

When you supply data you can state series colors, line widths, fill transparency and page/aspect
ratio, plus legend display, frame and position. Your explicit choice takes precedence over the
template default. Every field is reported as **applied**, **template default retained**, rejected, or
still needing confirmation — and only a field the current template can actually draw in Origin and
read back counts as applied. A supplied reference figure is a visual brief only; its data, text and
logos are never copied.

## Boundaries kept

- **Your source data is never modified**: the source CSV / TXT / XLS / XLSX file is read-only, and
  any derived helper column lives in memory or inside the editable Origin project.
- **No unauthorized scientific computation**: no automatic smoothing, interpolation, outlier
  removal, fitting, recomputed statistics, or added error bars.
- **Every column is classified first** as drawn, supportive, computation-only, retained-without-drawing,
  or uncertain. An uncertain column blocks the plan rather than becoming a new curve.

## Privacy and permissions

- The local drawing pipeline **does not initiate a network upload of your data**. Anything you hand to
  an agent platform is still governed by that platform's account, organization, and retention policies.
- Medical or portrait data must be deidentified by you, per your institution's rules, before it goes
  to an agent. The tool **does not perform automatic PHI detection**.
- A normal run needs an ordinary user account: no **administrator** rights, no **mouse** control, and no
  changes to **DCOM**, the **registry**, the firewall, or the Origin installation. If a step asks you
  to "run as administrator", it is not the normal path.

## License and provenance

Based on [EditaPlot](https://github.com/hang-jin/editaplot), licensed under
[Apache License 2.0](LICENSE). Upstream attribution and third-party licenses are kept in `NOTICE`,
`AUTHORS.md`, `THIRD_PARTY_NOTICES.md` and `ASSET_PROVENANCE.md`.

This project is **not** affiliated with or endorsed by OriginLab; "Origin" is a trademark of its
respective owner.
