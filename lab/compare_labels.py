import json, glob, os, sys
tests = json.load(open('sample/tests.json'))['tests']; idx = {t['id']: t['number'] for t in tests}
ans = json.load(open('sample/answers.json'))['commits']
d = sys.argv[1] if len(sys.argv) > 1 else 'out/samples_lf'
ok = 0
for f in sorted(glob.glob(os.path.join(d, '*.json'))):
    c = os.path.basename(f)[:-5]
    if c not in ans: continue
    r = json.load(open(f))
    mine = sorted(idx[k] for k, v in r['results'].items() if v == 'failed' and k in idx)
    exp = sorted(ans[c]['failing'])
    same = mine == exp
    ok += same
    print(c, ans[c]['kind'], 'MATCH' if same else f'DIFF mine={mine} expected={exp}', 'collect_err' if r['collect_errors'] else '')
print('matched', ok)
