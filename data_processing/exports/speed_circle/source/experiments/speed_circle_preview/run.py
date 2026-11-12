"""Export experimental 13-column circle files and audit prediction-window coverage."""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHAIN = ROOT / "experiments/file_chain_recheck"
TAIL = ROOT / "experiments/r050_fragment_audit"
FIELDS = ("elapsed_s", "enabled", "head_link", "speed_mps", "speed_limit_mps",
          "traction", "brake", "ato_percent", "target_speed_mps", "gradient_permille",
          "cumulative_distance_m", "next_stop_distance_m", "prev_stop_distance_m")


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def write(path, rows, fields=None):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def second(clock):
    h, m, s = map(int, clock.split(":"))
    return h*3600+m*60+s


def observed_fields(rows):
    last = rows[-1]
    def mean(column):
        samples = [(float(r[column]), int(r["n_raw"])) for r in rows if r[column]]
        return sum(v*n for v,n in samples)/sum(n for _,n in samples) if samples else None
    limit, target = mean("speed_limit_sample_mean"), mean("target_speed_sample_mean")
    return dict(enabled=last["enabled_last_observed"], head_link=last["head_link_last"],
                speed_limit_mps=limit/10 if limit is not None else None,
                target_speed_mps=target/10 if target is not None else None,
                traction=last["traction_last_observed"], brake=last["brake_last_observed"],
                ato_percent=mean("ato_sample_mean"), gradient_permille=mean("gradient_sample_mean"))


