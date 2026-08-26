set -euo pipefail
mkdir -p xauusd_replay/data/bid xauusd_replay/data/ask xauusd_replay/output
python - <<'PY'
import base64,gzip,pathlib,urllib.request,time
root=pathlib.Path('xauusd_replay')
for pattern,dst in [('signals_*.b64part','signals.csv'),('management_*.b64part','management_compact.csv')]:
    parts=sorted(root.glob(pattern))
    if not parts: raise RuntimeError(f'No parts for {pattern}')
    b64=''.join(p.read_text().strip() for p in parts)
    data=gzip.decompress(base64.b64decode(b64))
    (root/dst).write_bytes(data)
# Reconstruct the replay engine from its base64 parts.
parts=sorted(root.glob('replay_*.b64part'))
if not parts: raise RuntimeError('No replay engine parts')
b64=''.join(p.read_text().strip() for p in parts)
(root/'replay_remote.py').write_bytes(base64.b64decode(b64))
months=[]
y,m=2023,5
while (y,m) <= (2026,8):
    months.append((y,m))
    m+=1
    if m==13: y+=1;m=1
base='https://raw.githubusercontent.com/kevingtlin/Market-Data-Lab/main/xauusd/{side}/m1/xauusd_{side}_m1_{y:04d}_{m:02d}.csv'
for side in ('bid','ask'):
    for y,m in months:
        url=base.format(side=side,y=y,m=m)
        out=root/'data'/side/f'xauusd_{side}_m1_{y:04d}_{m:02d}.csv'
        for attempt in range(4):
            try:
                print('download',url, flush=True)
                urllib.request.urlretrieve(url,out)
                if out.stat().st_size < 1000:
                    raise RuntimeError(f'too small {out.stat().st_size}')
                break
            except Exception as e:
                if attempt==3: raise
                print('retry',e, flush=True);time.sleep(2*(attempt+1))
PY
python -m py_compile xauusd_replay/replay_remote.py
python xauusd_replay/replay_remote.py
