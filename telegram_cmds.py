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
    if sim:
        lineas.append(f"\n🧪 <b>Simulación abierta</b>: {sim['moneda']} {sim['direccion']} ({sim['patron_tipo']}) | TP objetivo: {sim['tp_objetivo_original']:.2f}")
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


def _cmd_probar_bingx(args: list) -> str:
    moneda = (args[0].upper() if args else "ETH")
    try:
        import bingx_api
        contrato_coinm = bingx_api.consultar_contrato(f"{moneda}-USD")
        precio_coinm = bingx_api.consultar_precio(f"{moneda}-USD")
        balance_coinm = bingx_api.consultar_balance(moneda)
        precio_usdtm = bingx_api.consultar_precio_usdtm(f"{moneda}-USDT")
        balance_usdtm = bingx_api.consultar_balance_usdtm()
        return (f"🧪 <b>Prueba BingX — {moneda}</b>\n"
                f"<b>Coin-M</b> — Contrato: <code>{contrato_coinm}</code>\nPrecio: {precio_coinm} | Balance: {balance_coinm}\n\n"
                f"<b>USDT-M</b> — Precio: {precio_usdtm} | Balance: {balance_usdtm}")
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
    """17/09 — Directiva V4.1: compara real (V4.1 en vivo), V4.1 fiel (simulada, sin pausa), V4 antigua (simulada), y la simulación original (patrón)."""
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
    r_v41_fiel = db.sim_resumen("simulaciones_v41_fiel", desde_fecha)
    r_v4_antigua = db.sim_resumen("simulaciones_v4_antigua", desde_fecha)
    r_original = db.resumen_simulaciones(desde_fecha)

    return (f"📊 <b>Comparación de estrategias — {etiqueta}</b>\n\n"
            f"🔴 Real (V4.1, con capital): {_fmt(r_real)}\n\n"
            f"👻 V4.1 fiel (sin pausa): {_fmt(r_v41_fiel)}\n\n"
            f"📐 V4 antigua (comparación): {_fmt(r_v4_antigua)}\n\n"
            f"🧪 Simulación original (patrón): {_fmt(r_original)}")


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
    elif cmd == "/gates":
        return _cmd_gates(args)
    elif cmd == "/informe":
        return _cmd_informe(args)
    elif cmd == "/comparar":
        return _cmd_comparar(args)
    elif cmd in ("/ayuda", "/help", "/start"):
        return (
            "🤖 <b>Bot BingX — Comandos</b>\n\n"
            "/estado — resumen de hoy\n"
            "/pendientes — posiciones abiertas\n"
            "/simulaciones [FECHA|todo] — resultados de la estrategia original (sin capital real)\n"
            "/gates [MONEDA] — últimos 10 chequeos (diagnóstico)\n"
            "/informe [FECHA|todo] — informe completo\n"
            "/pausar_todo [motivo]\n"
            "/reanudar_todo\n"
            "/probar_bingx MONEDA — prueba conexión sin operar real"
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
