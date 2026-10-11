"""Compose a multi-panel Origin page in a dedicated worker subprocess.

Mirrors ``run_template_worker`` for the Origin seam: the interactive-context
gate, the shared Origin job slot, structured error reporting and the progress
protocol are all reused, so a composed page behaves like any other EditaPlot
Origin job (including queueing against a concurrent render).
"""

from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path

from origin_sciplot.origin_backend.execution_context import (
    require_interactive_origin_context,
)
from origin_sciplot.origin_backend.job_queue import origin_job_slot
from origin_sciplot.origin_backend.panel_composer import (
    PanelCompositionError,
    compose_panel_page,
    parse_panel_spec,
)
from origin_sciplot.origin_backend.safe_errors import (
    OriginDrawError,
    OriginEnvironmentError,
    OriginExportError,
    WorkerExitCode,
    origin_activation_recovery,
    safe_error_message,
    structured_error_diagnostics,
)

from . import progress_protocol as proto


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compose an EditaPlot multi-panel page")
    parser.add_argument("--panel-spec-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.set_defaults(keep_origin_open=True)
    parser.add_argument("--keep-origin-open", dest="keep_origin_open", action="store_true")
    parser.add_argument("--close-origin", dest="keep_origin_open", action="store_false")
    return parser


def _report_path(output_dir: str | Path) -> str | None:
    candidate = Path(output_dir).expanduser().resolve() / "panel_verify_report.json"
    return str(candidate) if candidate.is_file() else None


def _report_queue_wait(elapsed_seconds: float) -> None:
    proto.progress(
        "origin_job_queue",
        "waiting",
        f"正在等待另一项 Origin 任务结束；已等待 {int(elapsed_seconds)} 秒。",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec_file = Path(args.panel_spec_file).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    try:
        try:
            payload = json.loads(spec_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PanelCompositionError(
                "panel_spec_unreadable", f"The panel spec could not be read: {exc}"
            ) from exc
        spec = parse_panel_spec(payload, base_dir=spec_file.parent)

        require_interactive_origin_context()
        proto.progress(
            "compose_panels",
            "running",
            f"正在把 {len(spec.panels)} 个面板组合到一个 Origin 页面。",
        )
        with origin_job_slot(
            job_kind="render",
            wait_report_interval=30.0,
            on_wait=_report_queue_wait,
        ) as lease:
            if lease.waited:
                proto.progress(
                    "origin_job_queue",
                    "success",
                    "已获得 Origin 使用权，开始当前任务。",
                )
            report = compose_panel_page(
                spec,
                output_dir,
                keep_origin_open=args.keep_origin_open,
            )
    except PanelCompositionError as exc:
        proto.error(exc.code, exc.message, report=_report_path(output_dir))
        return WorkerExitCode.VALIDATION_FAILED
    except OriginEnvironmentError as exc:
        recovery = origin_activation_recovery(exc.code)
        diagnostics = structured_error_diagnostics(exc)
        proto.error(
            exc.code,
            safe_error_message(exc),
            stage=exc.stage,
            report=_report_path(output_dir),
            **({"diagnostics": diagnostics} if diagnostics else {}),
            **({"recovery": recovery} if recovery is not None else {}),
        )
        return WorkerExitCode.ORIGIN_ENVIRONMENT
    except OriginDrawError as exc:
        proto.error(exc.code, safe_error_message(exc), stage=exc.stage,
                    report=_report_path(output_dir))
        return WorkerExitCode.ORIGIN_DRAW
    except OriginExportError as exc:
        proto.error(exc.code, safe_error_message(exc), stage=exc.stage,
                    report=_report_path(output_dir))
        return WorkerExitCode.EXPORT_FAILED
    except Exception:  # noqa: BLE001 - never leak unexpected local exception details
        proto.error(
            "panel_compose_unexpected",
            "The multi-panel composition failed.",
            stage="unknown",
            report=_report_path(output_dir),
            traceback=traceback.format_exc()[-800:],
        )
        return WorkerExitCode.UNKNOWN

    proto.done(
        status="passed" if report.get("programmatic_pass") else "failed",
        message=(
            f"多面板页已生成：{len(report.get('panels', []))} 个面板，"
            f"{report.get('page_size_cm', ['?', '?'])[0]}×{report.get('page_size_cm', ['?', '?'])[1]} cm。"
        ),
        panel_count=len(report.get("panels", [])),
        programmatic_pass=bool(report.get("programmatic_pass")),
        max_layer_delta_percent=report.get("max_layer_delta_percent"),
        exports=report.get("exports"),
        report=str(output_dir / "panel_verify_report.json"),
    )
    return WorkerExitCode.SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
