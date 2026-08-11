"""
Parse WebBench evaluation reports and upload a summary CSV to S3.

Report structure produced by webbench:
  apps/eval/report/eval-{hash}/
    Evaluation-{hash}.report.md   ← overall summary + per-project tables
    {Project}/
      {Project}-{hash}.report.md  ← per-project summary (all models)
      {Project}-{model}-{hash}/   ← per-model-per-project detail

The Evaluation-{hash}.report.md contains:
  ### Overview          ← overall averages across all projects
  ### {ProjectName}     ← per-project breakdown

Each table has rows:
  | >> Provider/Alias |  pass@1% | pass@2% |  error@1% | inputTokens | outputTokens |

We generate one CSV row per (model, project) combination, plus an "overall" row.
"""

import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import boto3
from dotenv import load_dotenv

# <repo-root> for config_env, so this works when run from another directory.
sys.path.append(str(Path(__file__).resolve().parent))

from config_env import (  # noqa: E402
    config_env_from_argv,
    internal_providers_from,
    read_sidecar,
    resolve_config_env,
    row_config_env,
)


def _load_env():
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=True)


def _upload_to_s3(local_path: Path, s3_prefix: str):
    try:
        bucket = os.environ.get("AWS_S3_BUCKET_NAME", "")
        s3_key = f"{s3_prefix}/{local_path.name}"
        s3 = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION", ""),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        )
        s3.upload_file(str(local_path), bucket, s3_key)
        print(f"[UPLOAD] {local_path} → s3://{bucket}/{s3_key}")
    except Exception as e:
        print(f"[WARN] Upload failed for {local_path}: {e}")


def _parse_percent(val: str) -> float | None:
    m = re.search(r"([\d.]+)%", val)
    return float(m.group(1)) if m else None


def _parse_int(val: str) -> int | None:
    m = re.search(r"\d+", val.strip())
    return int(m.group()) if m else None


def _parse_model_row(cells: list[str]) -> dict:
    """
    Parse value cells from a '| >> model | ...' table row.
    Cells layout (retry=1): pass@1 | inputTokens | outputTokens
    Cells layout (retry=2): pass@1 | pass@2 | error@1 | inputTokens | outputTokens
    """
    pct_cells = [c for c in cells if "%" in c]
    int_cells = [c for c in cells if "%" not in c and re.search(r"\d+", c)]

    n = len(pct_cells)
    pass_at_1 = _parse_percent(pct_cells[0]) if n >= 1 else None
    pass_at_2 = _parse_percent(pct_cells[1]) if n >= 2 else None
    error_at_1 = _parse_percent(pct_cells[n - 1]) if n >= 2 else None  # last % col is error

    input_tokens = _parse_int(int_cells[0]) if len(int_cells) > 0 else None
    output_tokens = _parse_int(int_cells[1]) if len(int_cells) > 1 else None

    return {
        "pass_at_1": pass_at_1,
        "pass_at_2": pass_at_2,
        "error_at_1": error_at_1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _split_title(title: str) -> tuple[str, str]:
    if "/" in title:
        provider, _, model = title.partition("/")
        return provider.strip(), model.strip()
    return "unknown", title.strip()


def _parse_report(report_md: str) -> list[dict]:
    """
    Parse Evaluation-{hash}.report.md.
    Returns list of dicts with keys:
      section, model_title, pass_at_1, pass_at_2, error_at_1, input_tokens, output_tokens
    'section' is "Overview" or the project name.
    """
    rows = []
    current_section = None

    for line in report_md.splitlines():
        stripped = line.strip()

        # Section headings: ### Overview or ### ProjectName
        if stripped.startswith("### "):
            current_section = stripped[4:].strip()
            continue

        if current_section is None:
            continue

        # Model data rows: | >> Provider/Alias | ... |
        if not stripped.startswith("| >>"):
            continue

        cells = [c.strip() for c in stripped.split("|")]
        # cells[0]='' cells[1]='>> Provider/Alias' cells[2..N-1]=values cells[N]=''
        model_title = cells[1].lstrip(">").strip()
        value_cells = [c for c in cells[2:] if c]

        metrics = _parse_model_row(value_cells)
        rows.append({"section": current_section, "model_title": model_title, **metrics})

    return rows


def generate_report(
    report_dir: str,
    s3_prefix: str | None = None,
    config_env: str | None = None,
) -> str | None:
    """
    Parse Evaluation-*.report.md in report_dir, write a CSV, optionally upload to S3.

    Args:
        report_dir: path to apps/eval/report/eval-{hash}/
        s3_prefix:  S3 key prefix for upload

    Returns:
        Path to the generated CSV, or None if no data found.
    """
    report_dir_path = Path(report_dir)

    # Invoked standalone against an existing eval-{hash} dir there is no flag, so fall
    # back to the sidecar written at generation time, then to the registry default.
    if config_env is None:
        config_env = read_sidecar(report_dir_path)
    config_env = resolve_config_env(config_env, None)
    internal_providers = internal_providers_from(None)
    print(f"[REPORT] config_env: {config_env}")

    # Find the top-level report: Evaluation-{hash}.report.md
    candidates = list(report_dir_path.glob("Evaluation-*.report.md"))
    if not candidates:
        print(f"[WARN] No Evaluation-*.report.md found in {report_dir}")
        return None

    report_path = candidates[0]
    content = report_path.read_text(encoding="utf-8")
    parsed_rows = _parse_report(content)

    if not parsed_rows:
        print(f"[WARN] No model rows found in {report_path}")
        return None

    now = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
    csv_rows = []
    for r in parsed_rows:
        provider, model = _split_title(r["model_title"])
        csv_rows.append({
            "date": now,
            "provider": provider,
            "model": model,
            "project": r["section"],
            "pass_at_1": r["pass_at_1"],
            "pass_at_2": r["pass_at_2"],
            "error_at_1": r["error_at_1"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            # Must stay LAST: the Athena regex expects config_env trailing. Applies to
            # the Overview row too, which is the only one the unified view reads.
            "config_env": row_config_env(config_env, provider, internal_providers),
        })
        print(f"  {provider}/{model} [{r['section']}]: "
              f"pass@1={r['pass_at_1']}% error@1={r['error_at_1']}%")

    csv_path = report_dir_path / f"results_{now}.csv"
    fieldnames = ["date", "provider", "model", "project",
                  "pass_at_1", "pass_at_2", "error_at_1",
                  "input_tokens", "output_tokens", "config_env"]

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"[REPORT] CSV written to: {csv_path}")

    if s3_prefix:
        _upload_to_s3(csv_path, s3_prefix)
        _upload_to_s3(report_path, s3_prefix)

    return str(csv_path)


if __name__ == "__main__":
    cli_config_env = config_env_from_argv()
    if len(sys.argv) < 2:
        print("Usage: webbench_report.py <report_dir> [s3_prefix] [--config-env <id>]")
        print("  report_dir: path to apps/eval/report/eval-{hash}/")
        sys.exit(1)

    _load_env()
    s3_prefix = sys.argv[2] if len(sys.argv) > 2 else None
    generate_report(sys.argv[1], s3_prefix, config_env=cli_config_env)
