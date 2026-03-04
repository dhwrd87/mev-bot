#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

METRIC_SELECTOR_RE = re.compile(r"(?<![A-Za-z0-9_:])([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^{}]*\})?")
LABEL_MATCH_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|=|!=)\s*\"(?:\\.|[^\"\\])*\"")


@dataclass
class LintIssue:
    dashboard: str
    panel_title: str
    panel_id: int | None
    ref_id: str
    expr: str
    metric: str
    problem: str
    suggestion: str


def _load_contract(path: Path) -> dict[str, set[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    metrics = raw.get("metrics", {})
    if not isinstance(metrics, dict):
        return out
    for name, payload in metrics.items():
        if not isinstance(name, str) or not isinstance(payload, dict):
            continue
        keys = payload.get("label_keys", [])
        out[name] = {str(k) for k in keys if isinstance(k, str)}
    return out


def _iter_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for p in panels:
        if not isinstance(p, dict):
            continue
        found.append(p)
        nested = p.get("panels")
        if isinstance(nested, list):
            found.extend(_iter_panels(nested))
    return found


def _parse_label_matchers(selector: str) -> list[tuple[str, str, str]]:
    inner = selector.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    out: list[tuple[str, str, str]] = []
    for m in LABEL_MATCH_RE.finditer(inner):
        out.append((m.group(1), m.group(2), m.group(0)))
    return out


def _drop_missing_label_matchers(selector: str, missing: set[str]) -> str:
    inner = selector.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    kept: list[str] = []
    for part in parts:
        m = LABEL_MATCH_RE.match(part)
        if not m:
            kept.append(part)
            continue
        if m.group(1) in missing:
            continue
        kept.append(part)
    return "{" + ", ".join(kept) + "}" if kept else ""


def _suggest_expr(expr: str, metric: str, selector: str | None, missing_labels: set[str], contract_metrics: set[str]) -> str:
    if metric not in contract_metrics:
        near = difflib.get_close_matches(metric, [m for m in contract_metrics if m.startswith("mevbot_")], n=3)
        if near:
            return (
                f"Metric `{metric}` not found in contract. "
                f"Use an existing metric (examples: {', '.join(near)})."
            )
        return f"Metric `{metric}` not found in contract; use an existing metric with matching semantics."
    if selector and missing_labels:
        new_selector = _drop_missing_label_matchers(selector, missing_labels)
        return expr.replace(metric + selector, metric + new_selector, 1)
    return expr


def lint_dashboards(contract: dict[str, set[str]], dashboards_root: Path) -> tuple[list[LintIssue], int, int]:
    issues: list[LintIssue] = []
    dash_count = 0
    expr_count = 0

    for path in sorted(dashboards_root.rglob("*.json")):
        dash_count += 1
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            issues.append(
                LintIssue(
                    dashboard=str(path),
                    panel_title="<dashboard>",
                    panel_id=None,
                    ref_id="-",
                    expr="",
                    metric="",
                    problem=f"invalid_json: {e}",
                    suggestion="Fix JSON syntax.",
                )
            )
            continue

        panels = _iter_panels(doc.get("panels", []) if isinstance(doc.get("panels", []), list) else [])
        for panel in panels:
            title = str(panel.get("title") or "<untitled>")
            pid = panel.get("id") if isinstance(panel.get("id"), int) else None
            targets = panel.get("targets", [])
            if not isinstance(targets, list):
                continue
            for t in targets:
                if not isinstance(t, dict):
                    continue
                expr = t.get("expr")
                if not isinstance(expr, str) or not expr.strip():
                    continue
                expr = expr.strip()
                expr_count += 1

                seen_in_expr: set[tuple[str, str | None]] = set()
                for m in METRIC_SELECTOR_RE.finditer(expr):
                    metric = m.group(1)
                    selector = m.group(2)
                    key = (metric, selector)
                    if key in seen_in_expr:
                        continue
                    seen_in_expr.add(key)

                    # Ignore pure function/keyword tokens that are not metrics and have no selector.
                    if metric not in contract and selector is None:
                        continue

                    if metric not in contract:
                        issues.append(
                            LintIssue(
                                dashboard=str(path),
                                panel_title=title,
                                panel_id=pid,
                                ref_id=str(t.get("refId") or "A"),
                                expr=expr,
                                metric=metric,
                                problem="metric_not_in_contract",
                                suggestion=_suggest_expr(expr, metric, selector, set(), set(contract.keys())),
                            )
                        )
                        continue

                    if selector is None:
                        continue

                    used_labels = {k for (k, _, _) in _parse_label_matchers(selector)}
                    missing = used_labels - contract.get(metric, set())
                    if missing:
                        issues.append(
                            LintIssue(
                                dashboard=str(path),
                                panel_title=title,
                                panel_id=pid,
                                ref_id=str(t.get("refId") or "A"),
                                expr=expr,
                                metric=metric,
                                problem=f"labels_not_on_metric: {', '.join(sorted(missing))}",
                                suggestion=_suggest_expr(expr, metric, selector, missing, set(contract.keys())),
                            )
                        )

    return issues, dash_count, expr_count


def write_report(path: Path, issues: list[LintIssue], dash_count: int, expr_count: int, contract_path: Path) -> None:
    lines: list[str] = []
    lines.append("# Grafana Dashboard Lint Report")
    lines.append("")
    lines.append(f"- Contract: `{contract_path}`")
    lines.append(f"- Dashboards scanned: **{dash_count}**")
    lines.append(f"- Panel queries scanned: **{expr_count}**")
    lines.append(f"- Issues found: **{len(issues)}**")
    lines.append("")

    curated = [i for i in issues if "/ARCHIVE/" not in i.dashboard]
    archived = [i for i in issues if "/ARCHIVE/" in i.dashboard]
    lines.append(f"- Curated dashboard issues: **{len(curated)}**")
    lines.append(f"- Archive dashboard issues: **{len(archived)}**")
    lines.append("")

    if not issues:
        lines.append("No contract mismatches found.")
    else:
        if curated:
            lines.append("## Curated Dashboard Issues")
            lines.append("")
            lines.append("| Dashboard | Panel | Ref | Metric | Problem | Suggested PromQL |")
            lines.append("|---|---|---:|---|---|---|")
            for i in curated:
                dash = i.dashboard.replace("|", "\\|")
                panel = i.panel_title.replace("|", "\\|")
                metric = i.metric.replace("|", "\\|")
                problem = i.problem.replace("|", "\\|")
                suggestion = i.suggestion.replace("|", "\\|").replace("\n", " ")
                lines.append(f"| `{dash}` | {panel} | {i.ref_id} | `{metric}` | {problem} | `{suggestion}` |")
            lines.append("")

        if archived:
            lines.append("## Archive Dashboard Issues")
            lines.append("")
            lines.append("_These do not affect provisioned dashboards but are listed for cleanup context._")
            lines.append("")
            lines.append("| Dashboard | Panel | Ref | Metric | Problem | Suggested PromQL |")
            lines.append("|---|---|---:|---|---|---|")
            for i in archived:
                dash = i.dashboard.replace("|", "\\|")
                panel = i.panel_title.replace("|", "\\|")
                metric = i.metric.replace("|", "\\|")
                problem = i.problem.replace("|", "\\|")
                suggestion = i.suggestion.replace("|", "\\|").replace("\n", " ")
                lines.append(f"| `{dash}` | {panel} | {i.ref_id} | `{metric}` | {problem} | `{suggestion}` |")
            lines.append("")

        lines.append("## Issues")
        lines.append("")
        lines.append("| Dashboard | Panel | Ref | Metric | Problem | Suggested PromQL |")
        lines.append("|---|---|---:|---|---|---|")
        for i in issues:
            dash = i.dashboard.replace("|", "\\|")
            panel = i.panel_title.replace("|", "\\|")
            metric = i.metric.replace("|", "\\|")
            problem = i.problem.replace("|", "\\|")
            suggestion = i.suggestion.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{dash}` | {panel} | {i.ref_id} | `{metric}` | {problem} | `{suggestion}` |")

        lines.append("")
        lines.append("## Notes")
        lines.append("")
        lines.append("- `metric_not_in_contract` typically means the metric is absent or stale.")
        lines.append("- `labels_not_on_metric` means label matchers in PromQL cannot match that metric, often causing blank panels.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Lint Grafana dashboard queries against Prometheus metric label contract.")
    ap.add_argument("--contract", default="artifacts/prom_contract.json", help="Path to contract JSON")
    ap.add_argument("--dashboards-root", default="grafana/dashboards", help="Dashboards root")
    ap.add_argument("--report", default="grafana/dashboard_lint_report.md", help="Markdown report output")
    args = ap.parse_args()

    contract_path = Path(args.contract)
    if not contract_path.exists():
        print(f"ERROR: contract file not found: {contract_path}", file=sys.stderr)
        return 2

    contract = _load_contract(contract_path)
    issues, dash_count, expr_count = lint_dashboards(contract, Path(args.dashboards_root))
    report_path = Path(args.report)
    write_report(report_path, issues, dash_count, expr_count, contract_path)
    print(f"Wrote {report_path} (issues={len(issues)}, dashboards={dash_count}, queries={expr_count})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
