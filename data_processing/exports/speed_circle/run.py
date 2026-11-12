"""Reproduce the 13-column export using only this package and the five Excel files."""
import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent


def read(path):
    with path.open(encoding="utf-8-sig",newline="") as f:
        return list(csv.DictReader(f))


def raw_gap_edges(raw, destination):
    """Extract the original adjacent sample fields consumed by the boundary classifier."""
    pieces = []
    for path in sorted(raw.glob("all_11_*.xlsx")):
        data = pd.read_excel(path)
        data.columns = data.columns.map(str)
        clock = pd.to_timedelta(data["0"]).dt.total_seconds().astype(int)
        seq = pd.to_numeric(data["Unnamed: 0"])
        blocks = seq.diff().lt(0).cumsum()+1
        rows = pd.DataFrame({"delta":clock.diff(),"block":blocks,"previous_block":blocks.shift()})
        right = rows.index[rows.delta.gt(1) & rows.block.eq(rows.previous_block)]
        left = right-1
        def values(column, index):
            return data.loc[index,column].to_numpy()
        left_brake, right_brake = (pd.to_numeric(values("6", k)) for k in (left,right))
        left_traction, right_traction = (pd.to_numeric(values("5", k)) for k in (left,right))
        pieces.append(pd.DataFrame(dict(
            record_id=[f"{path.stem}_S{n:03}" for n in blocks.loc[right]],
            left_second=clock.loc[left].to_numpy(), right_second=clock.loc[right].to_numpy(),
            left_time=values("0",left),right_time=values("0",right),
            left_speed_mps=pd.to_numeric(values("3",left))*.1,
            right_speed_mps=pd.to_numeric(values("3",right))*.1,
            left_link=pd.to_numeric(values("2",left)),right_link=pd.to_numeric(values("2",right)),
            brake_release=(left_brake==1)&(right_brake==0),
            traction_on=(left_traction==0)&(right_traction==1),
        )))
    table = pd.concat(pieces,ignore_index=True)
    assert len(table)==993
    destination.parent.mkdir()
    table.to_csv(destination,index=False,encoding="utf-8-sig",float_format="%.7f")


def two_stop_fragments(output):
    """Apply the reviewed two-station split; the exporter uses only time bounds."""
    experiments = output/"experiments"
    chain = experiments/"file_chain_recheck"
    leg = next(r for r in read(chain/"segments.csv") if r["segment"]=="all_11_14_F01_R050")
    start,end = int(leg["start"]),int(leg["end"])
    cuts = [(int(g["left"]),int(g["right"])) for g in read(chain/"gaps.csv")
            if g["record"]==leg["record"] and start<=int(g["left"])<int(g["right"])<=end
            and int(g["right"])-int(g["left"])>=600]
    assert len(cuts)==6
    def clock(t):
        return f"{t//3600:02}:{t//60%60:02}:{t%60:02}"
    with (experiments/"r050_fragment_audit/fragments.csv").open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.writer(f)
        writer.writerow(("fragment","start","end"))
        for i,(a,b) in enumerate(zip([start]+[r for _,r in cuts],[l for l,_ in cuts]+[end]),1):
            writer.writerow((i,clock(a),clock(b)))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    raw,output=args.raw.resolve(),args.output.resolve()
    if output.is_relative_to(HERE.parents[2]) or len(list(raw.glob("all_11_*.xlsx")))!=5:
        parser.error("Use a new output directory outside the repository and five source files")
    shutil.copytree(HERE/"source",output)
    config=output/"rail_processing/config.json"
    cfg=json.loads(config.read_text(encoding="utf-8"))
    cfg["input_dir"]=str(raw)
    config.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")
    with (output/"run.log").open("w",encoding="utf-8") as log:
        def stage(path):
            print(path,flush=True)
            subprocess.run([sys.executable,str(output/path)],cwd=output,stdout=log,
                           stderr=subprocess.STDOUT,check=True)
        stage("rail_processing/process_rail.py")
        raw_gap_edges(raw,output/"rail_processing/rule_based_station_outputs/gap_rule_audit.csv")
        stage("experiments/file_chain_recheck/run.py")
        two_stop_fragments(output)
        stage("experiments/speed_circle_preview/run.py")
    final=output/"experiments/speed_circle_preview"
    baseline=json.loads((output/"baseline_sha256.json").read_text(encoding="utf-8"))
    files={p.relative_to(final).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
           for folder in ("circles","unpaired") for p in (final/folder).glob("*.csv")}
    differences=sorted(name for name in files.keys()|baseline.keys() if files.get(name)!=baseline.get(name))
    summary=json.loads((final/"summary.json").read_text(encoding="utf-8"))
    result=dict(**summary,matching_csv=len(files)-sum(name in files for name in differences),changed_files=differences)
    (output/"verification.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2))
    if differences:
        raise SystemExit("CSV differs from the reviewed baseline")


if __name__=="__main__":
    main()
