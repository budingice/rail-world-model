import hashlib
import json

import pandas as pd


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
