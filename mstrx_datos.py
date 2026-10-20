"""mstrx_datos.py — recolector de velas de Pionex (SOLO LECTURA, endpoint público, sin claves).
Guarda MSTRX/BTC perp (15m, 1h, 4h) y BTC/USDT perp (1h, 4h) en la base del Shadow.
Si falla, nunca afecta al Shadow: main.py lo llama dentro de try/except."""
import time, csv, io, requests, db

BASE_URL = "https://api.pionex.com"
SERIES = [  # (símbolos candidatos en orden, intervalo, velas objetivo del primer relleno)
    (("MSTRX_BTC_PERP", "MSTRX_BTC.PERP"), "15M", 8000),
    (("MSTRX_BTC_PERP", "MSTRX_BTC.PERP"), "60M", 3000),
    (("MSTRX_BTC_PERP", "MSTRX_BTC.PERP"), "4H", 1500),
    (("BTC_USDT_PERP",), "60M", 3000),
    (("BTC_USDT_PERP",), "4H", 1500),
]
PAUSA_S = 0.5
MAX_PAGINAS = 30

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS mstrx_velas(par TEXT, tf TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
                     PRIMARY KEY(par, tf, ts))""")

def _pedir(symbol, interval, end_ms=None, limit=500):
    p = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_ms: p["endTime"] = int(end_ms)
    r = requests.get(BASE_URL + "/api/v1/market/klines", params=p, timeout=15)
    j = r.json()
    if not j.get("result", True) and not j.get("data"):
        raise RuntimeError(f"{symbol} {interval}: {str(j)[:160]}")
    ks = (j.get("data") or {}).get("klines") or []
    out = []
    for k in ks:
        out.append((int(k["time"]), float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]), float(k.get("volume", 0) or 0)))
    return sorted(out)

def _guardar(par, tf, filas):
    with db._lock, db._con() as c:
        c.executemany("INSERT OR REPLACE INTO mstrx_velas VALUES(?,?,?,?,?,?,?,?)", [(par, tf, *f) for f in filas])

def _extremos(par, tf):
    with db._lock, db._con() as c:
        r = c.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM mstrx_velas WHERE par=? AND tf=?", (par, tf)).fetchone()
    return r[0], r[1], r[2]

def _simbolo_valido(candidatos, interval):
    ult = None
    for s in candidatos:
        try:
            f = _pedir(s, interval, limit=5)
            if f: return s, f
        except Exception as e: ult = e
        time.sleep(PAUSA_S)
    raise RuntimeError(f"ningún símbolo funcionó {candidatos}: {ult}")

def recolectar():
    """Una pasada: relleno histórico la primera vez, después solo las velas nuevas. Devuelve lista de avisos."""
    init_tabla(); avisos = []
    for cands, tf, objetivo in SERIES:
        try:
            sym, recientes = _simbolo_valido(cands, tf)
            par = sym.split("_PERP")[0].replace(".PERP", "")
            mn, mx, n = _extremos(par, tf)
            _guardar(par, tf, recientes)
            if mx:  # pasada normal: pedir desde el último ts hacia adelante (500 velas alcanzan)
                _guardar(par, tf, _pedir(sym, tf))
            if n < objetivo:  # relleno hacia atrás
                mn, _, n = _extremos(par, tf)
                for _ in range(MAX_PAGINAS):
                    if n >= objetivo: break
                    filas = _pedir(sym, tf, end_ms=mn - 1)
                    if not filas: break
                    _guardar(par, tf, filas); mn2, _, n = _extremos(par, tf)
                    if mn2 >= mn: break
                    mn = mn2; time.sleep(PAUSA_S)
            time.sleep(PAUSA_S)
        except Exception as e:
            avisos.append(f"{cands[0]} {tf}: {str(e)[:120]}")
    return avisos

def resumen():
    init_tabla(); out = []
    with db._lock, db._con() as c:
        for par, tf, n, a, b in c.execute("SELECT par, tf, COUNT(*), MIN(ts), MAX(ts) FROM mstrx_velas GROUP BY par, tf").fetchall():
            f = lambda ms: time.strftime("%d/%m %H:%M", time.gmtime(ms / 1000))
            out.append(f"{par} {tf}: {n} velas, {f(a)} → {f(b)} UTC")
    return "\n".join(out) or "Sin datos de MSTRX todavía."

def csv_velas(par, tf):
    init_tabla(); buf = io.StringIO(); w = csv.writer(buf); w.writerow(["ts_utc_ms", "open", "high", "low", "close", "volume"])
    with db._lock, db._con() as c:
        for r in c.execute("SELECT ts,o,h,l,c,v FROM mstrx_velas WHERE par=? AND tf=? ORDER BY ts", (par, tf)): w.writerow(list(r))
    return buf.getvalue().encode()
