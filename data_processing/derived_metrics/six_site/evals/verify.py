"""Verify a fresh export against the archived six-site baseline."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

REFERENCE = Path(__file__).resolve().parents[1] / "configs/reference.json"


def verify(output_dir):
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "input_manifest.json").read_text(encoding="utf-8"))
    hashes = {item["name"]: item["sha256"] for item in manifest}
    if hashes != reference["input_sha256"]:
        changed = sorted(name for name, digest in hashes.items()
                         if reference["input_sha256"].get(name) != digest)
        raise ValueError(f"Inputs differ from the archived baseline: {changed}")
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    if summary != reference["summary"]:
        changed = sorted(key for key, value in reference["summary"].items()
                         if summary.get(key) != value)
        raise ValueError(f"Summary differs from the archived baseline: {changed}")

    checks = pd.read_csv(output_dir / "validation.csv")
    if checks.empty or not checks.status.isin(["PASS", "NOT_ESTABLISHED"]).all():
        raise ValueError("Model validation failed")

    seconds = pd.read_csv(output_dir / "seconds_six_site_model.csv", low_memory=False)
    keys = ["primary_record_id", "time_of_day_s"]
    if seconds.duplicated(keys).any() or len(seconds) != summary["all_seconds"]:
        raise ValueError("Invalid exported second grid")
    if seconds.n_raw.sum() != summary["raw_samples"]:
        raise ValueError("Raw sample count changed after export")
    if not seconds.loc[seconds.speed_model_imputed_flag, "original_missing_flag"].all():
        raise ValueError("Speed imputation overlaps observed seconds")

    pair = seconds.distance_pair_available_flag & seconds.stop_identity_scope.eq(
        "six_candidate_direction_specific_route"
    )
    keys = ["assumed_direction", "prev_stop_id", "next_stop_id"]
    intervals = pd.read_csv(output_dir / "direction_stop_intervals.csv")
    mapped = seconds.loc[pair].merge(intervals, on=keys, how="left", validate="many_to_one")
    total = mapped.distance_from_prev_stop_m + mapped.distance_to_next_stop_m
    if mapped.empty or not np.allclose(total, mapped.distance_m, rtol=0, atol=1e-5):
        raise ValueError("Exported stop pairs do not conserve route distance")
    print(f"PASS: {len(seconds):,} seconds, {summary['raw_samples']:,} raw samples")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    verify(parser.parse_args().output_dir)
