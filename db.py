"""
db.py — Bot BingX (ETH, rediseño 12/09/2026)
──────────────────────────────────────────────────────
Basado en "Análisis Inversión Futuros COIN-M-v2.pdf" (documento de
referencia): estructura de CICLOS con hasta 5 ENTRADAS escalonadas
cada uno (martingala inversa/DCA), en vez de 1 sola posición por señal.

Se mantiene en paralelo una tabla de SIMULACIONES (sin capital real):
la estrategia original (canal/doble-triple techo-piso, 1 sola entrada,
TP por figura, sin escalonado) — para comparar resultados más adelante
y decidir si conviene usarla, combinarla, o descartarla.
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
            patron_tipo TEXT,
            fecha TEXT NOT NULL,
            hora_inicio TEXT NOT NULL,

            precio_entrada_1 REAL,
            capital_ciclo REAL,
            adx REAL,
            rsi REAL,

            n_entradas_actuales INTEGER DEFAULT 1,
            precio_promedio_actual REAL,
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
            moneda TEXT, direccion TEXT, patron_tipo TEXT,
            fecha TEXT NOT NULL, hora TEXT NOT NULL,
            adx REAL, rsi REAL, paso_adx INTEGER, paso_rsi INTEGER,
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


def guardar_gates_log(moneda, direccion, patron_tipo, adx, rsi, paso_adx, paso_rsi, califico):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO gates_log (moneda, direccion, patron_tipo, fecha, hora, adx, rsi, paso_adx, paso_rsi, califico, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (moneda, direccion, patron_tipo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
          adx, rsi, int(paso_adx), int(paso_rsi), int(califico), ahora.isoformat()))
    conn.commit()
    conn.close()


# ── Ciclos reales (dinero real, estructura de 5 entradas) ───
def crear_ciclo(moneda, direccion, patron_tipo, precio_entrada_1, capital_ciclo, adx, rsi) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO ciclos (moneda, direccion, patron_tipo, fecha, hora_inicio, precio_entrada_1,
                             capital_ciclo, adx, rsi, precio_promedio_actual, tp_actual, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (moneda, direccion, patron_tipo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
          precio_entrada_1, capital_ciclo, adx, rsi, precio_entrada_1, None, ahora.isoformat()))
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


def obtener_entradas(ciclo_id: int) -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM entradas_ciclo WHERE ciclo_id = ? ORDER BY n_entrada", (ciclo_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def actualizar_ciclo(ciclo_id: int, n_entradas_actuales: int, precio_promedio: float, tp_actual: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE ciclos SET n_entradas_actuales = ?, precio_promedio_actual = ?, tp_actual = ?
        WHERE id = ?
    """, (n_entradas_actuales, precio_promedio, tp_actual, ciclo_id))
    conn.commit()
    conn.close()


def marcar_salida_parcial_hecha(ciclo_id: int):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE ciclos SET salida_parcial_hecha = 1 WHERE id = ?", (ciclo_id,))
    conn.commit()
    conn.close()


def ciclo_abierto(moneda: str = None):
    conn = _conn()
    cur = conn.cursor()
    if moneda:
        cur.execute("SELECT * FROM ciclos WHERE cerrado = 0 AND moneda = ? ORDER BY id DESC LIMIT 1", (moneda,))
    else:
        cur.execute("SELECT * FROM ciclos WHERE cerrado = 0 ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


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


# ── Simulaciones (estrategia vieja, sin capital real, para comparar) ──
def crear_simulacion(moneda, direccion, patron_tipo, precio_entrada, tp_objetivo_original) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO simulaciones (moneda, direccion, patron_tipo, precio_entrada, tp_objetivo_original,
                                    fecha, hora_inicio, creado)
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
