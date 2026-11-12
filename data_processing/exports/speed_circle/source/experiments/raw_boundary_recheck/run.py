"""Recheck gap boundaries using original Excel samples and run a separate preview."""
import csv
import json
import runpy
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median, quantiles

from openpyxl import load_workbook

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
RAIL = EXP.parent / "rail_processing"
base = runpy.run_path(str(EXP / "robust_gap_matching/run.py"))
verify = runpy.run_path(str(EXP / "validated_stop_distances/run.py"))
read = base["read"]


def write(name, rows):
    if rows:
        with (HERE / name).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def samples_for(grids, gaps):
    wanted = defaultdict(set)
    for record, events in gaps.items():
        for g in events:
            for edge, step in ((int(g["left_second"]), -1), (int(g["right_second"]), 1)):
                for row in base["context"](grids[record], edge, step):
                    a, b = (int(float(row[k])) for k in ("raw_excel_row_first", "raw_excel_row_last"))
                    wanted[row["file"]].update(range(a, b+1))
    config = json.loads((RAIL / "config.json").read_text(encoding="utf-8-sig"))
    directory = Path(config["input_dir"])
    if not directory.is_absolute():
        directory = Path("/mnt/d") / config["input_dir"].split(":/", 1)[1]
    raw = {}
    for name, lines in wanted.items():
        book = load_workbook(directory / name, read_only=True, data_only=True)
        sheet = book.active
        rows = sheet.values
        header = [str(x) for x in next(rows)]
        cols = [header.index(config["mapping"][k]) for k in ("head_link", "speed", "brake", "traction")]
        raw[name] = {i: tuple(row[k] for k in cols) for i,row in enumerate(rows, 2) if i in lines}
        book.close()
        assert len(raw[name]) == len(lines)
    return raw


def evidence(rows, raw):
    values = []
    for row in rows:
        first, last = (int(float(row[k])) for k in ("raw_excel_row_first", "raw_excel_row_last"))
        values.extend(raw[row["file"]][i] for i in range(first, last+1))
    links = Counter(str(v[0]) for v in values if v[0] is not None)
    link, count = links.most_common(1)[0] if links else ("", 0)
    share = count / sum(links.values()) if links else 0
    return (link if count >= 2 and share >= .70 else "", share, len(values),
            any(v[2:] == (1, 0) for v in values), any(v[2:] == (0, 1) for v in values),
            "|".join(str(v[0]) for v in values),
            "|".join(f"{v[1]}/{v[2]}/{v[3]}" for v in values))


def classify(g, grid, low, running, relaxed):
    d = base["classify"](g, grid, low, running, relaxed)
    left, right = d["left"], d["right"]
    before = base["context"](grid, left, -1)
    after = base["context"](grid, right, 1)
    a, b = evidence(before, RAW), evidence(after, RAW)
    vl, vr = d["left_mps"], d["right_mps"]
    dt = right-left
    quiet = vl is not None and vr is not None and max(vl, vr) <= low
    change = bool(a[0] and b[0] and a[0] != b[0])
    same = bool(a[0] and a[0] == b[0])
    resumed_moving = vr is not None and vr > running and sum(
        float(row["speed_sample_mean"] or 0) > running for row in after) >= 2
    kind, subtype = "review", ""
    if 2 <= dt <= 4 and not change and vl is not None and vr is not None and abs(vl-vr) <= 1:
        kind = "short_gap"
    elif vl is not None and vr is not None and min(vl,vr) > running:
        kind = "moving_gap"
    elif 30 <= dt <= 119 and quiet and same and a[3] and b[4]:
        kind = "intermediate_stop"
    elif dt > 120 and change and vl is not None and vl <= low and a[3]:
        if quiet and b[4]:
            kind, subtype = "terminal_candidate", "near_zero_restart"
        elif resumed_moving and (b[4] or g["brake_release"]=="True"):
            kind, subtype = "terminal_candidate", "restart_unobserved"
    d.update(kind=kind, subtype=subtype, stable_left_link=a[0], stable_right_link=b[0],
             left_link_share=a[1], right_link_share=b[1], left_samples=a[2], right_samples=b[2],
             arrival_state_support=a[3], departure_state_support=b[4],
             left_raw_links=a[5], right_raw_links=b[5],
             left_raw_speed_dmps_brake_traction=a[6], right_raw_speed_dmps_brake_traction=b[6],
             left_time=g["left_time"], right_time=g["right_time"])
    return d


