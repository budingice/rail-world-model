"""Read-only source processing. See config.json and outputs/report.md.

No synthetic subsecond timestamps, calendar year, station identity or gap distance.
SciPy is optional: NumPy polynomial kernels implement the documented fallback.
"""
from pathlib import Path
import argparse, hashlib, html, json, sys, platform
import numpy as np
import pandas as pd
try:
    from scipy.signal import savgol_filter
    from scipy.integrate import cumulative_trapezoid
    SCIPY = True
except ImportError:
    SCIPY = False

BASE = Path(__file__).resolve().parent

def dump_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

def csv(path, data, columns=None):
    if not isinstance(data, pd.DataFrame):
        data = pd.DataFrame(data, columns=columns)
    data.to_csv(path, index=False, encoding='utf-8-sig', float_format='%.7f')

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()

def cumtrap(y, x):
    if SCIPY: return cumulative_trapezoid(y, x, initial=0)
    return np.r_[0., np.cumsum((y[:-1]+y[1:])*np.diff(x)/2)]

def derivatives(y, window=7, order=2):
    """Centered offline polynomial and trailing causal polynomial. No edge extrapolation."""
    n=len(y); raw=np.full(n,np.nan); offline=raw.copy(); causal=raw.copy()
    if n>1: raw[1:]=np.diff(y)
    if n>=window:
        h=window//2
        if SCIPY:
            offline=savgol_filter(y,window,order,deriv=1,delta=1.)
            offline[:h]=np.nan; offline[-h:]=np.nan
        else:
            weights=np.linalg.pinv(np.vander(np.arange(-h,h+1,dtype=float),order+1,increasing=True))[1]
            offline[h:n-h]=np.lib.stride_tricks.sliding_window_view(y,window)@weights
        weights=np.linalg.pinv(np.vander(np.arange(-(window-1),1,dtype=float),order+1,increasing=True))[1]
        causal[window-1:]=np.lib.stride_tricks.sliding_window_view(y,window)@weights
    return raw,offline,causal

def stop_runs(mean, enter, exit_):
    active=False; start=None; runs=[]
    for i,v in enumerate(mean):
        if not active and v<=enter: active=True; start=i
        elif active and v>exit_: runs.append((start,i-1)); active=False
    if active: runs.append((start,len(mean)-1))
    return runs

