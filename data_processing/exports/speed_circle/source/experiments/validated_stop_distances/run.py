"""Validate time-paired runs and recompute distances between retained stop anchors."""
import csv
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
INPUT = EXP / "robust_gap_matching"


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write(name, rows):
    with (HERE / name).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def interval(rows, start, end, gaps):
    """Return positions at observed seconds, with bridges applied only at their right edge."""
    positions = {start: 0.0}
    previous = start
    for t in range(start+1, end+1):
        if not int(rows[t]["original_rows"]):
            continue
        if not rows[t]["speed_mps"]:
            return {}
        if t == previous+1:
            travel = (float(rows[previous]["speed_mps"])+float(rows[t]["speed_mps"]))/2
        else:
            gap = gaps.get((previous, t))
            if not gap or gap["kind"] not in ("moving_gap", "short_gap"):
                return {}
            travel = (float(gap["left_mps"])+float(gap["right_mps"]))*(t-previous)/2
        positions[t] = positions[previous]+travel
        previous = t
    return positions if end in positions else {}


def main():
    by_record = defaultdict(dict)
    all_seconds = read(INPUT / "seconds.csv")
    for row in all_seconds:
        by_record[row["record"]][int(row["original_second"])] = row
        row.update(prev_stop_distance_m="", next_stop_distance_m="", distance_scope="")
    gaps_by_record = defaultdict(dict)
    for gap in read(INPUT / "gaps.csv"):
        gaps_by_record[gap["record"]][int(gap["left"]), int(gap["right"])] = gap
    references = defaultdict(list)
    for r in read(EXP / "speed_distance_compact/directional_station_distances.csv"):
        references[r["direction"]].append(float(r["distance_m"]))
    leg_results = {}
    local_rows, template_rows = set(), set()
    interval_audit = []
    for leg in read(INPUT / "segments.csv"):
        record, segment = leg["record"], leg["segment"]
        rows = by_record[record]
        gaps = gaps_by_record[record]
        start, end = int(leg["start"]), int(leg["end"])
        internal = sorted((l, r) for (l, r), g in gaps.items()
                          if start < l < r < end and g["kind"] == "intermediate_stop")
        before = next((g for (l, r), g in gaps.items() if r == start and g["kind"] == "terminal_candidate"), None)
        after = next((g for (l, r), g in gaps.items() if l == end and g["kind"] == "terminal_candidate"), None)
        start_ok = bool(before) or bool(rows[start]["speed_mps"] and float(rows[start]["speed_mps"]) <= .10)
        end_ok = bool(after) or bool(rows[end]["speed_mps"] and float(rows[end]["speed_mps"]) <= .10)
        stops = ([(start, start)] if start_ok else []) + internal + ([(end, end)] if end_ok else [])
        measured = []
        positions_by_interval = []
        for (_, departure), (arrival, _) in zip(stops, stops[1:]):
            positions = interval(rows, departure, arrival, gaps)
            measured.append(positions.get(arrival))
            positions_by_interval.append((departure, arrival, positions))
        compatible = [direction for direction, ref in references.items()
                      if len(internal)==2 and start_ok and end_ok and len(measured)==3
                      and all(d is not None and abs(d-target)<=max(20, .05*target)
                              for d,target in zip(measured, ref))]
        leg_results[segment] = dict(**leg, internal_stops=len(internal), start_supported=start_ok,
                                    end_supported=end_ok, reference_matches="|".join(compatible),
                                    distance_1_m=measured[0] if measured else None,
                                    distance_2_m=measured[1] if len(measured)>1 else None,
                                    distance_3_m=measured[2] if len(measured)>2 else None)
        for departure, arrival, positions in positions_by_interval:
            interval_audit.append(dict(segment=segment, departure=departure, arrival=arrival,
                                       distance_m=positions.get(arrival), usable=bool(positions)))
            if not positions:
                continue
            for t, position in positions.items():
                row = rows[t]
                if not int(row["original_rows"]):
                    continue
                # Arrival uses the stop just reached. The following interval may supply its next stop.
                if t == arrival:
                    continue
                row.update(prev_stop_distance_m=position, next_stop_distance_m=positions[arrival]-position,
                           distance_scope="template_compatible" if compatible else "local_anonymous")
                local_rows.add((record, t))
                if compatible:
                    template_rows.add((record, t))
    circle_audit = []
    passed = set()
    for pair in read(INPUT / "candidate_circles.csv"):
        out, back = (leg_results[pair[k]] for k in ("outbound", "returning"))
        boundary = gaps_by_record[pair["record"]].get((int(out["end"]), int(back["start"])), {})
        terminal = boundary.get("kind") == "terminal_candidate"
        compatible = any(a != b for a in out["reference_matches"].split("|") if a
                         for b in back["reference_matches"].split("|") if b)
        ok = terminal and compatible
        if ok:
            passed.add(pair["circle"])
        circle_audit.append(dict(**pair, terminal_evidence=terminal,
                                 outbound_matches=out["reference_matches"],
                                 return_matches=back["reference_matches"], verified_candidate=ok,
                                 direction_resolution="ambiguous" if compatible and
                                 "|" in out["reference_matches"] and "|" in back["reference_matches"]
                                 else "conditional" if compatible else "unresolved"))
    assert len(circle_audit)==108
    assert all(not r["prev_stop_distance_m"] and not r["next_stop_distance_m"]
               for r in all_seconds if not int(r["original_rows"]))
    write("seconds_with_stop_distances.csv", all_seconds)
    write("segment_validation.csv", list(leg_results.values()))
    write("circle_validation.csv", circle_audit)
    write("interval_validation.csv", interval_audit)
    total = sum(int(r["original_rows"]) for r in all_seconds)
    print("Raw rows", total, "validated circles", len(passed),
          "geometry compatible legs", sum(bool(r["reference_matches"]) for r in leg_results.values()))
    print("Legs with any complete distance pair", len({r["segment"] for r in all_seconds if r["distance_scope"]}))
    for label, keys in (("local or template", local_rows), ("template compatible", template_rows),
                        ("verified circles", {k for k in local_rows if by_record[k[0]][k[1]]["circle"] in passed})):
        n = sum(int(by_record[record][t]["original_rows"]) for record,t in keys)
        print(label, n, round(100*n/total, 4))


if __name__ == "__main__":
    main()