def start_offsets(grids, ledger):
    starts = [g for g in ledger if g["subtype"] == "near_zero_restart"]
    results = []
    for gap in (g for g in ledger if g["subtype"] == "restart_unobserved"):
        matching, pooled = [], []
        target = float(gap["right_mps"])
        for start in starts:
            if start["record"] == gap["record"]:
                continue
            grid = grids[start["record"]]
            t = int(start["right"])
            distance, last = 0.0, float(grid[t]["speed_sample_mean"] or 0)
            for second in range(t+1,t+61):
                row = grid.get(second)
                if not row or not row["speed_sample_mean"] or not int(row["n_raw"]):
                    break
                speed = float(row["speed_sample_mean"])
                if last < target <= speed:
                    fraction = (target-last)/(speed-last)
                    estimate = (second-t-1+fraction, distance+(last+target)*fraction/2)
                    pooled.append(estimate)
                    if start["stable_right_link"] == gap["stable_right_link"]:
                        matching.append(estimate)
                    break
                distance += (last+speed)/2
                last = speed
        estimates = matching if len(matching) >= 3 else pooled
        times = [t for t,d in estimates]
        distances = [d for t,d in estimates]
        low, high = (quantiles(distances,n=10,method="inclusive")[i] for i in (0,8)) if len(distances)>=2 else ("","")
        results.append(dict(record=gap["record"], left_time=gap["left_time"], right_time=gap["right_time"],
                            right_second=gap["right"], target_mps=target, sample_count=len(estimates),
                            source="matching_link" if len(matching) >= 3 else "pooled_departures",
                            estimated_departure_elapsed_s=median(times) if times else "",
                            offset_m=median(distances) if distances else "", p10_m=low,p90_m=high))
    return results