def main():
    source_by_record = {}
    for edge in read(CHAIN/"internal_boundary_audit.csv"):
        source_by_record[edge["right_record"]] = edge["chain"]
    observed = defaultdict(list)
    for row in read(ROOT/"rail_processing/outputs/seconds.csv"):
        if int(row["n_raw"]):
            chain = source_by_record.get(row["session_id"], Path(row["file"]).stem+"_F01")
            observed[chain,int(row["time_of_day_s"])].append(row)
    grid = {(r["record"],int(r["original_second"])):r for r in read(CHAIN/"seconds_with_stop_distances.csv")}
    assert all(sum(int(r["n_raw"]) for r in rows)==int(grid[key]["original_rows"])
               for key,rows in observed.items())
    assert sum(int(r["original_rows"]) for r in grid.values())==315619
    legs = {r["segment"]:r for r in read(CHAIN/"segments.csv")}
    old_tail = legs.pop("all_11_14_F01_R050")
    pairs = list(read(CHAIN/"circle_validation.csv"))
    tail_ids = []
    for fragment in read(TAIL/"fragments.csv"):
        ident = f'{old_tail["segment"]}_T{int(fragment["fragment"]):02}'
        tail_ids.append(ident)
        legs[ident] = dict(record=old_tail["record"], segment=ident, circle="",
                           start=str(second(fragment["start"])), end=str(second(fragment["end"])))
    for i in range(0,6,2):
        circle = f"all_11_14_two_stop_C{i//2+1:03}"
        a,b = tail_ids[i:i+2]
        legs[a]["circle"] = legs[b]["circle"] = circle
        pairs.append(dict(circle=circle, outbound=a, returning=b, verified_candidate="field_confirmed"))
    assert len(legs)==226 and len(pairs)==109
    assert len({p[k] for p in pairs for k in ("outbound","returning")})==218

    output = HERE/"circles"
    output.mkdir(exist_ok=True)
    index, circle_index, masks = [], [], []
    totals = Counter()
    used = set()

    def emit(ident, phase, circle, start, end, origin, route, filename, offset):
        leg = legs[ident]
        chain = leg["record"]
        previous_speed = position = None
        batch = []
        for t in range(start,end+1):
            key = chain,t
            assert key not in used
            used.add(key)
            original = observed.get(key, [])
            derived = grid[key]
            row = dict.fromkeys(FIELDS)
            row["elapsed_s"] = t-origin
            if original and phase!="turnaround":
                row.update(observed_fields(original))
                row["speed_mps"] = derived["speed_mps"]
                if route=="six_point":
                    for field in ("prev_stop_distance_m","next_stop_distance_m"):
                        row[field] = derived[field]
                    row["cumulative_distance_m"] = derived["cumulative_estimate_m"]
                else:
                    speed = float(row["speed_mps"]) if row["speed_mps"] else None
                    if t==start:
                        position=0.0
                    elif speed is not None and previous_speed is not None and position is not None:
                        position += (speed+previous_speed)/2
                    else:
                        position=None
                    row["cumulative_distance_m"]=position
                    previous_speed=speed
            elif route=="two_stop" and phase!="turnaround":
                previous_speed=position=None
            if t==start and phase!="turnaround":
                assert original and row["elapsed_s"]==0
                row["cumulative_distance_m"]=0.0
            n = int(derived["original_rows"])
            has_pair = all(row[k] not in (None,"") for k in ("prev_stop_distance_m","next_stop_distance_m"))
            totals["raw_rows_with_distance_pair"] += n if has_pair else 0
            totals["exported_raw_rows"] += n
            if circle:
                totals["circle_raw_rows"] += n
                totals["circle_raw_rows_with_distance_pair"] += n if has_pair else 0
            batch.append(row)
            masks.append(dict(output_file=filename,data_row=offset+len(batch),circle=circle,
                              segment=ident if phase!="turnaround" else "",phase=phase,
                              original_file=chain.rsplit("_F",1)[0]+".xlsx",
                              chain=chain,original_second=t,n_raw=n,
                              source_status=("confirmed_two_stop_mixed_gap" if route=="two_stop"
                                             and phase=="turnaround" else derived["status"]),
                              offline_distance_scope=derived["distance_scope"] if has_pair else ""))
        if phase!="turnaround":
            index.append(dict(circle=circle,segment=ident,route=route,phase=phase,output_file=filename,
                              first_data_row=offset+1,last_data_row=offset+len(batch),
                              first_original_second=start,last_original_second=end))
        return batch

    for pair in pairs:
        a,b = (legs[pair[k]] for k in ("outbound","returning"))
        circle = pair["circle"]
        route = "two_stop" if "two_stop" in circle else "six_point"
        filename = circle+".csv"
        start,end,restart,finish = map(int,(a["start"],a["end"],b["start"],b["end"]))
        outbound = emit(a["segment"],"outbound",circle,start,end,start,route,filename,0)
        hold = emit(a["segment"],"turnaround",circle,end+1,restart-1,start,route,filename,len(outbound))
        returning = emit(b["segment"],"return",circle,restart,finish,restart,route,filename,len(outbound)+len(hold))
        write(output/filename,outbound+hold+returning,FIELDS)
        circle_index.append(dict(circle=circle,route=route,output_file=filename,outbound=a["segment"],
                                 returning=b["segment"],validation=pair["verified_candidate"],
                                 turnaround_first_second=end+1,turnaround_last_second=restart-1,
                                 return_reset_data_row=len(outbound)+len(hold)+1,
                                 total_seconds=len(outbound)+len(hold)+len(returning)))
    unpaired = HERE/"unpaired"
    unpaired.mkdir(exist_ok=True)
    for ident,leg in legs.items():
        if leg["circle"]:
            continue
        start,end = map(int,(leg["start"],leg["end"]))
        filename=ident+".csv"
        write(unpaired/filename,emit(ident,"unpaired","",start,end,start,
              "two_stop" if ident in tail_ids else "six_point",filename,0),FIELDS)
    assert totals["exported_raw_rows"]==315619
    assert len(index)==226
    assert len([r for r in index if not r["circle"]])==8
    write(HERE/"circle_index.csv",circle_index)
    write(HERE/"segment_index.csv",index)
    write(HERE/"second_index.csv",masks)
    summary = dict(circles=len(pairs),segments=len(legs),unpaired=8,**totals)
    (HERE/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps(summary,indent=2))


if __name__=="__main__":
    main()
