"""
db.py — Bot BingX (ETH, rediseño V4 — 13/09/2026)
──────────────────────────────────────────────────────
Basado en "Estrategia Avanzada COIN-M V4.pdf". Cambios respecto a la
versión anterior: se guarda el TIPO DE CONTRATO usado en cada ciclo
(COIN-M o USDT-M — depende de la dirección, según el documento), y el
ATR absoluto (para los reingresos por múltiplo de ATR en vez de %).

Se mantiene la tabla de SIMULACIONES de la estrategia ORIGINAL (canal/
doble-triple techo-piso, sin capital real) sin tocar — Juanjo pidió
que siga recolectando datos en paralelo mientras se prueba este nuevo
diseño.
"""
import sqlite3
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", "/data/bot.db")
TZ_ARG = timezone(timedelta(hours=-3))


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ciclos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            moneda TEXT NOT NULL,
            direccion TEXT NOT NULL,
            contrato_tipo TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora_inicio TEXT NOT NULL,

            precio_entrada_1 REAL,
            atr_abs REAL,
            capital_ciclo REAL,

            n_entradas_actuales INTEGER DEFAULT 1,
            hvn_precio REAL,
            tp_actual REAL,
            salida_parcial_hecha INTEGER DEFAULT 0,

            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            fecha_cierre TEXT,
            hora_cierre TEXT,

            creado TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS entradas_ciclo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ciclo_id INTEGER NOT NULL,
            n_entrada INTEGER NOT NULL,
            precio REAL NOT NULL,
            margen_usd REAL NOT NULL,
            order_id TEXT,
            creado TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            moneda TEXT NOT NULL,
            direccion TEXT NOT NULL,
            patron_tipo TEXT,
            precio_entrada REAL,
            tp_objetivo_original REAL,
            fecha TEXT NOT NULL,
            hora_inicio TEXT NOT NULL,
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            fecha_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gates_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            moneda TEXT, direccion_candidata TEXT,
            fecha TEXT NOT NULL, hora TEXT NOT NULL,
            paso_ema200 INTEGER, paso_atr_vela INTEGER, paso_rsi INTEGER, paso_bollinger INTEGER,
            califico INTEGER, creado TEXT NOT NULL
        )
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT)")

    conn.commit()
    conn.close()


def pausar_todo(motivo: str = ""):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('pausado_global', '1')")
    conn.commit()
    conn.close()


def reanudar_todo():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('pausado_global', '0')")
    conn.commit()
    conn.close()


def esta_pausado_global() -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT valor FROM config WHERE clave = 'pausado_global'")
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0] == "1")


def guardar_gates_log(moneda, direccion_candidata, paso_ema200, paso_atr_vela, paso_rsi, paso_bollinger, califico):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO gates_log (moneda, direccion_candidata, fecha, hora, paso_ema200, paso_atr_vela, paso_rsi, paso_bollinger, califico, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (moneda, direccion_candidata, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
          int(paso_ema200), int(paso_atr_vela), int(paso_rsi), int(paso_bollinger), int(califico), ahora.isoformat()))
    conn.commit()
    conn.close()


# ── Ciclos reales ─────────────────────────────────────────────
def crear_ciclo(moneda, direccion, contrato_tipo, precio_entrada_1, atr_abs, capital_ciclo) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO ciclos (moneda, direccion, contrato_tipo, fecha, hora_inicio, precio_entrada_1, atr_abs, capital_ciclo, creado)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (moneda, direccion, contrato_tipo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
          precio_entrada_1, atr_abs, capital_ciclo, ahora.isoformat()))
    conn.commit()
    ciclo_id = cur.lastrowid
    conn.close()
    return ciclo_id