def main():
    global RAW
    grids, gaps = defaultdict(dict), defaultdict(list)
    for row in read(RAIL / "outputs/seconds.csv"):
        grids[row["session_id"]][int(row["time_of_day_s"])] = row
    for row in read(RAIL / "rule_based_station_outputs/gap_rule_audit.csv"):
        gaps[row["record_id"]].append(row)
    RAW = samples_for(grids, gaps)
    base["evaluate"].__globals__.update(OUT=HERE, classify=classify)
    print("Initial phase:", base["evaluate"](grids, gaps,.10,1.0,False,True))
    ledger = read(HERE / "gaps.csv")
    offsets = start_offsets(grids, ledger)
    write("start_offsets.csv", offsets)
    old = {}
    targets = set()
    write("four_cases.csv", [dict(old_kind=old[g["record"],g["left"],g["right"]]["kind"],
                                 old_role=old[g["record"],g["left"],g["right"]]["role"], **g)
                             for g in ledger if (g["record"],g["left"]) in targets])
    seconds = read(HERE / "seconds.csv")
    for row in seconds:
        if row["original_rows"] == "0":
            row["speed_mps"] = row["cumulative_estimate_m"] = ""
    write("seconds.csv", seconds)
    verify["main"].__globals__.update(HERE=HERE, INPUT=HERE)
    # The old verifier asserts 108 candidates. This experiment supplies its own count check.
    source = (EXP/"validated_stop_distances/run.py").read_text(encoding="utf-8")
    source = source.replace("assert len(circle_audit)==108", "assert len(circle_audit)==len(read(INPUT / 'candidate_circles.csv'))")
    scope = {"__file__":str(HERE/"run.py"),"__name__":"validator"}
    exec(compile(source,"validated_stop_distances/run.py","exec"),scope)
    scope["INPUT"] = HERE
    scope["main"]()
    legs = read(HERE/"segment_validation.csv")
    lookup = {r["segment"]:r for r in legs}
    groups = defaultdict(list)
    for leg in legs:
        groups[leg["record"]].append(leg)
    boundaries = {(g["record"],g["left"],g["right"]):g for g in ledger}
    def compatible(a,b):
        g = boundaries.get((a["record"],a["end"],b["start"]),{})
        return g.get("kind")=="terminal_candidate" and any(
            x and y and x!=y for x in a["reference_matches"].split("|")
            for y in b["reference_matches"].split("|"))
    pairs = []
    labels = {}
    for record, batch in groups.items():
        phases = [list(zip(batch[i::2],batch[i+1::2])) for i in (0,1)]
        chosen = max(phases,key=lambda p:(sum(compatible(a,b) for a,b in p),len(p)))
        for i,(a,b) in enumerate(chosen,1):
            circle = f"{record}_C{i:03}"
            labels[a["segment"]] = labels[b["segment"]] = circle
            pairs.append(dict(record=record,circle=circle,outbound=a["segment"],returning=b["segment"],
                              confidence="time_order_candidate"))
    write("candidate_circles.csv",pairs)
    paired_boundaries = {(p["record"],lookup[p["outbound"]]["end"],lookup[p["returning"]]["start"]):p["circle"] for p in pairs}
    waits = {}
    for g in ledger:
        if g["kind"] != "terminal_candidate":
            continue
        circle = paired_boundaries.get((g["record"],g["left"],g["right"]),"")
        g["role"] = "within_circle_turnaround" if circle else "terminal_role_pending"
        if not circle:
            left = next((r for r in groups[g["record"]] if r["end"] == g["left"]), {})
            right = next((r for r in groups[g["record"]] if r["start"] == g["right"]), {})
            left_circle = labels.get(left.get("segment"), "")
            right_circle = labels.get(right.get("segment"), "")
            if left_circle and right_circle and left_circle != right_circle:
                g["role"] = "between_candidate_circles"
            elif left_circle and not right_circle:
                g["role"] = "possible_rest_before_incomplete_circle"
        for t in range(int(g["left"])+1,int(g["right"])):
            waits[g["record"],str(t)] = circle, g["subtype"], g["role"]
    for row in seconds:
        row["circle"] = labels.get(row["segment"],"")
        if (hold := waits.get((row["record"],row["original_second"]))) is not None:
            row["circle"], subtype, role = hold
            row["status"] = subtype if subtype=="restart_unobserved" else role
    write("gaps.csv", ledger)
    write("four_cases.csv", [dict(old_kind=old[g["record"],g["left"],g["right"]]["kind"],
                                 old_role=old[g["record"],g["left"],g["right"]]["role"], **g)
                             for g in ledger if (g["record"],g["left"]) in targets])
    for leg in legs:
        leg["circle"] = labels.get(leg["segment"],"")
    write("segments.csv", [{k:r[k] for k in ("record","segment","start","end","circle")} for r in legs])
    write("seconds.csv",seconds)
    record_boundaries = []
    records = list(grids)
    for left,right in zip(records, records[1:]):
        a,b = grids[left][max(grids[left])],grids[right][min(grids[right])]
        dt = int(b["time_of_day_s"])-int(a["time_of_day_s"])
        same_file = a["file"]==b["file"]
        record_boundaries.append(dict(left_record=left,right_record=right,same_file=same_file,
            delta_s=dt,left_link=a["head_link_last"],right_link=b["head_link_first"],
            left_speed_mps=a["speed_last_mps"],right_speed_mps=b["speed_first_mps"],
            status="possible_continuity_review" if same_file and 0<=dt<=4 and
                   a["head_link_last"]==b["head_link_first"] else "no_verified_continuity"))
    write("record_boundary_review.csv",record_boundaries)
    write("unpaired_review.csv",[dict(**r,review_status="record_edge_check_adjacent_record"
                                       if r in (groups[r["record"]][0],groups[r["record"]][-1])
                                       else "internal_pairing_conflict")
                                 for r in legs if r["segment"] not in labels])
    print("Selected phase:",len(legs),"segments,",len(pairs),"candidate circles",
          Counter(g["kind"] for g in ledger))
    scope["main"]()


if __name__=="__main__":
    main()
