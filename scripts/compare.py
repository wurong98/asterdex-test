#!/usr/bin/env python3
import json
import sys
from pathlib import Path

print('label,exchange,REST_n,REST_p50_ms,REST_p99_ms,WS_messages,WS_errors,event_age_p50_estimate_ms')
for arg in sys.argv[1:]:
    p = Path(arg)
    data = json.loads(p.read_text())
    for name, r in data['results'].items():
        rest = r['warm_rest_rtt']
        print(','.join(map(str, [data['metadata']['label'], name, rest['n'],
                                rest.get('p50_ms', 'NA'), rest.get('p99_ms', 'NA'),
                                r['ws_messages'], r['ws_errors'],
                                r['estimated_event_age'].get('p50_ms', 'NA')])))
