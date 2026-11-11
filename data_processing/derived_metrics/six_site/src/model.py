"""Offline model of six conditional stop locations."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from .io import csv, dump_json, sha
CONFIG = Path(__file__).resolve().parents[1] / 'configs/six_site.json'

def shape_coordinates(b, es, target, max_reconstruction, scale_bounds):
    y = b.speed_model_mps.to_numpy(float).copy()
    missing = ~np.isfinite(y)
    reconstructed = bool(missing.any())
    # Reconstruct bounded motion gaps only for the position shape.
    if reconstructed:
        good = np.flatnonzero(~missing)
        if not len(good) or good[0] != 0 or good[-1] != len(y) - 1:
            return (None, 'unbounded_missing_speed')
        start = None
        longest = 0
        for i, v in enumerate(np.r_[missing, False]):
            if v and start is None:
                start = i
            elif not v and start is not None:
                longest = max(longest, i - start)
                start = None
        if longest > max_reconstruction:
            return (None, 'motion_gap_exceeds_template_reconstruction_limit')
        y[missing] = np.interp(np.flatnonzero(missing), good, y[good])
    # Anchor the integrated speed profile to four direction-specific stops.
    temp = np.r_[0.0, np.cumsum((y[:-1] + y[1:]) / 2)]
    xp = []
    for j, (_, e) in enumerate(es.iterrows()):
        mask = b.time_of_day_s.between(e.start_second, e.end_second)
        value = float(np.median(temp[mask]))
        if j == 0:
            value = 0.0
        if j == 3:
            value = float(temp[-1])
        xp.append(value)
    xp = np.asarray(xp)
    if len(xp) != 4 or not np.all(np.diff(xp) > 0):
        return (None, 'nonmonotonic_or_missing_anchor')
    scales = np.diff(target) / np.diff(xp)
    if np.any((scales < scale_bounds[0]) | (scales > scale_bounds[1])):
        return (None, 'interval_scale_outside_0p85_to_1p15' if scale_bounds == [0.85, 1.15] else 'interval_scale_outside_allowed_bounds')
    progress = np.interp(temp, xp, target)
    for j, (_, e) in enumerate(es.iterrows()):
        progress[b.time_of_day_s.between(e.start_second, e.end_second)] = target[j]
    return ({'progress': progress, 'speed_shape': y, 'anchor_shape_positions': xp, 'scales': scales, 'reconstructed': reconstructed, 'reconstructed_at_second': missing}, 'mapped')

def run(input_root, output_dir, source_seconds=None):
    """Fit conditional positions using the five upstream CSV artifacts."""
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    routes = cfg['routes']
    inputs = {name: Path(input_root) / relative for name, relative in cfg.pop('inputs').items()}
    if source_seconds is not None:
        inputs['seconds'] = Path(source_seconds)
    missing = [str(path) for path in inputs.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError('Missing input CSVs: ' + ', '.join(missing))
    src = pd.read_csv(inputs['seconds'], low_memory=False)
    gaps = pd.read_csv(inputs['gaps'])
    trips = pd.read_csv(inputs['trips'])
    events = pd.read_csv(inputs['events'])
    sites = pd.read_csv(inputs['sites'])
    coords = sites.set_index('candidate_site_id').AB_position_m.to_dict()
    line_length = float(coords['C5'])
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rawcols = ['file', 'session_id', 'time_of_day_s', 'timestamp_label', 'date_label', 'n_raw', 'speed_sample_mean', 'speed_min', 'speed_max', 'speed_first_mps', 'speed_last_mps', 'head_link_first', 'head_link_last', 'traction_sample_fraction', 'brake_sample_fraction', 'ato_sample_mean', 'gradient_sample_mean', 'raw_excel_row_first', 'raw_excel_row_last']
    rawcols += [c for c in ['speed_precompleted_mps', 'ato_precompleted_percent', 'gradient_precompleted_permille', 'acceleration_precompleted_diff_mps2', 'precompletion_speed_flag', 'precompletion_ato_flag', 'precompletion_gradient_flag', 'precompletion_gap_delta_s'] if c in src]
    base = src[rawcols].copy()
    base['primary_record_id'] = base.session_id
    first = base.groupby('primary_record_id').raw_excel_row_first.transform('min')
    base['internal_sequence_first'] = base.raw_excel_row_first - first
    base['internal_sequence_last'] = base.raw_excel_row_last - first
    base = base.drop(columns=['raw_excel_row_first', 'raw_excel_row_last', 'session_id']).set_index(['primary_record_id', 'time_of_day_s'], drop=False)
    base['original_missing_flag'] = base.n_raw.eq(0)
    base['speed_model_mps'] = base.speed_precompleted_mps if 'speed_precompleted_mps' in base else base.speed_sample_mean
    base['speed_model_imputed_flag'] = False
    base['short_gap_interpolated_flag'] = False
    base['stationary_gap_assumption_flag'] = False
    base['rule_conflict_flag'] = False
    base['gap_rule'] = 'observed'
    base.loc[base.original_missing_flag, 'gap_rule'] = 'unresolved'
    for c in ['trip_id', 'trip_assumed_direction', 'assumed_direction', 'direction_phase', 'prev_stop_id', 'next_stop_id', 'current_stop_id', 'stop_identity_scope', 'position_source', 'arrival_trip_id', 'departure_trip_id']:
        base[c] = pd.Series(index=base.index, dtype='str')
    for c in ['raw_trip_distance_m', 'trip_progress_model_m', 'position_AB_m', 'distance_from_prev_stop_m', 'distance_to_next_stop_m', 'speed_shape_for_position_mps']:
        base[c] = np.nan
    base['six_site_model_mapped_flag'] = False
    base['template_motion_reconstruction_flag'] = False
    base['motion_reconstruction_at_this_second'] = False
    base['direction_hypothesis_flag'] = True
    base['common_AB_frame_assumption_flag'] = True
    base['offline_future_information_flag'] = True
    base['position_source'] = 'unlocated'
    mapping = []
    interval_checks = []
    raw_integrals = []
    mapped = {}
    local_pair_details = []
    # Apply speed rules before fitting trips. Terminal gaps are handled later.
    for _, g in gaps.iterrows():
        rid = g.record_id
        idx = base.primary_record_id.eq(rid) & base.time_of_day_s.gt(g.left_second) & base.time_of_day_s.lt(g.right_second)
        base.loc[idx, 'gap_rule'] = g.rule
        if g.rule == 'short_error_linear_speed_fill':
            lv = base.loc[(rid, int(g.left_second)), 'speed_sample_mean']
            rv = base.loc[(rid, int(g.right_second)), 'speed_sample_mean']
            n = int(idx.sum())
            base.loc[idx, 'speed_model_mps'] = np.linspace(lv, rv, n + 2)[1:-1]
            base.loc[idx, 'speed_model_imputed_flag'] = True
            base.loc[idx, 'short_gap_interpolated_flag'] = True
        elif g.rule == 'assumed_intermediate_station_stop':
            base.loc[idx, 'speed_model_mps'] = 0.0
            base.loc[idx, 'speed_model_imputed_flag'] = True
            base.loc[idx, 'stationary_gap_assumption_flag'] = True
        if g.rule in ['assumed_intermediate_station_stop', 'assumed_terminal_direction_change'] and (not bool(g.both_low)):
            conflict = base.primary_record_id.eq(rid) & base.time_of_day_s.between(g.left_second, g.right_second)
            base.loc[conflict, 'rule_conflict_flag'] = True
    print('Applying six-site model to', len(trips), 'candidate trips', flush=True)
    for _, t in trips.iterrows():
        mask = base.primary_record_id.eq(t.primary_record_id) & base.time_of_day_s.between(t.start_second, t.end_second)
        b = base.loc[mask].copy().reset_index(drop=True)
        es = events[events.trip_id.eq(t.trip_id)].sort_values('local_order')
        direction = t.assumed_direction
        route = routes[direction]
        target = np.array([abs(coords[s] - coords[route[0]]) for s in route])
        y = b.speed_model_mps.to_numpy(float)
        rawdist = np.cumsum(np.r_[0.0, (y[:-1] + y[1:]) / 2])
        base.loc[mask, 'trip_id'] = t.trip_id
        base.loc[mask, 'trip_assumed_direction'] = direction
        base.loc[mask, 'assumed_direction'] = direction
        base.loc[mask, 'direction_phase'] = 'within_candidate_run'
        base.loc[mask, 'raw_trip_distance_m'] = rawdist
        if np.isfinite(rawdist).all():
            raw_integrals.append({'trip_id': t.trip_id, 'calculated_m': rawdist[-1], 'previous_rule_estimate_m': t.distance_m, 'difference_m': rawdist[-1] - t.distance_m})
        eligible_shape = bool(t.stop_visits_under_rule == 4 and t.link_signature == '10-15-18-57' and t.raw_low_support_all_stops)
        result = None
        status = 'incomplete_or_unsupported_four_stop_signature'
        if eligible_shape:
            result, status = shape_coordinates(b, es, target, cfg['max_unknown_motion_reconstruction_seconds'], cfg['interval_scale_allowed'])
        if result is not None:
            progress = result['progress']
            ab = coords[route[0]] + progress if direction == 'D0' else coords[route[0]] - progress
            idx = b.set_index(['primary_record_id', 'time_of_day_s']).index
            base.loc[idx, 'trip_progress_model_m'] = progress
            base.loc[idx, 'position_AB_m'] = ab
            base.loc[idx, 'speed_shape_for_position_mps'] = result['speed_shape']
            base.loc[idx, 'six_site_model_mapped_flag'] = True
            base.loc[idx, 'template_motion_reconstruction_flag'] = result['reconstructed']
            base.loc[idx, 'motion_reconstruction_at_this_second'] = result['reconstructed_at_second']
            base.loc[idx, 'stop_identity_scope'] = 'six_candidate_direction_specific_route'
            base.loc[idx, 'position_source'] = 'six_site_template_motion_reconstruction' if result['reconstructed'] else 'six_site_anchor_map_from_speed_integral'
            for k in range(3):
                a = es.iloc[k]
                z = es.iloc[k + 1]
                moving = b.time_of_day_s.gt(a.end_second) & b.time_of_day_s.lt(z.start_second)
                ii = idx[moving.to_numpy()]
                pp = progress[moving.to_numpy()]
                base.loc[ii, 'prev_stop_id'] = route[k]
                base.loc[ii, 'next_stop_id'] = route[k + 1]
                base.loc[ii, 'distance_from_prev_stop_m'] = np.maximum(0.0, pp - target[k])
                base.loc[ii, 'distance_to_next_stop_m'] = np.maximum(0.0, target[k + 1] - pp)
                interval_checks.append({'trip_id': t.trip_id, 'assumed_direction': direction, 'prev_stop_id': route[k], 'next_stop_id': route[k + 1], 'reference_interval_m': target[k + 1] - target[k], 'shape_interval_m': result['anchor_shape_positions'][k + 1] - result['anchor_shape_positions'][k], 'scale': result['scales'][k], 'motion_reconstructed': result['reconstructed']})
            for k, (_, e) in enumerate(es.iterrows()):
                stop = b.time_of_day_s.between(e.start_second, e.end_second)
                ii = idx[stop.to_numpy()]
                base.loc[ii, 'current_stop_id'] = route[k]
                base.loc[ii, 'prev_stop_id'] = route[k]
                base.loc[ii, 'distance_from_prev_stop_m'] = 0.0
                if k < 3:
                    base.loc[ii, 'next_stop_id'] = route[k + 1]
                    base.loc[ii, 'distance_to_next_stop_m'] = target[k + 1] - target[k]
            mapped[t.trip_id] = {'route': route, 'progress': progress}
        else:
            # Use anonymous stop pairs when a trip cannot be placed on the A–B axis.
            for k in range(len(es) - 1):
                a = es.iloc[k]
                z = es.iloc[k + 1]
                if not (a.raw_low_speed_support and z.raw_low_speed_support):
                    continue
                span = b.time_of_day_s.between(a.start_second, z.end_second)
                bb = b[span]
                yy = bb.speed_model_mps.to_numpy(float)
                if len(bb) < 2 or not np.isfinite(yy).all():
                    continue
                xx = np.r_[0.0, np.cumsum((yy[:-1] + yy[1:]) / 2)]
                aa = bb.time_of_day_s.between(a.start_second, a.end_second)
                zz = bb.time_of_day_s.between(z.start_second, z.end_second)
                p = float(np.median(xx[aa]))
                q = float(np.median(xx[zz]))
                length = q - p
                if length <= 0:
                    continue
                ii = bb.set_index(['primary_record_id', 'time_of_day_s']).index
                moving = bb.time_of_day_s.gt(a.end_second) & bb.time_of_day_s.lt(z.start_second)
                mi = ii[moving.to_numpy()]
                prev = f'{t.trip_id}_P{k}'
                next_ = f'{t.trip_id}_P{k + 1}'
                base.loc[mi, 'prev_stop_id'] = prev
                base.loc[mi, 'next_stop_id'] = next_
                base.loc[mi, 'distance_from_prev_stop_m'] = np.maximum(0.0, xx[moving] - p)
                base.loc[mi, 'distance_to_next_stop_m'] = np.maximum(0.0, q - xx[moving])
                si = ii[aa.to_numpy()]
                base.loc[si, 'current_stop_id'] = prev
                base.loc[si, 'prev_stop_id'] = prev
                base.loc[si, 'next_stop_id'] = next_
                base.loc[si, 'distance_from_prev_stop_m'] = 0.0
                base.loc[si, 'distance_to_next_stop_m'] = length
                base.loc[ii, 'stop_identity_scope'] = 'local_anonymous_stop_pair'
                base.loc[ii, 'position_source'] = 'local_stop_pair_speed_integral_no_AB_position'
                zi = ii[zz.to_numpy()]
                base.loc[zi, 'current_stop_id'] = next_
                base.loc[zi, 'prev_stop_id'] = next_
                base.loc[zi, 'distance_from_prev_stop_m'] = 0.0
                local_pair_details.append({'trip_id': t.trip_id, 'prev_stop_id': prev, 'next_stop_id': next_, 'distance_m': length})
        mapping.append({'trip_id': t.trip_id, 'primary_record_id': t.primary_record_id, 'assumed_direction': direction, 'shape_signature_eligible': eligible_shape, 'six_site_mapped': result is not None, 'template_motion_reconstructed': result['reconstructed'] if result is not None else False, 'status': status})
    # Link mapped runs only at a shared endpoint within one primary record.
    holds = []
    for rid, tt in trips.groupby('primary_record_id', sort=False):
        tt = tt.sort_values('trip_order')
        for (_, a), (_, z) in zip(list(tt.iterrows())[:-1], list(tt.iterrows())[1:]):
            turn = gaps[gaps.record_id.eq(rid) & gaps.left_second.eq(a.end_second) & gaps.right_second.eq(z.start_second) & gaps.rule.eq('assumed_terminal_direction_change')]
            if len(turn) != 1:
                continue
            gap = turn.iloc[0]
            status = 'unknown_boundary_not_both_mapped'
            if a.trip_id in mapped and z.trip_id in mapped:
                ar = mapped[a.trip_id]['route']
                zr = mapped[z.trip_id]['route']
                if ar[-1] != zr[0]:
                    status = 'endpoint_identity_conflict'
                elif not bool(gap.both_low):
                    status = 'nonlow_speed_edges_no_stationary_hold'
                else:
                    status = 'conditional_same_endpoint_terminal_hold'
                    station = zr[0]
                    next_ = zr[1]
                    mask = base.primary_record_id.eq(rid) & base.time_of_day_s.between(a.end_second, z.start_second - 1)
                    ii = base.loc[mask].index
                    base.loc[ii, 'position_AB_m'] = coords[station]
                    base.loc[ii, 'current_stop_id'] = station
                    base.loc[ii, 'prev_stop_id'] = station
                    base.loc[ii, 'next_stop_id'] = next_
                    base.loc[ii, 'distance_from_prev_stop_m'] = 0.0
                    base.loc[ii, 'distance_to_next_stop_m'] = abs(coords[next_] - coords[station])
                    base.loc[ii, 'assumed_direction'] = z.assumed_direction
                    base.loc[ii, 'direction_phase'] = 'terminal_hold_next_departure_assumed'
                    base.loc[ii, 'arrival_trip_id'] = a.trip_id
                    base.loc[ii, 'departure_trip_id'] = z.trip_id
                    base.loc[ii, 'six_site_model_mapped_flag'] = True
                    base.loc[ii, 'stop_identity_scope'] = 'six_candidate_direction_specific_route'
                    base.loc[ii, 'position_source'] = 'conditional_terminal_stationary_hold'
                    base.loc[ii, 'stationary_gap_assumption_flag'] = True
                    empty = mask & base.original_missing_flag
                    base.loc[empty, 'speed_model_mps'] = 0.0
                    base.loc[empty, 'speed_model_imputed_flag'] = True
                    base.loc[empty, 'gap_rule'] = 'conditional_terminal_stationary_hold'
            holds.append({'primary_record_id': rid, 'arrival_trip_id': a.trip_id, 'departure_trip_id': z.trip_id, 'left_second': int(a.end_second), 'right_second': int(z.start_second), 'missing_seconds': int(z.start_second - a.end_second - 1), 'status': status})
    mapped_table = pd.DataFrame(mapping)
    holds = pd.DataFrame(holds)
    ic = pd.DataFrame(interval_checks)
    base['distance_pair_available_flag'] = base.distance_from_prev_stop_m.notna() & base.distance_to_next_stop_m.notna()
    base['unlocated_flag'] = ~base.distance_pair_available_flag
    # Stop pairs follow direction-specific visits, including skipped sites.
    route_intervals = []
    for dr, rr in routes.items():
        for a, z in zip(rr[:-1], rr[1:]):
            route_intervals.append({'assumed_direction': dr, 'prev_stop_id': a, 'next_stop_id': z, 'distance_m': abs(coords[z] - coords[a])})
    site_table = sites.copy()
    site_table['interpretation'] = 'conditional_candidate_not_independently_confirmed_station'
    for dr in routes:
        site_table[f'{dr}_visit_order'] = site_table.candidate_site_id.map({s: k for k, s in enumerate(routes[dr])})
    checks = []

    def ck(name, ok, detail):
        checks.append({'check': name, 'status': 'PASS' if ok else 'FAIL', 'detail': detail})
    ck('all_original_seconds_preserved', len(base) == len(src), 'Includes long boundary-gap empty seconds')
    ck('all_raw_samples_preserved', int(base.n_raw.sum()) == int(src.n_raw.sum()), 'No original observation discarded')
    ck('primary_records_and_keys', base.primary_record_id.nunique() == src.session_id.nunique() and (not base.index.duplicated().any()), 'Internal-sequence grouping retained')
    valid = base[base.distance_pair_available_flag & base.stop_identity_scope.eq('six_candidate_direction_specific_route')]
    allowed = {(r['assumed_direction'], r['prev_stop_id'], r['next_stop_id']): r['distance_m'] for r in route_intervals}
    keys = list(zip(valid.assumed_direction, valid.prev_stop_id, valid.next_stop_id))
    expected = np.array([allowed.get(k, np.nan) for k in keys])
    ck('direction_specific_stop_pairs', np.isfinite(expected).all(), 'Only actual stops of the assumed direction are used')
    ck('distance_conservation', np.allclose(valid.distance_from_prev_stop_m + valid.distance_to_next_stop_m, expected, atol=1e-06), 'Previous plus next distance equals skipped-stop route interval')
    ck('nonnegative_distances', base.distance_from_prev_stop_m.dropna().ge(-1e-06).all() and base.distance_to_next_stop_m.dropna().ge(-1e-06).all(), 'No negative distances')
    ck('AB_coordinate_bounds', base.position_AB_m.dropna().between(-1e-06, line_length + 1e-06).all(), 'Modeled position is in candidate A-B segment')
    mono = True
    for tid, z in mapped.items():
        mono &= bool(np.all(np.diff(z['progress']) >= -1e-06))
    ck('run_progress_monotonic', mono, 'Progress increases in travel direction for D0 and D1')
    ri = pd.DataFrame(raw_integrals)
    ck('raw_integrals_reproduce_prior_rules', ri.difference_m.abs().lt(0.0002).all(), 'Independent reconstruction of known raw-integral lengths')
    ck('unresolved_speed_not_silently_filled', base.loc[base.gap_rule.eq('unresolved'), 'speed_model_mps'].isna().all(), 'Unknown motion may have separate position-shape reconstruction, not fabricated raw speed')
    ck('imputed_flags_match_missing_observations', not (base.speed_model_imputed_flag & ~base.original_missing_flag).any(), 'Original speed values are not overwritten')
    checks.append({'check': 'physical_station_id_and_direction_truth', 'status': 'NOT_ESTABLISHED', 'detail': 'Six sites and direction alternation are user-approved conditional assumptions'})
    failed = [check for check in checks if check['status'] == 'FAIL']
    if failed:
        raise ValueError(f'Model validation failed: {failed}')
    final = base.reset_index(drop=True)
    csv(out / 'seconds_six_site_model.csv', final)
    csv(out / 'trip_mapping.csv', mapped_table)
    csv(out / 'candidate_sites.csv', site_table)
    csv(out / 'direction_stop_intervals.csv', route_intervals)
    csv(out / 'terminal_hold_decisions.csv', holds)
    csv(out / 'interval_scaling.csv', ic)
    csv(out / 'local_anonymous_pairs.csv', local_pair_details)
    csv(out / 'raw_integral_reconciliation.csv', ri)
    csv(out / 'validation.csv', checks)
    perrecord = final.groupby('primary_record_id').agg(seconds=('n_raw', 'size'), raw_samples=('n_raw', 'sum'), original_missing_seconds=('original_missing_flag', 'sum'), distance_pair_seconds=('distance_pair_available_flag', 'sum'), six_site_mapped_seconds=('six_site_model_mapped_flag', 'sum'), unknown_seconds=('unlocated_flag', 'sum'), speed_imputed_seconds=('speed_model_imputed_flag', 'sum'), motion_reconstructed_seconds=('motion_reconstruction_at_this_second', 'sum')).reset_index()
    csv(out / 'record_quality.csv', perrecord)
    summary = {'primary_records': int(base.primary_record_id.nunique()), 'candidate_runs': len(trips), 'six_site_mapped_runs': int(mapped_table.six_site_mapped.sum()), 'motion_reconstructed_runs': int(mapped_table.template_motion_reconstructed.sum()), 'all_seconds': len(final), 'raw_samples': int(final.n_raw.sum()), 'original_missing_seconds': int(final.original_missing_flag.sum()), 'distance_pair_available_seconds': int(final.distance_pair_available_flag.sum()), 'six_site_model_seconds': int(final.six_site_model_mapped_flag.sum()), 'local_anonymous_pair_seconds': int((final.distance_pair_available_flag & final.stop_identity_scope.eq('local_anonymous_stop_pair')).sum()), 'unknown_distance_pair_seconds': int(final.unlocated_flag.sum()), 'speed_model_imputed_seconds': int(final.speed_model_imputed_flag.sum()), 'short_gap_interpolated_seconds': int(final.short_gap_interpolated_flag.sum()), 'motion_reconstructed_at_second_count': int(final.motion_reconstruction_at_this_second.sum()), 'conditional_terminal_holds': int(holds.status.eq('conditional_same_endpoint_terminal_hold').sum()), 'direction_routes': routes, 'physical_truth_confirmed': False}
    dump_json(out / 'summary.json', summary)
    dump_json(out / 'config_used.json', cfg)
    dump_json(out / 'input_manifest.json', [{'name': k, 'file': str(p), 'sha256': sha(p)} for k, p in inputs.items()])
    meanings = {'distance_from_prev_stop_m': '距该方向上一实际停车点；停站时上一停车点=当前点，距离为0；m', 'distance_to_next_stop_m': '距该方向下一实际停车点，不是6个候选点中的几何最近点；无可确定下一点时为空；m', 'position_AB_m': '六候选点条件模型下A-B坐标，不是实测定位；m', 'assumed_direction': '用于下一停车点关系的假设方向；终点等待时采用下一出发方向', 'trip_assumed_direction': '所在候选运行段的固定假设方向，首段D0仅为约定', 'direction_phase': '段内行驶/停站或终点等待下一出发；实际换向时刻未知', 'template_motion_reconstruction_flag': '该段坐标依赖未知运动速度形状与站位锚定重建', 'motion_reconstruction_at_this_second': '本秒为未知运动段的速度形状重建点，原speed_model仍为空', 'speed_shape_for_position_mps': '仅用于离线位置形状重建，不是实测速度，不作为原速度替代', 'original_missing_flag': '原始整秒桶没有记录', 'speed_model_imputed_flag': '速度模型在原始缺失秒补值，包括条件停车零速', 'stationary_gap_assumption_flag': '本秒使用停车假设，非原始连续静止观测', 'rule_conflict_flag': '时间规则与至少一侧原始低速状态冲突', 'stop_identity_scope': '六候选点映射/局部匿名停车点/无法定位', 'raw_trip_distance_m': '按接受的速度补值规则积分，遇未处理运动缺口后为空', 'offline_future_information_flag': '站点匹配、前后距离及位置重建依赖未来信息，仅用于离线分析'}
    csv(out / 'data_dictionary.csv', [{'column': c, 'meaning': meanings.get(c, '原始聚合、条件模型坐标或质量标记')} for c in final.columns])
    return summary