def svg_plot(path, series, title, xlabel='Observed time (seconds, session-relative)'):
    """Standalone SVG; NaN breaks lines. Each series has independent labeled panel."""
    width=1150; panel=190; height=65+panel*len(series)
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<text x="65" y="25" font-family="sans-serif" font-size="18">{html.escape(title)}</text>']
    for j,(label,x,y) in enumerate(series):
        x=np.asarray(x,dtype=float); y=np.asarray(y,dtype=float)
        good=np.isfinite(x)&np.isfinite(y); top=55+j*panel; lo=float(np.nanmin(y)) if good.any() else 0.; hi=float(np.nanmax(y)) if good.any() else 1.
        if hi<=lo: hi=lo+1
        xmin=float(np.nanmin(x)) if len(x) else 0.; xmax=float(np.nanmax(x)) if len(x) else 1.
        if xmax<=xmin:xmax=xmin+1
        parts.append(f'<text x="65" y="{top-8}" font-family="sans-serif" font-size="13">{html.escape(label)}; range {lo:.3f} to {hi:.3f}</text>')
        parts.append(f'<path d="M65 {top} V{top+130} H1100" fill="none" stroke="#888"/>')
        chunks=[]; chunk=[]
        for a,b,ok in zip(x,y,good):
            if ok: chunk.append((65+(a-xmin)/(xmax-xmin)*1035,top+130-(b-lo)/(hi-lo)*125))
            elif chunk:chunks.append(chunk);chunk=[]
        if chunk: chunks.append(chunk)
        for chunk in chunks:
            # preserve min/max within local buckets for very long plots
            if len(chunk)>6000:
                keep=[]
                for k in range(0,len(chunk),max(1,len(chunk)//2500)):
                    block=chunk[k:k+max(1,len(chunk)//2500)]; ids=sorted(set([0,len(block)-1,int(np.argmin([v[1] for v in block])),int(np.argmax([v[1] for v in block]))]));keep.extend(block[q] for q in ids)
                chunk=keep
            points=' '.join(f'{a:.2f},{b:.2f}' for a,b in chunk)
            parts.append(f'<polyline points="{points}" fill="none" stroke="#176899" stroke-width="1.2"/>')
        parts.append(f'<text x="65" y="{top+150}" font-family="sans-serif" font-size="11">{xmin:.0f}</text><text x="1050" y="{top+150}" font-family="sans-serif" font-size="11">{xmax:.0f}</text>')
    parts.append(f'<text x="65" y="{height-8}" font-family="sans-serif" font-size="12">{html.escape(xlabel)}</text></svg>')
    path.write_text('\n'.join(parts),encoding='utf-8')

def run(config_path):
    cfg=json.loads(config_path.read_text(encoding='utf-8')); p=cfg['candidate_parameters']; m=cfg['mapping']
    out=BASE/cfg['output_dir']; out.mkdir(parents=True,exist_ok=True); (out/'plots').mkdir(exist_ok=True)
    files=sorted(Path(cfg['input_dir']).glob(cfg['pattern'])); assert files,'No input files'
    profiles=[]; fields=[]; gaps=[]; boundaries=[]; duplicates=[]; seconds_all=[]; events=[]; segments=[]; links=[]; sensitivities=[]; manifests=[]; intervals=[]; joins=[]; exceptions=[]
    readme=Path(cfg['input_dir'])/'readme.txt'
    if readme.exists(): manifests.append({'file':str(readme),'bytes':readme.stat().st_size,'sha256':sha(readme)})
    for f in files:
        print('Reading',f.name,flush=True)
        before=sha(f); manifests.append({'file':str(f),'bytes':f.stat().st_size,'sha256':before})
        xl=pd.ExcelFile(f)
        assert len(xl.sheet_names)==1,'Unexpected multiple worksheets; inspect before continuing'
        d=pd.read_excel(xl); d.columns=d.columns.map(str); d['excel_row']=np.arange(len(d))+2
        assert all(c in d for c in m.values())
        payload=list(m.values()); t=pd.to_timedelta(d[m['time']].astype(str),errors='coerce').dt.total_seconds()
        assert t.notna().all(),'Invalid timestamp: requires dedicated quarantine handling'
        assert t.between(0,86399).all(),'Unexpected time range'
        d['second']=t.astype(int)
        nums=[m[k] for k in ['head_link','speed','speed_limit','traction','brake','ato','target_speed','gradient']]
        for c in nums:d[c]=pd.to_numeric(d[c],errors='coerce')
        delta=t.diff(); reset=d['Unnamed: 0'].diff().lt(0) if 'Unnamed: 0' in d else pd.Series(False,index=d.index)
        rollback=delta.lt(0)
        d['session_no']=(reset|rollback).cumsum()+1
        for i in np.flatnonzero((reset|rollback).to_numpy()):
            boundaries.append({'file':f.name,'excel_row':int(d.at[i,'excel_row']),'previous_time':d.at[i-1,m['time']],'time':d.at[i,m['time']],'row_index_reset':bool(reset.iloc[i]),'time_rollback':bool(rollback.iloc[i]),'policy':'split_before_sort_no_midnight_assumption'})
        dup=d.duplicated(payload,keep=False)
        if dup.any():
            z=d.loc[dup,['excel_row','session_no']+payload].copy();z.insert(0,'file',f.name);duplicates.append(z)
        # Rows sharing an integer second may be legitimate distinct 5Hz samples. Never deduplicate them.
        for c in payload:
            v=d[c]; numeric=pd.to_numeric(v,errors='coerce')
            fields.append({'file':f.name,'column':c,'meaning':next(k for k,vv in m.items() if vv==c),'dtype':str(v.dtype),'missing':int(v.isna().sum()),'unique':int(v.nunique()),'min':numeric.min(),'median':numeric.median(),'max':numeric.max(),'first_value':str(v.iloc[0])})
        for val,count in delta.value_counts(dropna=False).items(): intervals.append({'file':f.name,'recorded_delta_s':val,'count':int(count),'interpretation':'timestamp_quantized_not_actual_sample_interval'})
        sp=d[m['speed']]*cfg['speed_to_mps']; invalid=sp.isna()|~sp.between(0,p['speed_max_mps']); bad_ato=~d[m['ato']].between(p['ato_min_percent'],p['ato_max_percent']); bad_state=~d[m['traction']].isin([0,1])|~d[m['brake']].isin([0,1])
        for mask,reason in [(invalid,'speed_invalid'),(bad_ato,'ato_out_of_range'),(bad_state,'state_out_of_range')]:
            if mask.any():
                z=d.loc[mask,['excel_row','session_no']+payload].copy();z.insert(0,'reason',reason);z.insert(0,'file',f.name);exceptions.append(z)
        d['speed_mps']=sp.mask(invalid);d['ato_valid']=d[m['ato']].mask(bad_ato)
        profile={'file':f.name,'sheets':','.join(xl.sheet_names),'rows':len(d),'time_first':d[m['time']].iloc[0],'time_last':d[m['time']].iloc[-1],'time_min_s':int(t.min()),'time_max_s':int(t.max()),'unique_recorded_seconds':int(t.nunique()),'sessions':int(d['session_no'].nunique()),'index_resets':int(reset.sum()),'time_rollbacks':int(rollback.sum()),'same_time_adjacent_pairs':int(delta.eq(0).sum()),'duplicate_payload_rows':int(dup.sum()),'missing_cells':int(d[payload].isna().sum().sum()),'speed_invalid_rows':int(invalid.sum()),'ato_invalid_rows':int(bad_ato.sum()),'state_invalid_rows':int(bad_state.sum()),'speed_min_mps':float(sp.min()),'speed_max_mps':float(sp.max()),'timestamp_resolution':'1s','actual_interval_recoverable':False}
        for sno,session in d.groupby('session_no',sort=False):
            sid=f'{f.stem}_S{int(sno):03d}'; ss=session['second']; group=session.groupby('second',sort=True)
            grid=np.arange(int(ss.min()),int(ss.max())+1)
            sec=pd.DataFrame(index=grid);sec.index.name='time_of_day_s'
            sec['n_raw']=group.size().reindex(grid,fill_value=0)
            for key in ['speed','speed_limit','ato','target_speed','gradient']:
                c='speed_mps' if key=='speed' else 'ato_valid' if key=='ato' else m[key]
                sec[key+'_sample_mean']=group[c].mean()
                sec[key+'_min']=group[c].min();sec[key+'_max']=group[c].max()
            sec['speed_first_mps']=group['speed_mps'].first();sec['speed_last_mps']=group['speed_mps'].last();sec['n_speed_valid']=group['speed_mps'].count().reindex(grid,fill_value=0)
            sec['head_link_first']=group[m['head_link']].first();sec['head_link_last']=group[m['head_link']].last()
            sec['head_link_unique_count']=group[m['head_link']].nunique()
            sec['raw_excel_row_first']=group['excel_row'].min();sec['raw_excel_row_last']=group['excel_row'].max()
            for key in ['traction','brake','enabled']:
                c=m[key];sec[key+'_last_observed']=group[c].last(); sec[key+'_switches']=group[c].agg(lambda x:int(x.ne(x.shift()).sum()-1))
                if key!='enabled':sec[key+'_sample_fraction']=group[c].mean()
            sec['traction_brake_both_sample_fraction']=session.assign(both=(session[m['traction']].eq(1)&session[m['brake']].eq(1)).astype(float)).groupby('second')['both'].mean()
            sec['ato_outlier_count']=bad_ato.loc[session.index].groupby(session['second']).sum()
            sec['speed_invalid_count']=invalid.loc[session.index].groupby(session['second']).sum()
            sec['file']=f.name;sec['session_id']=sid
            sec['timestamp_label']=[f'{int(x)//3600:02d}:{int(x)//60%60:02d}:{int(x)%60:02d}' for x in grid]
            sec['date_label']=f.stem.removeprefix('all_');sec['year']=np.nan
            sec['gap_flag']=sec.n_raw.eq(0);sec['count_below_nominal_flag']=sec.n_raw.between(1,p['nominal_samples_per_second_for_count_flag_only']-1)
            sec['coverage_ratio']=np.nan;sec['valid_coverage_s']=np.nan;sec['speed_time_weighted_mean_mps']=np.nan
            sec['coverage_status']='unknown_subsecond_times_absent';sec['imputed_flag']=False
            sec['key_speed_missing_flag']=sec.n_speed_valid.lt(sec.n_raw)
            good=sec.n_raw.gt(0)&sec.n_speed_valid.eq(sec.n_raw)
            run_id=(good.ne(good.shift())|~good).cumsum()
            sec['segment_id']=pd.Series(index=grid,dtype='str')
            for c in ['segment_distance_proxy_m','acceleration_raw_mps2','acceleration_filtered_offline_mps2','acceleration_filtered_causal_mps2','distance_from_prev_stop_candidate_proxy_m','distance_to_next_stop_candidate_proxy_m']:sec[c]=np.nan
            sec['stop_state_offline']='unknown';sec['stop_confirmed_causal']=False
            sec['acceleration_boundary_flag']=True;sec['position_quality']='unlocated';sec['stop_candidate_id']=pd.Series(index=grid,dtype='str')
            valid_seq=0
            for _,block in sec[good].groupby(run_id[good]):
                valid_seq+=1;segid=f'{sid}_G{valid_seq:03d}';ix=block.index.to_numpy();y=block.speed_sample_mean.to_numpy();x=ix.astype(float)+.5
                dist=cumtrap(y,x);a,af,ac=derivatives(y,p['smoothing_window_s'],p['smoothing_polyorder'])
                sec.loc[ix,'segment_id']=segid;sec.loc[ix,'segment_distance_proxy_m']=dist
                sec.loc[ix,'acceleration_raw_mps2']=a;sec.loc[ix,'acceleration_filtered_offline_mps2']=af;sec.loc[ix,'acceleration_filtered_causal_mps2']=ac
                sec.loc[ix,'acceleration_boundary_flag']=~np.isfinite(af)
                sec.loc[ix,'position_quality']='relative_bin_mean_proxy_no_anchor';sec.loc[ix,'stop_state_offline']='running'
                runs=stop_runs(y,p['stop_enter_mps'],p['stop_exit_mps']);anchors=[]
                for a0,b0 in runs:
                    eid=f'{segid}_P{len(anchors)+1:02d}';anchors.append((a0,b0,eid));duration=b0-a0+1;left=a0==0;right=b0==len(y)-1
                    state='sustained_low_speed_candidate' if duration>=p['stop_min_duration_s'] else 'brief_low_speed_candidate'
                    sec.loc[ix[a0:b0+1],'stop_state_offline']=state;sec.loc[ix[a0:b0+1],'stop_candidate_id']=eid
                    if duration>=p['stop_min_duration_s']:sec.loc[ix[a0+p['stop_min_duration_s']-1:b0+1],'stop_confirmed_causal']=True
                    events.append({'event_id':eid,'file':f.name,'session_id':sid,'segment_id':segid,'start_second':int(ix[a0]),'end_second':int(ix[b0]),'arrival_second_proxy':int(ix[a0]),'departure_second_proxy':int(ix[b0]+1) if not right else np.nan,'observed_low_speed_bins':duration,'elapsed_bin_centers_s':b0-a0,'dwell_duration_s':np.nan,'classification':state,'confidence':'low_no_door_or_station_anchor','left_truncated':left,'right_truncated':right,'head_link_start':sec.at[ix[a0],'head_link_first'],'head_link_end':sec.at[ix[b0],'head_link_last'],'station_id':None,'uses_future_data':True})
                # These are distances to observed low-speed episodes, never station distances.
                for (a0,b0,e0),(a1,b1,e1) in zip(anchors[:-1],anchors[1:]):
                    if a1<=b0:continue
                    ii=ix[b0:a1+1]; sec.loc[ii,'distance_from_prev_stop_candidate_proxy_m']=dist[b0:a1+1]-dist[b0];sec.loc[ii,'distance_to_next_stop_candidate_proxy_m']=dist[a1]-dist[b0:a1+1]
                    joins.append({'segment_id':segid,'prev_candidate':e0,'next_candidate':e1,'distance_proxy_m':dist[a1]-dist[b0],'status':'anonymous_low_speed_anchors_not_stations'})
                complete=bool(y[0]<=p['stop_exit_mps'] and y[-1]<=p['stop_exit_mps'])
                segments.append({'segment_id':segid,'file':f.name,'session_id':sid,'start_second':int(ix[0]),'end_second':int(ix[-1]),'seconds':len(ix),'distance_proxy_m':float(dist[-1]),'start_speed_mps':float(y[0]),'end_speed_mps':float(y[-1]),'start_link':block.head_link_first.iloc[0],'end_link':block.head_link_last.iloc[-1],'low_speed_both_ends':complete,'distance_zero_order_proxy_m':float(y[:-1].sum()),'quadrature_sensitivity_m':float(dist[-1]-y[:-1].sum()),'station_interval_status':'unconfirmed'})
                for enter in [0.0277777778,p['stop_enter_mps'],0.138888889]:
                    for duration in [2,3,5]:
                        rr=stop_runs(y,enter,max(p['stop_exit_mps'],enter));sensitivities.append({'segment_id':segid,'enter_mps':enter,'min_bins':duration,'candidate_count':sum(b-a+1>=duration for a,b in rr)})
            # observed gaps, including zero-to-zero gaps; no hidden motion assumptions
            unique=group.size().index.to_numpy()
            for prev,nxt in zip(unique[:-1],unique[1:]):
                if nxt-prev<=1:continue
                before=sec.at[prev,'speed_last_mps'];after=sec.at[nxt,'speed_first_mps']
                gaps.append({'file':f.name,'session_id':sid,'last_observed_second':int(prev),'next_observed_second':int(nxt),'recorded_gap_s':int(nxt-prev),'missing_whole_seconds':int(nxt-prev-1),'last_speed_mps':before,'next_speed_mps':after,'last_link':sec.at[prev,'head_link_last'],'next_link':sec.at[nxt,'head_link_first'],'low_speed_both_edges':bool(np.isfinite(before) and np.isfinite(after) and before<=p['stop_exit_mps'] and after<=p['stop_exit_mps']),'classification':'unobserved_interval_not_dwell','distance_during_gap_m':np.nan})
            for c in ['distance_raw_m','distance_corrected_m','distance_from_prev_station_m','distance_to_next_station_m','lap_id','direction','vehicle_id','current_station_id','prev_station_id','next_station_id']:sec[c]=np.nan
            sec['acceleration_review_flag']=sec.acceleration_raw_mps2.abs().gt(p['acceleration_review_mps2'])
            # causal running distance is available only after each bin closes; it still assumes bin means.
            sec['feature_available_at_second']=sec.index+1
            sec['causal_motion_feature_status']='past_and_current_bin_only_available_at_bin_end'
            sec['offline_future_feature_flag']=True
            sec=sec.reset_index(); seconds_all.append(sec)
            tr=session.loc[session[m['head_link']].ne(session[m['head_link']].shift()),['excel_row','second',m['head_link']]].copy();tr.columns=['excel_row','time_of_day_s','head_link'];tr.insert(0,'session_id',sid);tr.insert(0,'file',f.name);links.append(tr)
        profiles.append(profile)
        assert sha(f)==manifests[-1]['sha256'],'Source changed during run'
    seconds=pd.concat(seconds_all,ignore_index=True);sg=pd.DataFrame(segments);ev=pd.DataFrame(events);gap=pd.DataFrame(gaps)
    # Cross-file dates have no year. Clock overlap is not actual overlap; exact content hashes are evidence only.
    overlap=[]
    for i,a in enumerate(profiles):
        for b in profiles[i+1:]:
            overlap.append({'file_a':a['file'],'file_b':b['file'],'clock_range_overlap_s':max(0,min(a['time_max_s'],b['time_max_s'])-max(a['time_min_s'],b['time_min_s'])+1),'calendar_overlap_status':'unknown_year_filename_days_differ','same_file_hash':next(x['sha256'] for x in manifests if x['file'].endswith(a['file']))==next(x['sha256'] for x in manifests if x['file'].endswith(b['file']))})
    matches=sg.groupby(['start_link','end_link','low_speed_both_ends'],dropna=False).agg(count=('segment_id','size'),distance_median_proxy_m=('distance_proxy_m','median'),distance_min_proxy_m=('distance_proxy_m','min'),distance_max_proxy_m=('distance_proxy_m','max')).reset_index();matches['match_status']='repeated_link_endpoint_signature_only_not_station_or_lap'
    csv(out/'file_profile.csv',profiles);csv(out/'field_profile.csv',fields);csv(out/'recorded_time_deltas.csv',intervals)
    csv(out/'session_boundaries.csv',boundaries);csv(out/'file_overlap.csv',overlap)
    csv(out/'duplicate_payload_rows.csv',pd.concat(duplicates,ignore_index=True) if duplicates else pd.DataFrame(columns=['file','excel_row','session_no']+list(m.values())))
    csv(out/'anomalous_rows.csv',pd.concat(exceptions,ignore_index=True) if exceptions else pd.DataFrame(columns=['file','reason','excel_row']+list(m.values())))
    csv(out/'seconds.csv',seconds);csv(out/'gaps.csv',gap);csv(out/'stop_events.csv',ev);csv(out/'segments.csv',sg);csv(out/'station_matching.csv',matches);csv(out/'link_transitions.csv',pd.concat(links,ignore_index=True));csv(out/'stop_threshold_sensitivity.csv',sensitivities)
    csv(out/'anonymous_stop_intervals.csv',joins,columns=['segment_id','prev_candidate','next_candidate','distance_proxy_m','status'])
    causal_drop=['acceleration_filtered_offline_mps2','stop_state_offline','stop_candidate_id','distance_from_prev_stop_candidate_proxy_m','distance_to_next_stop_candidate_proxy_m','offline_future_feature_flag']
    csv(out/'seconds_causal.csv',seconds.drop(columns=causal_drop))
    dump_json(out/'config_used.json',cfg);dump_json(out/'source_manifest.json',manifests)
    checks=[]
    def check(name,ok,detail):checks.append({'check':name,'status':'PASS' if ok else 'FAIL','detail':detail})
    check('unit_conversion',np.isclose(100*cfg['speed_to_mps'],10),'100 dm/s = 10 m/s')
    check('one_row_per_session_second',not seconds.duplicated(['session_id','time_of_day_s']).any(),'Sessions never merged on clock time')
    check('source_rows_accounted',int(seconds.n_raw.sum())==sum(a['rows'] for a in profiles),'No raw row silently deleted')
    check('no_gap_integration',seconds.loc[seconds.gap_flag,'segment_distance_proxy_m'].isna().all(),'Missing seconds retain unknown distance')
    check('segment_zero_origin',seconds.dropna(subset=['segment_id']).groupby('segment_id').segment_distance_proxy_m.first().eq(0).all(),'Each segment resets to zero; no global concatenation')
    check('no_station_fabrication',seconds.distance_to_next_station_m.isna().all(),'Station distances not identifiable')
    check('no_imputation',not seconds.imputed_flag.any(),'No synthetic 0.2-second observations')
    check('coverage_unknown',seconds.coverage_ratio.isna().all(),'Counts do not identify true time coverage')
    vv=seconds.dropna(subset=['segment_id']); dd=vv.groupby('segment_id').segment_distance_proxy_m.diff()
    check('nonnegative_segment_motion',dd.dropna().ge(-1e-7).all(),'Speed magnitudes nonnegative')
    for name,detail in [('ring_wrap','No confirmed loop length, direction or absolute anchor'),('station_distance_conservation','No confirmed station map'),('station_interval_residual','No ground-truth station distances')]:checks.append({'check':name,'status':'NOT_EVALUABLE','detail':detail})
    # Independent numerical fixtures for constant speed, linear acceleration and causal prefix invariance.
    check('constant_speed_integral_fixture',np.allclose(cumtrap(np.full(6,10.),np.arange(6.)),np.arange(6.)*10),'Independent analytic solution 10 m/s')
    raw,off,ca=derivatives(2*np.arange(15.)+1)
    check('linear_acceleration_fixture',np.allclose(off[np.isfinite(off)],2) and np.allclose(ca[np.isfinite(ca)],2),'Analytic a=2 m/s2')
    prefix=derivatives(2*np.arange(10.)+1)[2]
    check('causal_prefix_invariance',np.allclose(ca[:10],prefix,equal_nan=True),'Future samples do not change trailing derivative')
    for segid,b in vv.groupby('segment_id'):
        if len(b)>1:assert np.all(np.diff(b.time_of_day_s)==1)
    check('all_segments_time_contiguous',True,'Verified every segment diff=1 second')
    csv(out/'validation.csv',checks);assert not any(c['status']=='FAIL' for c in checks)
    # Representative charts: longest session; complete-motion segment; gap edges; ATO outlier.
    plotlist=[]
    pick=seconds.session_id.value_counts().index[0];b=seconds[seconds.session_id.eq(pick)]
    svg_plot(out/'plots'/'overview.svg', [('Speed sample mean (m/s)',b.time_of_day_s,b.speed_sample_mean),('Offline acceleration estimate (m/s2)',b.time_of_day_s,b.acceleration_filtered_offline_mps2),('Segment distance proxy (m)',b.time_of_day_s,b.segment_distance_proxy_m)],pick+'; gaps are blank; distance resets per segment');plotlist.append('plots/overview.svg')
    full=sg[sg.low_speed_both_ends & sg.seconds.gt(20)]
    if len(full):
        pick=full.iloc[0].segment_id;b=seconds[seconds.segment_id.eq(pick)]
        svg_plot(out/'plots'/'motion_stop_detail.svg',[('Speed (m/s)',b.time_of_day_s,b.speed_sample_mean),('Traction / brake (0/1 sample fraction)',b.time_of_day_s,b.brake_sample_fraction),('Segment distance proxy (m)',b.time_of_day_s,b.segment_distance_proxy_m)],pick+'; endpoint low speed is not confirmed station stop');plotlist.append('plots/motion_stop_detail.svg')
    if len(gap):
        g=gap[gap.low_speed_both_edges].iloc[0] if gap.low_speed_both_edges.any() else gap.iloc[0]
        b=seconds[seconds.session_id.eq(g.session_id)&seconds.time_of_day_s.between(g.last_observed_second-15,g.next_observed_second+15)]
        svg_plot(out/'plots'/'gap_detail.svg',[('Speed (m/s); unobserved interval left blank',b.time_of_day_s,b.speed_sample_mean),('Raw samples per recorded second',b.time_of_day_s,b.n_raw)],g.session_id+'; zero-to-zero gap does not establish dwell');plotlist.append('plots/gap_detail.svg')
    chartbody=''.join(f'<h2>{html.escape(q)}</h2><object type="image/svg+xml" data="{q}" style="width:100%;max-width:1150px"></object>' for q in plotlist)
    (out/'charts.html').write_text('<!doctype html><meta charset="utf-8"><title>Rail data review</title><style>body{font:15px sans-serif;max-width:1200px;margin:24px auto}h2{font-size:16px}</style><h1>Rail data review</h1><p>Speed unit confirmed. Missing subsecond times prevent exact time weighting. Anonymous low-speed events are not stations.</p>'+chartbody,encoding='utf-8')
    obs=seconds.n_raw.gt(0); stats={'raw_rows':sum(a['rows'] for a in profiles),'files':len(files),'sessions':seconds.session_id.nunique(),'continuous_segments':len(sg),'second_rows_including_gaps':len(seconds),'observed_session_seconds':int(obs.sum()),'missing_session_seconds':int(seconds.gap_flag.sum()),'gap_events':len(gap),'zero_to_zero_gap_candidates':int(gap.low_speed_both_edges.sum()) if len(gap) else 0,'stop_candidates':len(ev),'sustained_low_speed_candidates':int(ev.classification.eq('sustained_low_speed_candidate').sum()) if len(ev) else 0,'confirmed_station_events':0,'ato_outliers':sum(a['ato_invalid_rows'] for a in profiles),'station_unlocated_ratio':1.0,'imputed_ratio':0.0,'subsecond_coverage_unknown_ratio':1.0,'scipy_used':SCIPY,'numpy_version':np.__version__,'pandas_version':pd.__version__,'python':sys.version,'platform':platform.platform()}
    dump_json(out/'summary.json',stats)
    # Cross-session/file exact duplicate seconds are diagnostics only: without vehicle/date they cannot be resolved.
    audits=[]
    for title,sel in [('longest_complete_motion',full.head(1)),('short_segment',sg.sort_values('seconds').head(1)),('largest_distance_proxy',sg.sort_values('distance_proxy_m',ascending=False).head(1))]:
        for _,r in sel.iterrows():audits.append({'case':title,'id':r.segment_id,'finding':f'{r.seconds} bins; proxy {r.distance_proxy_m:.3f} m; low-speed ends={r.low_speed_both_ends}','limitation':'station identity and true subsecond timing unavailable'})
    if len(gap):audits.append({'case':'missing_stop_interval','id':str(gap.iloc[0].session_id),'finding':gap.iloc[0].to_json(),'limitation':'No dwell or distance can be inferred inside gap'})
    audits.append({'case':'ring_crossing','id':'unknown','finding':'Not identifiable from present fields alone','limitation':'Link recurrence does not prove a completed loop'})
    csv(out/'representative_review.csv',audits)
    report=f'''# 环线运营数据处理报告

## 已完成与结果口径

已实际处理 {stats['files']} 个文件、{stats['raw_rows']:,} 条原始记录。原始文件只读，SHA-256 见 source_manifest.json。
输出按文件及采集会话分开，不把未证实车辆身份的数据拼接积分。{stats['sessions']} 个保守会话、{stats['continuous_segments']} 个连续有效秒级段。
共 {stats['observed_session_seconds']:,} 个有观测的会话秒，网格补列 {stats['missing_session_seconds']:,} 个缺失秒；各会话内共 {stats['gap_events']} 个缺口。
会话间空档与回退单列 session_boundaries.csv，不计入上述会话内部缺失秒。各会话可能有重叠时刻，因此不能将这些秒数直接解释为整车唯一运行时长。

readme.txt 为0字节。字段含义采用用户本轮明确确认：0时间、1是否使能、2车头Link、3速度、4限速、5牵引、6制动、7ATO百分比、8目标速度、9坡度。速度按0.1转为m/s；限速、目标速度保留原值，其单位尚未单独确认；坡度‰保留原值及用户未确认的符号约定。

## 时间轴：决定精度的限制

原始时间只有HH:MM:SS，无日期年份及亚秒值。同秒多行是合法候选观测，不等同于重复采样。
recorded_time_deltas.csv 中0秒、1秒间隔只是量化时间差，不能还原真实5Hz采样间隔。
秒窗口[t,t+1)，timestamp_label标记左端；speed_sample_mean是该秒有效样本算术均值，绝非时间加权均值。
位置代理对应t+0.5秒的窗口中心；秒均值代表该中心是建模近似。每段第一中心处位置为0，首末半秒不外推。
真实coverage_ratio、valid_coverage_s及时间加权速度均留空。n_raw<5只是低于名义点数标记，不是确认丢样。秒内未知缺测不可识别。
不伪造0.2秒时间戳、不推断年份、不将时间回退自动解释成跨日。按行号重置或时钟回退建立会话，任何缺失整秒或速度无效秒切断积分。

## 距离与加速度

segment_distance_proxy_m仅为基于秒均值的梯形积分代理，非精确原始时间积分；其单位是米，各段独立归零。
distance_raw_m、distance_corrected_m及真实前后站距离全部留空，未定位比例100%。缺口距离未知，插补比例0%。
distance_from_prev_stop_candidate_proxy_m与distance_to_next_stop_candidate_proxy_m仅在同段相邻低速事件之间提供匿名代理，不能用于声称距真实站点的距离。
加速度由秒均值差分估计，是平均运动学变化，非瞬时测量。离线滤波为7点二次局部多项式，边界不外推；因果滤波使用7点尾随多项式。
本次SciPy可用性：{SCIPY}。当前环境无SciPy，安装未获批准，使用NumPy梯形积分及多项式伪逆卷积核；若以后安装SciPy，同一脚本自动调用SciPy分支。
ATO超出[0,100]的 {stats['ato_outliers']} 条记录在anomalous_rows.csv保留，ATO统计时剔除异常值，速度积分不受其删除影响。
同时牵引和制动不擅自当作错误，提供占比供核查。使能字符串原样保留，不擅自修复已存在的编码乱码。

## 停车与环线识别

识别 {stats['stop_candidates']} 个低速候选，其中 {stats['sustained_low_speed_candidates']} 个满足候选持续时间阈值；确认站点事件0个。
停车进入阈值0.3km/h、退出0.8km/h、至少3个低速秒窗口，全部为候选参数；stop_threshold_sensitivity.csv给出阈值和持续时间敏感性。
事件到离时刻只是整秒代理。dwell_duration_s为空，因为采样未知且不能确认真实停站；observed_low_speed_bins不等于准确停留时长。
缺口中 {stats['zero_to_zero_gap_candidates']} 处前后边缘均低速，说明可能存在停留数据被删除，但也可能缺失了其他运行过程，不能补为停车。
车头Link是轨道拓扑线索，不是站号。link_transitions.csv保留变化序列；station_matching.csv仅统计重复Link起终点签名及段内距离分布，不把相同签名直接视作同一站间。
无法仅凭重复Link证明同一辆车、内外环方向、完整一圈或环线长度。没有车号、方向、拓扑映射或可靠锚点，不能生成真实站名、圈次和绝对位置。

## 验证与人工复核范围

validation.csv实际检查单位换算、原始行数守恒、秒键唯一、缺口不积分、连续段间隔、分段归零、非负里程及解析数值样例、因果前缀不变性。
跨圈、真实站间距离守恒、站间残差标为NOT_EVALUABLE，不伪报通过。segments.csv给出矩形与梯形积分代理差异，这是算法敏感性，不是实际定位误差。
representative_review.csv及plots包含正常连续运行到低速端点、缺口和短段案例；没有证据可确认区间停车或真实跨圈，无法人工验证这些类别。

## 预测建模与因果版本

seconds.csv包含利用未来样本的居中滤波、离线候选事件、距下一低速事件距离，均为离线特征。
seconds_causal.csv移除这些字段，仅保留当前秒结束后可用的均值、原始差分、尾随多项式导数、截至当前的段内积分及低速持续确认。
feature_available_at_second=t+1：不能在t秒开始时使用当前整秒均值。静态会话/段ID仅用于分组，不作为预测特征。
stop_confirmed_causal仅确认已观测持续低速，不确认真实停站。按日期/会话分组划分训练测试，勿随机打散相邻秒。

## 文件说明

- seconds.csv：完整秒网格、原始样本汇总、质量标记、运动学代理及离线特征。
- seconds_causal.csv：剔除未来依赖字段的版本。
- gaps.csv、session_boundaries.csv：会话内缺口与会话边界。
- stop_events.csv：匿名低速候选，标明左右截断。
- segments.csv、station_matching.csv、link_transitions.csv：连续段积分、重复拓扑签名及Link变化。
- file_profile.csv、field_profile.csv、file_overlap.csv、duplicate_payload_rows.csv、anomalous_rows.csv：体检证据；重复载荷记录不自动删除。
- config_used.json、summary.json、source_manifest.json、validation.csv：配置、汇总、源文件校验、验证。
- charts.html、plots/*.svg：可直接打开的曲线。

## 真正需要补充的信息

1. 原始采集是否保留毫秒时间，或是否有明确固定采样周期及丢样序号？若只有当前整秒时间，精确积分、覆盖率无法恢复。
2. 数据是否事先剔除了停车记录、截取了部分区间？行号重置是否对应原始文件、车次或不同车辆？
3. 车号、采集日期年份、方向字段或原始文件批次映射。数据分布相似不能证明同一车辆。
4. 车头Link的拓扑表、方向及与车站停车标的对应关系。可先只提供匿名站编号，未必需要真实站名。
5. 限速/目标速度是否也是dm/s，坡度正方向，使能乱码原始含义，以及门状态/到站标志是否可另行提供。
6. 最终用途与允许误差。当前输出可用于探索；真实站距及站间校正需要可靠站点或里程锚点。
'''
    (out/'report.md').write_text(report,encoding='utf-8')
    dictionary=[]
    for c in seconds.columns:
        dictionary.append({'column':c,'availability':'offline_future_dependent' if c in causal_drop else 'bin_end_or_static_group_metadata','note':'See report.md; proxy is not surveyed position; blank means unknown'})
    csv(out/'data_dictionary.csv',dictionary)
    print(json.dumps(stats,ensure_ascii=False,indent=2),flush=True)
    print('Outputs:',out,flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--config',type=Path,default=BASE/'config.json');args=ap.parse_args();run(args.config)
