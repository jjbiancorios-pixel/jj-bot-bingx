"""
telegram_cmds.py — Bot BingX
──────────────────────────────────────────────────────
"""
import requests
import os
from datetime import datetime
import db

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

_ultimo_update_id = 0


def inicializar_offset_telegram():
    global _ultimo_update_id
    intentos = 0
    while intentos < 50:
        intentos += 1
        data = _api("getUpdates", offset=_ultimo_update_id + 1, timeout=1)
        if not data.get("ok"):
            break
        updates = data.get("result", [])
        if not updates:
            break
        _ultimo_update_id = max(u["update_id"] for u in updates)
    print(f"📡 Telegram (BingX): listo, escuchando desde update_id={_ultimo_update_id}.")


def _api(method: str, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        r = requests.post(url, json=params, timeout=20)
        return r.json()
    except Exception as e:
        print(f"Telegram API error ({method}): {e}")
        return {}


def enviar(msg: str):
    resultado = _api("sendMessage", chat_id=CHAT_ID, text=msg, parse_mode="HTML")
    if not resultado.get("ok"):
        print(f"⚠️ enviar() (BingX): Telegram RECHAZÓ el mensaje — {str(resultado)[:300]}")
    return resultado


def _parse_float(s):
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _cmd_estado() -> str:
    r = db.resumen_ciclos(datetime.now(db.TZ_ARG).strftime("%Y%m%d"))
    if r["n_cerrados"] == 0:
        return "📊 Sin ciclos cerrados hoy."
    return (f"📊 <b>Estado de hoy</b>\nCerrados: {r['n_cerrados']} | ✅ {r['n_ganadores']} | ❌ {r['n_perdedores']} "
            f"| Win rate: {r['win_rate_pct']}%\nResultado neto: {r['resultado_neto_pct']:+.2f}% | "
            f"Entradas promedio por ciclo: {r['entradas_promedio']}")


def _cmd_pendientes() -> str:
    ciclos = db.ciclos_abiertos()
    sim = db.simulacion_abierta()
    lineas = []
    if ciclos:
        for ciclo in ciclos:
            tp_txt = f"{ciclo['tp_actual']:.2f}" if ciclo['tp_actual'] else "sin calcular todavía"
            lineas.append(f"📋 <b>{ciclo['moneda']} {ciclo['direccion']}</b> ({ciclo['contrato_tipo']})")
            lineas.append(f"  Entrada {ciclo['n_entradas_actuales']}/5 | Entrada 1: {ciclo['precio_entrada_1']:.2f} | TP (VPVR): {tp_txt}")
    else:
        lineas.append("✅ Sin ciclos reales abiertos.")

    # 20/09: visibilidad de las simulaciones en sombra (V4 antigua,
    # V4.1 fiel, V5.0 fiel) — antes /pendientes no las mostraba nunca,
    # sin forma de saber si tenían algo abierto mientras esperan cerrar.
    etiquetas_sombra = {
        "simulaciones_v4_antigua": "📐 V4 antigua",
        "simulaciones_v41_fiel": "📜 V4.1 fiel (sin BTC-Anchor)",
        "simulaciones_v5_fiel": "👻 V5.0 fiel (con BTC-Anchor)",
    }
    hubo_sombra = False
    for tabla, etiqueta in etiquetas_sombra.items():
        for sim_sombra in db.sim_ciclos_abiertos(tabla):
            hubo_sombra = True
            tp_txt = f"{sim_sombra['tp_actual']:.2f}" if sim_sombra['tp_actual'] else "sin calcular todavía"
            lineas.append(f"\n{etiqueta}: <b>{sim_sombra['moneda']} {sim_sombra['direccion']}</b>")
            lineas.append(f"  Entrada {sim_sombra['n_entradas_actuales']}/5 | Entrada 1: {sim_sombra['precio_entrada_1']:.2f} | TP (VPVR): {tp_txt}")
    if not hubo_sombra:
        lineas.append("\n(Sin posiciones abiertas en ninguna de las 3 simulaciones en sombra tampoco.)")

    if sim:
        lineas.append(f"\n🧪 <b>Simulación original abierta</b>: {sim['moneda']} {sim['direccion']} ({sim['patron_tipo']}) | TP objetivo: {sim['tp_objetivo_original']:.2f}")
    return "\n".join(lineas)


def _cmd_simulaciones(args: list) -> str:
    desde_fecha = None
    if args and args[0].lower() != "todo":
        desde_fecha = args[0]
    r = db.resumen_simulaciones(desde_fecha)
    if r["n_cerradas"] == 0:
        return "🧪 Sin simulaciones cerradas en este período."
    return (f"🧪 <b>Simulaciones (estrategia original, sin capital real)</b>\n"
            f"Cerradas: {r['n_cerradas']} | ✅ {r['n_ganadoras']} | ❌ {r['n_perdedoras']} | Win rate: {r['win_rate_pct']}%\n"
            f"Resultado neto: {r['resultado_neto_pct']:+.2f}%")


def _cmd_pausar_todo(args: list) -> str:
    db.pausar_todo(" ".join(args) if args else "sin motivo")
    return "🛑 Bot BingX pausado — no abre posiciones nuevas."


def _cmd_reanudar_todo() -> str:
    db.reanudar_todo()
    return "✅ Bot BingX reanudado."


def _cmd_fijar_margen(args: list) -> str:
    """20/09 — fija margen aislado AHORA, sin esperar a una entrada nueva (para confirmar que el endpoint de Coin-M funciona de verdad)."""
    moneda = (args[0].upper() if args else "ETH")
    try:
        import bingx_api
        r_coinm = bingx_api.fijar_margen_aislado(f"{moneda}-USD")
        r_usdtm = bingx_api.fijar_margen_aislado_usdtm(f"{moneda}-USDT")
        margen_coinm_after = bingx_api.consultar_margen_actual(f"{moneda}-USD")
        margen_usdtm_after = bingx_api.consultar_margen_actual_usdtm(f"{moneda}-USDT")
        return (f"🔧 <b>Fijar margen aislado — {moneda}</b>\n\n"
                f"Coin-M — respuesta: <code>{r_coinm}</code>\nQuedó en: <code>{margen_coinm_after}</code>\n\n"
                f"USDT-M — respuesta: <code>{r_usdtm}</code>\nQuedó en: <code>{margen_usdtm_after}</code>")
    except Exception as e:
        return f"⚠️ Error: {e}"


def _cmd_probar_bingx(args: list) -> str:
    moneda = (args[0].upper() if args else "ETH")
    try:
        import bingx_api
        contrato_coinm = bingx_api.consultar_contrato(f"{moneda}-USD")
        precio_coinm = bingx_api.consultar_precio(f"{moneda}-USD")
        balance_coinm = bingx_api.consultar_balance(moneda)
        precio_usdtm = bingx_api.consultar_precio_usdtm(f"{moneda}-USDT")
        balance_usdtm = bingx_api.consultar_balance_usdtm()
        margen_coinm = bingx_api.consultar_margen_actual(f"{moneda}-USD")
        margen_usdtm = bingx_api.consultar_margen_actual_usdtm(f"{moneda}-USDT")
        return (f"🧪 <b>Prueba BingX — {moneda}</b>\n"
                f"<b>Coin-M</b> — Contrato: <code>{contrato_coinm}</code>\nPrecio: {precio_coinm} | Balance: {balance_coinm}\n"
                f"Modo de margen: <code>{margen_coinm}</code>\n\n"
                f"<b>USDT-M</b> — Precio: {precio_usdtm} | Balance: {balance_usdtm}\n"
                f"Modo de margen: <code>{margen_usdtm}</code>")
    except Exception as e:
        return f"⚠️ Error al conectar con BingX: {e}"


def _cmd_gates(args: list) -> str:
    try:
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if args:
            cur.execute("SELECT * FROM gates_log WHERE moneda = ? ORDER BY id DESC LIMIT 10", (args[0].upper(),))
        else:
            cur.execute("SELECT * FROM gates_log ORDER BY id DESC LIMIT 10")
        filas = [dict(r) for r in cur.fetchall()]
        conn.close()
        if not filas:
            return "Sin registros de gates todavía."
        lineas = ["🔍 <b>Últimos chequeos (BingX V4)</b>"]
        for f in filas:
            dc = f["direccion_candidata"] or "sin dirección"
            detalle = f"ATR:{'✅' if f['paso_atr_vela'] else '❌'} RSI:{'✅' if f['paso_rsi'] else '❌'} BB:{'✅' if f['paso_bollinger'] else '❌'}"
            lineas.append(f"{f['fecha']} {f['hora']} | {dc} | {detalle} | {'CALIFICÓ' if f['califico'] else 'no calificó'}")
        return "\n".join(lineas)
    except Exception as e:
        # 14/09: antes, si esto tiraba una excepción, el comando no
        # respondía NADA (silencio total) — ahora al menos avisa el
        # motivo, en vez de parecer que el bot no escuchó el comando.
        print(f"⚠️ _cmd_gates: {e}", flush=True)
        return f"⚠️ Error al leer gates_log: {e}"


def _cmd_comparar(args: list) -> str:
    """17/09 — Directiva V5.0: compara real (V5.0 en vivo, con BTC-Anchor), V5.0 fiel, V4.1 fiel (sin BTC-Anchor), V4 antigua, y la simulación original (patrón)."""
    desde_fecha = None
    if args and args[0].lower() != "todo":
        desde_fecha = args[0]
    etiqueta = "TODO EL HISTORIAL" if desde_fecha is None else desde_fecha

    def _fmt(r):
        if r.get("n_cerrados", r.get("n_cerradas", 0)) == 0:
            return "sin cierres todavía"
        n = r.get("n_cerrados", r.get("n_cerradas"))
        return f"n={n} | win rate {r['win_rate_pct']}% | neto {r['resultado_neto_pct']:+.2f}%"

    r_real = db.resumen_ciclos(desde_fecha)
    r_v5_fiel = db.sim_resumen("simulaciones_v5_fiel", desde_fecha)
    r_v41_fiel = db.sim_resumen("simulaciones_v41_fiel", desde_fecha)
    r_v4_antigua = db.sim_resumen("simulaciones_v4_antigua", desde_fecha)
    r_original = db.resumen_simulaciones(desde_fecha)

    return (f"📊 <b>Comparación de estrategias — {etiqueta}</b>\n\n"
            f"🔴 Real (V5.0, con capital): {_fmt(r_real)}\n\n"
            f"👻 V5.0 fiel — con BTC-Anchor (sin pausa): {_fmt(r_v5_fiel)}\n\n"
            f"📜 V4.1 fiel — sin BTC-Anchor (comparación): {_fmt(r_v41_fiel)}\n\n"
            f"📐 V4 antigua (comparación): {_fmt(r_v4_antigua)}\n\n"
            f"🧪 Simulación original (patrón): {_fmt(r_original)}")


def _cmd_corregir_historico() -> str:
    """
    20/09 — comando de UN SOLO USO: recalcula resultado_pct de los
    cierres por TP que usaron la fórmula vieja (solo entrada 1, sin
    ponderar las demás) — bug corregido en fix6. Back-deriva el precio
    real de cierre a partir del resultado_pct guardado (con la fórmula
    vieja) y precio_entrada_1, después recalcula con
    calcular_pnl_pct_margen usando las entradas reales guardadas.
    """
    import sqlite3
    import gestion_riesgo
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    tablas = [("ciclos", "entradas_ciclo", "ciclo_id"),
              ("simulaciones_v5_fiel", "simulaciones_v5_fiel_entradas", "sim_id"),
              ("simulaciones_v41_fiel", "simulaciones_v41_fiel_entradas", "sim_id"),
              ("simulaciones_v4_antigua", "simulaciones_v4_antigua_entradas", "sim_id")]

    corregidos = []
    for tabla, tabla_entradas, campo_id in tablas:
        cur.execute(f"SELECT * FROM {tabla} WHERE cerrado = 1 AND motivo_cierre = 'tp_vpvr'")
        filas = [dict(r) for r in cur.fetchall()]
        for f in filas:
            cur.execute(f"SELECT precio, margen_usd FROM {tabla_entradas} WHERE {campo_id} = ?", (f["id"],))
            entradas = [{"precio": e[0], "margen_usd": e[1]} for e in cur.fetchall()]
            if len(entradas) <= 1:
                continue  # 1 sola entrada -> la fórmula vieja ya daba el resultado correcto, nada que corregir

            resultado_viejo = f["resultado_pct"]
            precio_1 = f["precio_entrada_1"]
            direccion = f["direccion"]
            factor = resultado_viejo / (100 * gestion_riesgo.LEVERAGE_FIJO)
            precio_cierre_estimado = precio_1 * (1 + factor) if direccion == "LARGO" else precio_1 * (1 - factor)

            resultado_nuevo = gestion_riesgo.calcular_pnl_pct_margen(entradas, precio_cierre_estimado, direccion, gestion_riesgo.LEVERAGE_FIJO)
            cur.execute(f"UPDATE {tabla} SET resultado_pct = ? WHERE id = ?", (resultado_nuevo, f["id"]))
            corregidos.append(f"{tabla} #{f['id']}: {resultado_viejo:+.2f}% → {resultado_nuevo:+.2f}% (precio cierre estimado: {precio_cierre_estimado:.2f}, {len(entradas)} entradas)")

    conn.commit()
    conn.close()

    if not corregidos:
        return "✅ No se encontró ningún cierre por TP con más de 1 entrada para corregir."
    return "🔧 <b>Corrección histórica aplicada</b>\n" + "\n".join(corregidos)


def _cmd_ultimas(args: list) -> str:
    """20/09 — últimas operaciones cerradas, con detalle (motivo y % exacto), en las 5 estrategias."""
    n = 5
    if args and args[0].isdigit():
        n = min(int(args[0]), 20)

    def _filas(tabla_o_query, es_ciclos_real=False):
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        tabla = "ciclos" if es_ciclos_real else tabla_o_query
        cur.execute(f"SELECT * FROM {tabla} WHERE cerrado = 1 AND resultado_pct IS NOT NULL ORDER BY id DESC LIMIT ?", (n,))
        filas = [dict(r) for r in cur.fetchall()]
        conn.close()
        return filas

    etiquetas = [
        ("ciclos", "🔴 Real (V5.0, con capital)", True),
        ("simulaciones_v5_fiel", "👻 V5.0 fiel", False),
        ("simulaciones_v41_fiel", "📜 V4.1 fiel", False),
        ("simulaciones_v4_antigua", "📐 V4 antigua", False),
    ]

    bloques = []
    for tabla, etiqueta, es_real in etiquetas:
        filas = _filas(tabla, es_real)
        if not filas:
            bloques.append(f"{etiqueta}: sin cierres todavía")
            continue
        lineas_detalle = []
        for f in filas:
            lineas_detalle.append(f"  {f['fecha_cierre']} {f['hora_cierre']} | {f['direccion']} | {f['motivo_cierre']} | {f['resultado_pct']:+.2f}%")
        bloques.append(f"{etiqueta}:\n" + "\n".join(lineas_detalle))

    return f"📋 <b>Últimas {n} operaciones cerradas, por estrategia</b>\n\n" + "\n\n".join(bloques)


def _cmd_informe(args: list) -> str:
    desde_fecha = None
    if args and args[0].lower() != "todo":
        desde_fecha = args[0]
    r = db.resumen_ciclos(desde_fecha)
    if r["n_cerrados"] == 0:
        return "📊 Sin ciclos cerrados en este período."
    return (f"📊 <b>Informe</b>\nCerrados: {r['n_cerrados']} | ✅ {r['n_ganadores']} | ❌ {r['n_perdedores']} "
            f"| Win rate: {r['win_rate_pct']}%\nResultado neto: {r['resultado_neto_pct']:+.2f}% | "
            f"Entradas promedio: {r['entradas_promedio']}")


def procesar_comando(texto: str) -> str:
    partes = texto.strip().split()
    if not partes:
        return ""
    cmd = partes[0].lower()
    args = partes[1:]

    if cmd == "/estado":
        return _cmd_estado()
    elif cmd == "/pendientes":
        return _cmd_pendientes()
    elif cmd == "/simulaciones":
        return _cmd_simulaciones(args)
    elif cmd == "/pausar_todo":
        return _cmd_pausar_todo(args)
    elif cmd == "/reanudar_todo":
        return _cmd_reanudar_todo()
    elif cmd == "/probar_bingx":
        return _cmd_probar_bingx(args)
    elif cmd == "/fijar_margen":
        return _cmd_fijar_margen(args)
    elif cmd == "/gates":
        return _cmd_gates(args)
    elif cmd == "/informe":
        return _cmd_informe(args)
    elif cmd == "/comparar":
        return _cmd_comparar(args)
    elif cmd == "/ultimas":
        return _cmd_ultimas(args)
    elif cmd == "/corregir_historico":
        return _cmd_corregir_historico()
    elif cmd in ("/ayuda", "/help", "/start"):
        return (
            "🤖 <b>Bot BingX — Comandos</b>\n\n"
            "/estado — resumen de hoy\n"
            "/pendientes — posiciones abiertas (real + las 3 simulaciones en sombra)\n"
            "/simulaciones [FECHA|todo] — resultados de la estrategia original (sin capital real)\n"
            "/comparar [FECHA|todo] — las 5 estrategias juntas (real V5.0, V5.0 fiel, "
            "V4.1 fiel, V4 antigua, simulación original)\n"
            "/ultimas [N] — últimas N operaciones cerradas por estrategia, con detalle (motivo y % exacto)\n"
            "/gates [MONEDA] — últimos 10 chequeos (diagnóstico)\n"
            "/informe [FECHA|todo] — informe completo\n"
            "/pausar_todo [motivo]\n"
            "/reanudar_todo\n"
            "/probar_bingx MONEDA — prueba conexión sin operar real\n"
            "/corregir_historico — uso puntual: recalcula cierres por TP que usaron la fórmula vieja (antes de fix6)"
        )
    return f"⚠️ No reconozco el comando \"{cmd}\" — mandá /ayuda."


def revisar_updates():
    global _ultimo_update_id
    data = _api("getUpdates", offset=_ultimo_update_id + 1, timeout=5)
    if not data.get("ok"):
        print(f"⚠️ revisar_updates (BingX): Telegram getUpdates falló — {str(data)[:300]}", flush=True)
        return
    for update in data.get("result", []):
        _ultimo_update_id = max(_ultimo_update_id, update["update_id"])
        msg = update.get("message", {})
        texto = msg.get("text", "")
        chat_id_msg = str(msg.get("chat", {}).get("id", ""))
        if not texto.startswith("/"):
            continue
        if CHAT_ID and chat_id_msg != str(CHAT_ID):
            continue
        respuesta = procesar_comando(texto)
        if respuesta:
            enviar(respuesta)