def guardar_entrada(ciclo_id: int, n_entrada: int, precio: float, margen_usd: float, order_id: str = None):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO entradas_ciclo (ciclo_id, n_entrada, precio, margen_usd, order_id, creado)
        VALUES (?,?,?,?,?,?)
    """, (ciclo_id, n_entrada, precio, margen_usd, order_id, datetime.now(TZ_ARG).isoformat()))
    conn.commit()
    conn.close()


def actualizar_ciclo_entrada(ciclo_id: int, n_entradas_actuales: int):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE ciclos SET n_entradas_actuales = ? WHERE id = ?", (n_entradas_actuales, ciclo_id))
    conn.commit()
    conn.close()


def actualizar_tp(ciclo_id: int, hvn_precio: float, tp_actual: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE ciclos SET hvn_precio = ?, tp_actual = ? WHERE id = ?", (hvn_precio, tp_actual, ciclo_id))
    conn.commit()
    conn.close()


def ciclo_abierto(moneda: str = None, contrato_tipo: str = None):
    """
    13/09: ahora acepta filtro por contrato_tipo — permite tener 2
    ciclos LARGO simultáneos e independientes en la misma moneda (uno
    en Coin-M/ETH, otro en USDT-M), cada uno con su propio capital y
    sus propias entradas, tal como confirmó Juanjo.
    """
    conn = _conn()
    cur = conn.cursor()
    condiciones = ["cerrado = 0"]
    params = []
    if moneda:
        condiciones.append("moneda = ?")
        params.append(moneda)
    if contrato_tipo:
        condiciones.append("contrato_tipo = ?")
        params.append(contrato_tipo)
    query = f"SELECT * FROM ciclos WHERE {' AND '.join(condiciones)} ORDER BY id DESC LIMIT 1"
    cur.execute(query, tuple(params))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def ciclos_abiertos(moneda: str = None) -> list:
    conn = _conn()
    cur = conn.cursor()
    if moneda:
        cur.execute("SELECT * FROM ciclos WHERE cerrado = 0 AND moneda = ?", (moneda,))
    else:
        cur.execute("SELECT * FROM ciclos WHERE cerrado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def obtener_entradas(ciclo_id: int) -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM entradas_ciclo WHERE ciclo_id = ? ORDER BY n_entrada", (ciclo_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def marcar_salida_parcial_hecha(ciclo_id: int):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE ciclos SET salida_parcial_hecha = 1 WHERE id = ?", (ciclo_id,))
    conn.commit()
    conn.close()


def cerrar_ciclo(ciclo_id: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE ciclos SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ciclo_id))
    conn.commit()
    conn.close()


def resumen_ciclos(desde_fecha: str = None) -> dict:
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT * FROM ciclos WHERE cerrado = 1 AND resultado_pct IS NOT NULL"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    cerrados = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not cerrados:
        return {"n_cerrados": 0}
    ganadores = [f for f in cerrados if f["resultado_pct"] > 0]
    return {
        "n_cerrados": len(cerrados), "n_ganadores": len(ganadores), "n_perdedores": len(cerrados) - len(ganadores),
        "win_rate_pct": round(len(ganadores) / len(cerrados) * 100, 1),
        "resultado_neto_pct": round(sum(f["resultado_pct"] for f in cerrados), 2),
        "entradas_promedio": round(sum(f["n_entradas_actuales"] for f in cerrados) / len(cerrados), 1),
    }


# ── Simulaciones (estrategia original, sin capital real) ──────
def crear_simulacion(moneda, direccion, patron_tipo, precio_entrada, tp_objetivo_original) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO simulaciones (moneda, direccion, patron_tipo, precio_entrada, tp_objetivo_original, fecha, hora_inicio, creado)
        VALUES (?,?,?,?,?,?,?,?)
    """, (moneda, direccion, patron_tipo, precio_entrada, tp_objetivo_original,
          ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ahora.isoformat()))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def simulacion_abierta(moneda: str = None):
    conn = _conn()
    cur = conn.cursor()
    if moneda:
        cur.execute("SELECT * FROM simulaciones WHERE cerrado = 0 AND moneda = ? ORDER BY id DESC LIMIT 1", (moneda,))
    else:
        cur.execute("SELECT * FROM simulaciones WHERE cerrado = 0 ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def cerrar_simulacion(sim_id: int, resultado_pct: float):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE simulaciones SET cerrado = 1, resultado_pct = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


def resumen_simulaciones(desde_fecha: str = None) -> dict:
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT * FROM simulaciones WHERE cerrado = 1 AND resultado_pct IS NOT NULL"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    cerradas = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not cerradas:
        return {"n_cerradas": 0}
    ganadoras = [f for f in cerradas if f["resultado_pct"] > 0]
    return {
        "n_cerradas": len(cerradas), "n_ganadoras": len(ganadoras), "n_perdedoras": len(cerradas) - len(ganadoras),
        "win_rate_pct": round(len(ganadoras) / len(cerradas) * 100, 1),
        "resultado_neto_pct": round(sum(f["resultado_pct"] for f in cerradas), 2),
    }
