import json, urllib.request, os
TOKEN = os.environ.get('GATEWAY_TOKEN', '3GUuP3hEpOvaF1JE2TEiEq_WedZj1-JfjqcvgHcQn8w')

def get(path, base='http://127.0.0.1:8109'):
    req = urllib.request.Request(f'{base}{path}', headers={
        'Accept': 'application/json',
        'Authorization': f'Bearer {TOKEN}',
        'X-Aria-Token': TOKEN,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        return e.code, body
    except Exception as e:
        return None, str(e)

# Try various provider endpoints
for ep in ['/v1/providers', '/v1/models', '/api/v1/providers', '/v1/health']:
    s, b = get(ep)
    print(f'{ep}: status={s}')
    if isinstance(b, str):
        print(f'  body: {b[:200]}')
    elif isinstance(b, dict):
        print(f'  keys: {list(b.keys())[:10]}')
        if 'providers' in b:
            providers = b['providers']
            if isinstance(providers, dict):
                print(f'  providers: {list(providers.keys())}')
                for k, v in list(providers.items())[:5]:
                    if isinstance(v, dict):
                        print(f'    {k}: price={v.get("price", v.get("usd","?"))} model={v.get("model_name", v.get("name",""))}')
                    else:
                        print(f'    {k}: {str(v)[:100]}')
            else:
                print(f'  providers list: {len(providers)}')
                for p in providers[:5]:
                    print(f'    {p}')
    elif isinstance(b, list):
        print(f'  list len: {len(b)}')
        for p in b[:5]:
            if isinstance(p, dict):
                print(f'    {p.get("name","?")}: price={p.get("price","?")}')
