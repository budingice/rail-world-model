import csv
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent
RAIL = OUT.parents[1] / "rail_processing"


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write(name, rows):
    if rows:
        with (OUT / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def number(value):
    return float(value) if value else None


def context(grid, edge, step):
    rows = []
    for offset in range(5):
        row = grid.get(edge + step * offset)
        if not row or not int(row["n_raw"]):
            break
        rows.append(row)
    return rows


def stable(rows, column, tolerance):
    values = [r[column] for r in rows]
    if (tolerance and len(values) >= 3 and values[0] != values[1]
            and values[1] == values[2] and values[1]):
        values = values[1:]
    return values[0] if len(values) >= 2 and values[0] and values[0] == values[1] else ""


def classify(g, grid, low, running, relaxed):
    left, right = int(g["left_second"]), int(g["right_second"])
    before, after = context(grid, left, -1), context(grid, right, 1)
    vl, vr = number(g["left_speed_mps"]), number(g["right_speed_mps"])
    quiet = all(v is not None and v <= low for v in (vl, vr))
    links = [stable(rows, "head_link_last", False) for rows in (before, after)]
    # Avoid treating a stable state change across the gap as an isolated spike.
    edge_links = [g["left_link"], g["right_link"]]
    links = [value or (edge_links[i] if relaxed else "") for i, value in enumerate(links)]
    same = bool(all(links) and links[0] == links[1])
    changed = bool(all(links) and links[0] != links[1])
    support = []
    for rows, brake, traction in ((before, 1, 0), (after, 0, 1)):
        support.append(any(number(r["brake_last_observed"]) == brake and
                           number(r["traction_last_observed"]) == traction for r in rows[:2]))
    departure = all(support) and (relaxed or
                 (g["brake_release"] == g["traction_on"] == "True"))
    dt = right-left
    if dt <= 4 and not changed and vl is not None and vr is not None and abs(vr-vl) <= 1:
        kind = "short_gap"
    elif all(v is not None and v > running for v in (vl, vr)):
        kind = "moving_gap"
    elif quiet and departure and same and 30 <= dt <= 119:
        kind = "intermediate_stop"
    elif quiet and departure and changed and dt > 120:
        kind = "terminal_candidate"
    else:
        kind = "review"
    return dict(kind=kind, left=left, right=right, left_mps=vl, right_mps=vr,
                stable_left_link=links[0], stable_right_link=links[1],
                arrival_state_support=support[0], departure_state_support=support[1])


def evaluate(grids, gaps, low, running, relaxed, export=False):
    seconds, ledger, segments, pairs = [], [], [], []
    total = covered = usable = paired = 0
    segment_count = pair_count = 0
    for record, grid in grids.items():
        decisions = [classify(g, grid, low, running, relaxed) for g in gaps[record]]
        cuts = [d for d in decisions if d["kind"] == "terminal_candidate"]
        cuts.sort(key=lambda d: d["left"])
        bounds = list(zip([min(grid)] + [d["right"] for d in cuts],
                          [d["left"] for d in cuts] + [max(grid)]))
        ids = [f"{record}_R{i:03}" for i in range(1, len(bounds)+1)]
        pair_count += len(bounds)//2
        segment_count += len(bounds)
        roles = {}
        for i, d in enumerate(cuts):
            roles[d["left"]] = ("within_circle_turnaround" if i%2 == 0 and i+1<len(ids)
                                else "between_candidate_circles" if i+2<len(ids)
                                else "terminal_unpaired")
        marks = {}
        bridges = {}
        for d in decisions:
            l, r = d["left"], d["right"]
            kind = roles.get(l, d["kind"])
            for t in range(l+1, r):
                marks[t] = (kind, d)
            if d["kind"] == "moving_gap":
                bridges[r] = d
            if export:
                ledger.append(dict(record=record, **d, role=kind))
        total += sum(int(row["n_raw"]) for row in grid.values())
        for i, ((start, end), segment) in enumerate(zip(bounds, ids)):
            circle = f"{record}_C{i//2+1:03}" if i < 2*(len(ids)//2) else ""
            if export:
                segments.append(dict(record=record, segment=segment, start=start, end=end, circle=circle))
                if circle and i%2==0:
                    pairs.append(dict(record=record, circle=circle, outbound=segment, returning=ids[i+1],
                                      confidence="time_order_candidate"))
            position = 0.0
            previous = None
            for t in range(start, end+1):
                row = grid[t]
                n = int(row["n_raw"])
                covered += n
                paired += n if circle else 0
                speed = number(row["speed_sample_mean"])
                kind, d = marks.get(t, ("observed" if n else "review", None))
                if not n and kind == "short_gap":
                    speed = d["left_mps"] + (d["right_mps"]-d["left_mps"])*(t-d["left"])/(d["right"]-d["left"])
                if not n and kind == "intermediate_stop":
                    speed = 0.0
                if speed is not None:
                    usable += n
                    if previous and position is not None:
                        last, v = previous
                        bridge = bridges.get(t)
                        if t == last+1:
                            position += (v+speed)/2
                        elif bridge and bridge["left"] == last:
                            position += (bridge["left_mps"]+bridge["right_mps"])*(t-last)/2
                        else:
                            position = None
                    elif t != start:
                        position = None
                    previous = t, speed
                if export:
                    seconds.append(dict(record=record, original_second=t, segment=segment,
                                        circle=circle, elapsed_s=t-start, original_rows=n,
                                        status=kind, speed_mps=speed,
                                        cumulative_estimate_m=position if speed is not None else None))
        if export:
            for d in cuts:
                for t in range(d["left"]+1, d["right"]):
                    seconds.append(dict(record=record, original_second=t, segment="",
                                        circle=f"{record}_C{cuts.index(d)//2+1:03}"
                                        if roles[d["left"]]=="within_circle_turnaround" else "",
                                        elapsed_s="", original_rows=0, status=roles[d["left"]],
                                        speed_mps=0, cumulative_estimate_m=""))
    if export:
        seconds.sort(key=lambda r: (r["record"], r["original_second"]))
        assert len({(r["record"], r["original_second"]) for r in seconds}) == len(seconds)
        write("seconds.csv", seconds)
        write("gaps.csv", ledger)
        write("segments.csv", segments)
        write("candidate_circles.csv", pairs)
    return dict(low_mps=low, moving_mps=running, allow_endpoint_link_fallback=relaxed,
                segments=segment_count, candidate_circles=pair_count, raw_rows=total,
                segment_raw_rows=covered, paired_raw_rows=paired, speed_raw_rows=usable,
                segment_pct=round(100*covered/total, 2), paired_pct=round(100*paired/total, 2),
                speed_pct=round(100*usable/total, 2))


def main():
    grids, gaps = defaultdict(dict), defaultdict(list)
    for row in read(RAIL / "outputs/seconds.csv"):
        grids[row["session_id"]][int(row["time_of_day_s"])] = row
    for row in read(RAIL / "rule_based_station_outputs/gap_rule_audit.csv"):
        gaps[row["record_id"]].append(row)
    results = []
    for low, moving in ((0.05, 1.5), (0.10, 1.0), (0.20, 0.5)):
        for relaxed in (False, True):
            result = evaluate(grids, gaps, low, moving, relaxed, (low, relaxed)==(0.10, False))
            results.append(result)
            print(result)
    write("sensitivity.csv", results)


if __name__ == "__main__":
    main()
