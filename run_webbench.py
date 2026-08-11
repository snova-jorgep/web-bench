import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# <repo-root> for config_env, so this works when run from another directory.
sys.path.append(str(Path(__file__).resolve().parent))

from config_env import (  # noqa: E402
    config_env_from_argv,
    resolve_config_env,
    write_sidecar,
)
from webbench_report import generate_report


CONFIG_FILENAME = "config_webbench.yaml"
BENCH_GROUP_NAME = "webbench"

current_dir = Path(__file__).resolve().parent
config_path = current_dir / CONFIG_FILENAME
env_file = current_dir / ".env"
model_json_path = current_dir / "apps" / "eval" / "src" / "model.json"
eval_dist = current_dir / "apps" / "eval" / "dist" / "index.js"


def load_env():
    print(f"[SETUP] Loading env from {env_file}")
    if not env_file.exists():
        print(f"[ERROR] {env_file} not found.")
        sys.exit(1)
    load_dotenv(env_file, override=True)
    print("[OK] Environment variables loaded.\n")


def _build_model_entries(cfg: dict) -> list[dict]:
    """Generate model.json entries for all non-skipped provider/model combos."""
    entries = []
    model_mappings = cfg.get("model_mappings", {})
    base_urls = cfg.get("base_urls", {})
    provider_api_keys = cfg.get("provider_api_keys", {})

    for provider, models in model_mappings.items():
        base_url = base_urls.get(provider, "")
        api_key_env = provider_api_keys.get(provider, "")
        for alias, model_id in models.items():
            if model_id == "not_available":
                continue
            entries.append({
                "model": model_id,
                "provider": "openai",
                "apiBase": base_url,
                "apiKey": f"{{{{{api_key_env}}}}}",
                "title": f"{provider}/{alias}",
            })
    return entries


def _inject_models(new_entries: list[dict]) -> str:
    """Add/update our provider entries in model.json; return the original content for restore."""
    original = model_json_path.read_text(encoding="utf-8")
    data = json.loads(original)

    # Build index of existing entries by title for O(1) lookup
    existing_by_title = {m.get("title", m.get("model")): i for i, m in enumerate(data["models"])}
    added = updated = 0
    for entry in new_entries:
        title = entry["title"]
        if title in existing_by_title:
            data["models"][existing_by_title[title]] = entry  # update in-place (picks up new apiBase)
            updated += 1
        else:
            data["models"].append(entry)
            added += 1

    model_json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[SETUP] model.json: {added} added, {updated} updated.")
    return original


def _restore_model_json(original: str):
    model_json_path.write_text(original, encoding="utf-8")
    print("[CLEANUP] model.json restored.")


def run_command(cmd: list, log_file: Path, dry_run: bool, extra_env: dict | None = None) -> bool:
    cmd_str = " ".join(cmd)
    print(f"[RUNNING] {cmd_str}")
    if dry_run:
        print("[DRY-RUN] Skipping execution.")
        return True
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **(extra_env or {})}
    try:
        with open(log_file, "w") as lf:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env=env,
                cwd=str(current_dir),
            )
            lf.write(result.stdout)
            print(result.stdout.strip())
        if result.returncode == 0:
            print(f"[DONE] {cmd_str}")
        else:
            print(f"[FAIL] exit code {result.returncode}: {cmd_str}")
        return result.returncode == 0
    except Exception as e:
        print(f"[EXCEPTION] {cmd_str}: {e}")
        return False


SMOKE_PROJECTS = "@web-bench/calculator,@web-bench/dom"
SMOKE_MODEL_COUNT = 1  # first non-skipped model per provider


def main():
    cli_config_env = config_env_from_argv()
    dry_run = "--dry-run" in sys.argv
    smoke = "--smoke" in sys.argv

    load_env()

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    webbench_opts = cfg.get("webbench_options", {})
    use_stable = webbench_opts.get("use_stable_projects", True)

    config_env = resolve_config_env(cli_config_env, cfg)
    print(f"[INFO] config_env: {config_env}")

    model_entries = _build_model_entries(cfg)
    if not model_entries:
        print("[ERROR] No models to evaluate (all marked not_available).")
        sys.exit(1)

    if smoke:
        # One model per provider, two small projects
        seen_providers: set[str] = set()
        smoke_entries = []
        for e in model_entries:
            provider = e["title"].split("/")[0]
            if provider not in seen_providers:
                smoke_entries.append(e)
                seen_providers.add(provider)
        model_entries = smoke_entries
        use_stable = False
        print(f"[SMOKE] Running {len(model_entries)} models (1 per provider) "
              f"on projects: {SMOKE_PROJECTS}\n")

    model_titles = [e["title"] for e in model_entries]
    models_csv = ",".join(model_titles)

    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_base = current_dir / "logs" / BENCH_GROUP_NAME / run_timestamp
    output_base.mkdir(parents=True, exist_ok=True)

    report_dir = current_dir / "apps" / "eval" / "report" / f"eval-{run_timestamp}"
    s3_prefix = f"fc-so-testing-suite/webbench_snova/{run_timestamp}"

    print(f"[INFO] Run hash: {run_timestamp}")
    print(f"[INFO] Models ({len(model_titles)}): {models_csv}")
    print(f"[INFO] Webbench report dir: {report_dir}")
    print(f"[INFO] S3 prefix: s3://{os.getenv('AWS_S3_BUCKET_NAME')}/{s3_prefix}")
    if dry_run:
        print("[INFO] Dry-run enabled: commands will be printed, not executed.\n")

    if not eval_dist.exists():
        print(f"[ERROR] {eval_dist} not found. Build webbench first:")
        print("  cd tests/webbench_snova && rush rebuild")
        sys.exit(1)

    node_cmd = [
        "node", str(eval_dist),
        "--auto-run",
        f"--hash={run_timestamp}",
        "--without-local-config",
        f"--models={models_csv}",
    ]
    if use_stable:
        node_cmd.append("--use-stable-projects")
    if smoke:
        node_cmd += ["--package-names", SMOKE_PROJECTS]

    log_file = output_base / "run.log"

    if dry_run:
        masked_cmd = node_cmd.copy()
        # keep models visible in dry-run but truncate if very long
        print(f"[DRY-RUN] {' '.join(masked_cmd)}")
    else:
        original_model_json = _inject_models(model_entries)
        try:
            run_command(node_cmd, log_file, dry_run=False)
        finally:
            _restore_model_json(original_model_json)

        # Sidecar written AFTER the node run, not before: report_dir is created by
        # node, and pre-creating it just to drop a file in would be a gratuitous risk.
        # Nothing is lost by waiting - if node dies there is no Evaluation-*.report.md,
        # so a later report run has nothing to parse and the id would be moot. This
        # differs from mini-SWE, where the second stage is a separate cloud eval and a
        # crash in between IS recoverable.
        write_sidecar(report_dir, config_env, cfg.get("base_urls", {}))

        generate_report(str(report_dir), s3_prefix, config_env=config_env)

    print(f"\n[COMPLETE] WebBench run finished.")
    print(f"Local report: {report_dir}")
    print(f"Local logs:   {output_base}")
    print(f"S3 prefix:    s3://{os.getenv('AWS_S3_BUCKET_NAME')}/{s3_prefix}")


if __name__ == "__main__":
    main()
