"""mstrx_sim.py — simulación en sombra (SIN dinero, SIN órdenes) de la grilla larga MSTRX/BTC de Pionex.
Usa las velas de 15m que baja mstrx_datos. Cada variante abre un ciclo y, al cerrar por TP/SL/liquidación,
reabre de inmediato en el cierre de esa vela. Mismas reglas que el backtest (adverso primero: O→L→H→C)."""
import json, csv, io, time
import numpy as np
import db

PAR, TF = "MSTRX_BTC", "15M"
BASE = dict(n=44, tp=0.012, fee_g=0.0003, fee_t=0.0005, liq=-0.95)
VARIANTES = {
    "M1": dict(BASE, lev=15, w=1/15, sl=None, nombre="15x ±6,7% sin SL"),
    "M2": dict(BASE, lev=10, w=0.10, sl=-0.60, nombre="10x ±10% SL -60%"),
    "M3": dict(BASE, lev=10, w=0.10, sl=None, nombre="10x ±10% sin SL"),
    "M4": dict(BASE, lev=8, w=0.20, sl=None, nombre="8x ±20% sin SL"),
    "M5": dict(BASE, lev=5, w=0.20, sl=None, nombre="5x ±20% sin SL"),
}

def init_tablas():
    with db._lock, db._con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS mstrx_estado(var TEXT PRIMARY KEY, st TEXT);
        CREATE TABLE IF NOT EXISTS mstrx_ciclos(id INTEGER PRIMARY KEY AUTOINCREMENT, var TEXT, ini INTEGER, fin INTEGER,
            res TEXT, dur_h REAL, mae REAL, pnl REAL);""")

# ---------- motor (funciones puras, probadas contra el backtest) ----------
def nuevo(P0, v, ts):
    n = v["n"]; lo = P0*(1-v["w"]); hi = P0*(1+v["w"]); s = (hi-lo)/n
    kc = int((P0-lo)/s); held = [False]*n; ent = [0.0]*n
    for k in range(kc, n): held[k] = True; ent[k] = P0
    q = v["lev"]/(n*P0)
    return dict(P0=P0, lo=lo, s=s, q=q, held=held, ent=ent, real=-v["fee_t"]*q*P0*(n-kc), mae=0.0, ini=ts, velas=0)

def _eq(st, p):
    return st["real"] + sum((p-e)*st["q"] for h, e in zip(st["held"], st["ent"]) if h)

def paso(st, v, o, h, l, c):
    """Procesa una vela. Devuelve (res, pnl) si el ciclo cerró, o (None, None). Modifica st."""
    n = v["n"]; lo = st["lo"]; s = st["s"]; q = st["q"]; held = st["held"]; ent = st["ent"]
    Lv = [lo+s*k for k in range(n+1)]
    st["velas"] += 1; cur = o
    for (x, y) in ((o, l), (l, h), (h, c)):
        if y < cur:
            for k in range(n):
                if (not held[k]) and Lv[k] < cur and Lv[k] >= y:
                    held[k] = True; ent[k] = Lv[k]; st["real"] -= v["fee_g"]*q*Lv[k]
        elif y > cur:
            for k in range(n):
                if held[k] and Lv[k+1] > cur and Lv[k+1] <= y:
                    st["real"] += (Lv[k+1]-ent[k])*q - v["fee_g"]*q*Lv[k+1]; held[k] = False
        cur = y
        e = _eq(st, cur); st["mae"] = min(st["mae"], e)
        nh = sum(held)
        if v["sl"] is not None and e <= v["sl"]:
            return "sl", v["sl"] - v["fee_t"]*q*cur*nh
        if e <= v["liq"]:
            return "liq", v["liq"]
        if e >= v["tp"]:
            return "tp", v["tp"] - v["fee_t"]*q*cur*nh
    return None, None

# ---------- ciclo de vida sobre la base ----------
def _velas_nuevas(desde_ts):
    with db._lock, db._con() as c:
        return c.execute("SELECT ts,o,h,l,c FROM mstrx_velas WHERE par=? AND tf=? AND ts>? ORDER BY ts", (PAR, TF, desde_ts)).fetchall()

def _ultima_vela():
    with db._lock, db._con() as c:
        r = c.execute("SELECT ts,c FROM mstrx_velas WHERE par=? AND tf=? ORDER BY ts DESC LIMIT 1", (PAR, TF)).fetchone()
    return r

def procesar():
    """Una pasada: avanza todas las variantes con las velas 15m nuevas. Devuelve cantidad de ciclos cerrados."""
    init_tablas(); cerrados = 0
    ult = _ultima_vela()
    if not ult: return 0
    for nom, v in VARIANTES.items():
        with db._lock, db._con() as c:
            r = c.execute("SELECT st FROM mstrx_estado WHERE var=?", (nom,)).fetchone()
        if r: stt = json.loads(r[0])
        else:   # primera vez: arranca en el cierre de la última vela disponible
            stt = dict(cyc=nuevo(ult[1], v, ult[0]), last=ult[0])
            with db._lock, db._con() as c: c.execute("INSERT OR REPLACE INTO mstrx_estado VALUES(?,?)", (nom, json.dumps(stt)))
            continue
        for ts, o, h, l, cl in _velas_nuevas(stt["last"]):
            res, pnl = paso(stt["cyc"], v, o, h, l, cl)
            stt["last"] = ts
            if res:
                cy = stt["cyc"]; fin = ts + 15*60*1000
                with db._lock, db._con() as c:
                    c.execute("INSERT INTO mstrx_ciclos(var,ini,fin,res,dur_h,mae,pnl) VALUES(?,?,?,?,?,?,?)",
                              (nom, cy["ini"], fin, res, cy["velas"]*0.25, cy["mae"], pnl))
                cerrados += 1
                stt["cyc"] = nuevo(cl, v, fin)   # reapertura inmediata en el cierre de la vela
        with db._lock, db._con() as c: c.execute("INSERT OR REPLACE INTO mstrx_estado VALUES(?,?)", (nom, json.dumps(stt)))
    return cerrados

def texto_resumen():
    init_tablas(); out = ["<b>MSTRX/BTC — simulación en sombra</b> (TP 1,2%, 44 rejillas, sin dinero)"]
    for nom, v in VARIANTES.items():
        with db._lock, db._con() as c:
            rows = c.execute("SELECT res,dur_h,mae,pnl FROM mstrx_ciclos WHERE var=?", (nom,)).fetchall()
            st = c.execute("SELECT st FROM mstrx_estado WHERE var=?", (nom,)).fetchone()
        n = len(rows); tp = sum(r[0] == "tp" for r in rows); sl = sum(r[0] == "sl" for r in rows); lq = sum(r[0] == "liq" for r in rows)
        pnl = sum(r[3] for r in rows)*100; peor = min([r[2] for r in rows], default=0)*100
        abierto = ""
        if st:
            s = json.loads(st[0]); u = _ultima_vela()
            if u:
                cy = s["cyc"]; e = _eq(cy, u[1])*100
                abierto = f" | abierto: {e:+.1f}% del margen, peor {cy['mae']*100:.0f}%, {cy['velas']*0.25:.1f} h"
        out.append(f"<b>{nom}</b> {v['nombre']}: {n} ciclos (TP {tp}, SL {sl}, liq {lq}) | suma {pnl:+.1f}% del margen | peor ciclo {peor:.0f}%{abierto}")
    return "\n".join(out)

def csv_ciclos():
    init_tablas(); buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["var", "nombre", "ini_utc", "fin_utc", "res", "dur_h", "peor_pct_margen", "pnl_pct_margen"])
    f = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms/1000))
    with db._lock, db._con() as c:
        for r in c.execute("SELECT var,ini,fin,res,dur_h,mae,pnl FROM mstrx_ciclos ORDER BY id"):
            w.writerow([r[0], VARIANTES[r[0]]["nombre"], f(r[1]), f(r[2]), r[3], r[4], round(r[5]*100, 2), round(r[6]*100, 2)])
    return buf.getvalue().encode()
