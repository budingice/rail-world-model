"""Rebuild acquisition-order chains within each original file and validate pairing."""
import runpy
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
scope = runpy.run_path(str(EXP / "raw_boundary_recheck/run.py"))
runtime = scope["main"].__globals__
runtime["HERE"] = HERE
read = scope["read"]
write = scope["write"]


def merge_second(left, right):
    row = dict(left)
    row["n_raw"] = str(int(left["n_raw"])+int(right["n_raw"]))
    for field, weight in (("speed_sample_mean", "n_speed_valid"),
                          ("brake_sample_fraction", "n_raw"), ("traction_sample_fraction", "n_raw")):
        values = [(float(r[field]), int(r[weight])) for r in (left,right) if r[field]]
        row[field] = str(sum(v*n for v,n in values)/sum(n for _,n in values)) if values else ""
    row["n_speed_valid"] = str(int(left["n_speed_valid"])+int(right["n_speed_valid"]))
    for field in ("raw_excel_row_last", "speed_last_mps", "head_link_last",
                  "brake_last_observed", "traction_last_observed"):
        row[field] = right[field]
    return row


def main():
    original = defaultdict(list)
    for row in read(scope["RAIL"] / "outputs/seconds.csv"):
        original[row["session_id"]].append(row)
    raw_gaps = read(scope["RAIL"] / "rule_based_station_outputs/gap_rule_audit.csv")
    chains, id_map = defaultdict(dict), {}
    audit, new_gaps = [], []
    previous = None
    number = 0
    for record, rows in original.items():
        first, last = rows[0], rows[-1]
        source = first["file"]
        right = int(first["time_of_day_s"])
        same = previous is not None and previous["file"] == source
        left = int(previous["time_of_day_s"]) if same else None
        dt = right-left if same else None
        if not same:
            number = 1
        elif dt < 0:
            number += 1
        chain = f"{Path(source).stem}_F{number:02}"
        id_map[record] = chain
        if same:
            edge = dict(left_record=previous["session_id"],right_record=record,chain=chain,
                        left_second=left,right_second=right,delta_s=dt,
                        left_speed_mps=previous["speed_last_mps"],right_speed_mps=first["speed_first_mps"],
                        left_link=previous["head_link_last"],right_link=first["head_link_first"],
                        left_brake=previous["brake_last_observed"],right_brake=first["brake_last_observed"],
                        left_traction=previous["traction_last_observed"],right_traction=first["traction_last_observed"],
                        overlap_observed_seconds=0, overlap_equal_mean_speed_seconds=0)
            if dt < 0:
                past = {int(r["time_of_day_s"]):r for r in original[previous["session_id"]] if int(r["n_raw"])}
                common = [(past[int(r["time_of_day_s"])],r) for r in rows
                          if int(r["n_raw"]) and int(r["time_of_day_s"]) in past]
                edge["overlap_observed_seconds"] = len(common)
                edge["overlap_equal_mean_speed_seconds"] = sum(
                    bool(a["speed_sample_mean"]) and bool(b["speed_sample_mean"]) and
                    abs(float(a["speed_sample_mean"])-float(b["speed_sample_mean"]))<1e-6
                    for a,b in common)
                edge["decision"] = "rollback_isolated"
            elif dt <= 1:
                same_link = edge["left_link"] == edge["right_link"]
                speed_close = all(edge[k] for k in ("left_speed_mps","right_speed_mps")) and abs(
                    float(edge["left_speed_mps"])-float(edge["right_speed_mps"])) <= 1
                states_same = all(edge["left_"+k]==edge["right_"+k] for k in ("brake","traction"))
                edge["decision"] = "continuous_supported" if same_link and speed_close and states_same else "continuous_with_signal_change"
            else:
                edge["decision"] = "classify_with_gap_rules"
                new_gaps.append(dict(record_id=chain,left_second=str(left),right_second=str(right),
                                     left_time=previous["timestamp_label"],right_time=first["timestamp_label"],
                                     brake_release=str(float(edge["left_brake"] or -1)==1 and float(edge["right_brake"] or -1)==0),
                                     traction_on=str(float(edge["left_traction"] or -1)==0 and float(edge["right_traction"] or -1)==1),
                                     **{k:edge[k] for k in ("left_speed_mps","right_speed_mps","left_link","right_link")}))
                for t in range(left+1,right):
                    chains[chain][t] = dict(first,time_of_day_s=str(t),n_raw="0",speed_sample_mean="")
            audit.append(edge)
        for row in rows:
            t = int(row["time_of_day_s"])
            grid = chains[chain]
            grid[t] = merge_second(grid[t],row) if t in grid else row
        previous = last
    assert len(audit)==51 and len(chains)==7
    all_rows = [dict(row,session_id=chain) for chain, grid in chains.items() for _,row in sorted(grid.items())]
    assert sum(int(r["n_raw"]) for r in all_rows)==315619
    all_gaps = [dict(g,record_id=id_map[g["record_id"]]) for g in raw_gaps] + new_gaps
    all_gaps.sort(key=lambda g:(g["record_id"],int(g["left_second"])))
    def experiment_read(path):
        if path == scope["RAIL"] / "outputs/seconds.csv":
            return all_rows
        if path == scope["RAIL"] / "rule_based_station_outputs/gap_rule_audit.csv":
            return all_gaps
        return read(path)
    runtime["read"] = experiment_read
    # The historical four-case filter returns no rows for the new chain IDs.
    scope["main"]()
    decisions = {(g["record"],g["left"],g["right"]):g for g in read(HERE/"gaps.csv")}
    for edge in audit:
        if edge["decision"]=="classify_with_gap_rules":
            gap = decisions[edge["chain"],str(edge["left_second"]),str(edge["right_second"])]
            edge["decision"] = gap["kind"]
            edge["pairing_role"] = gap["role"]
        else:
            edge["pairing_role"] = ""
    write("internal_boundary_audit.csv",audit)
    write("rollback_review.csv",[r for r in audit if r["delta_s"]<0])
    legs = read(HERE/"segments.csv")
    by_chain = defaultdict(list)
    for leg in legs:
        by_chain[leg["record"]].append(leg)
    reasons = []
    for chain,batch in by_chain.items():
        siblings = [k for k in chains if k.rsplit("_F",1)[0]==chain.rsplit("_F",1)[0]]
        for i, leg in enumerate(batch):
            if leg["circle"]:
                continue
            reason = ("file_edge_other_leg_not_observed" if
                      (i==0 and chain==siblings[0]) or (i==len(batch)-1 and chain==siblings[-1])
                      else "rollback_boundary_or_pairing_phase")
            reasons.append(dict(**leg,reason=reason))
    write("unpaired_reasons.csv",reasons)
    print("Unpaired reasons:",dict(__import__("collections").Counter(r["reason"] for r in reasons)))
    print("Boundary decisions:",dict(__import__("collections").Counter(r["decision"] for r in audit)))


if __name__=="__main__":
    main()
