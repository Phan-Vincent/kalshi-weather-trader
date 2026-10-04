import re, json, math

with open('index.html') as f:
    html = f.read()

# Check data injection
l_match = re.search(r'L\s*=\s*(\{.*?\});', html, re.DOTALL)
p_match = re.search(r'P\s*=\s*(\{.*?\});', html, re.DOTALL)

L = json.loads(l_match.group(1))
P = json.loads(p_match.group(1))

def check_values(obj, path='P'):
    issues = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            issues += check_values(v, f'{path}.{k}')
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            issues += check_values(v, f'{path}[{i}]')
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            issues.append(f'{path} = {obj}')
    elif obj is None:
        issues.append(f'{path} = None (may cause JS issues)')
    return issues

print('=== Data quality ===')
p_issues = check_values(P)
l_issues = check_values(L)
if not p_issues and not l_issues:
    print('  OK: No NaN/Inf/None in data')
else:
    for i in p_issues + l_issues:
        print(f'  ISSUE: {i}')

print(f'  P.daily: {len(P.get("daily",[]))} entries')
print(f'  P.cities: {len(P.get("cities",[]))} entries')
print(f'  P.weekly: {len(P.get("weekly",[]))} entries')
print(f'  P.monthly: {len(P.get("monthly",[]))} entries')
print(f'  P.brier_rolling: {len(P.get("brier_rolling",[]))} entries')
print(f'  L.history: {len(L.get("history",[]))} snapshots')

# Check data types match v3 expectations
daily = P.get('daily', [])
if daily:
    d0 = daily[0]
    print(f'\n=== Field check ===')
    for field in ['date','pnl','trades','wins']:
        print(f'  P.daily[0].{field}: {d0.get(field)} (type={type(d0.get(field)).__name__})')
cities = P.get('cities', [])
if cities:
    for field in ['code','city','pnl','consec_losses']:
        print(f'  P.cities[0].{field}: {cities[0].get(field)}')

weekly = P.get('weekly', [])
if weekly:
    for field in ['week','pnl','trades','wins']:
        print(f'  P.weekly[0].{field}: {weekly[0].get(field)}')

print(f'\n  Paper cash: ${P.get("cash",0):,.2f}')
print(f'  Live total: ${L.get("total_balance_dollars",0):,.2f}')
