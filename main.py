"""
main.py — Bot BingX (ETH, rediseño 12/09/2026)
──────────────────────────────────────────────────────────────
Basado en "Análisis Inversión Futuros COIN-M-v2.pdf" (documento de
referencia, tomado como guía): estructura de 5 entradas escalonadas
por ciclo (martingala inversa/DCA), TP sobre precio promedio
ponderado real, SL a los niveles del documento, salida parcial 50%
desde la entrada 4.

La señal que dispara la 1ra entrada de cada ciclo sigue siendo la
detección propia (canal / doble-triple techo-piso + ADX techo + RSI
extremo) — el documento no define esto, solo la gestión de la
posición una vez abierta.

EN PARALELO, sin capital real: se sigue registrando qué hubiera hecho
la estrategia ORIGINAL (1 sola entrada, TP por figura, sin escalonado)
con la misma señal — para comparar resultados más adelante.

Solo ETH por ahora.
"""
import requests
import pandas as pd
import numpy as np
import time
import threading
import schedule
from datetime import datetime, timezone, timedelta

import db
import telegram_cmds
import gestion_riesgo
import bingx_api

TZ_ARG = timezone(timedelta(hours=-3))
MONEDA = "ETH"
ADX_TECHO = 37  # transferido de Bot Cripto, sin validación propia todavía


# ── Datos: cascada Binance → Bybit (ETH, nunca BingX) ───────
def _velas_binance(symbol, n=100, interval="4h"):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if not isinstance(data, list) or len(data) < 30:
        raise ValueError("binance empty")
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "ct", "qav", "trades", "tbbav", "tbqav", "ignore"])
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    return df


def _velas_bybit(symbol, n=100, interval="240"):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if data.get("retCode") != 0:
        raise ValueError("bybit fail")
    rows = data["result"]["list"]
    if not rows or len(rows) < 30:
        raise ValueError("bybit empty")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turnover"])
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)


def get_velas_4h(moneda, n=100):
    symbol = f"{moneda}USDT"
    for f, kw in ((_velas_binance, {"interval": "4h"}), (_velas_bybit, {"interval": "240"})):
        try:
            df = f(symbol, n, **kw)
            if df is not None and len(df) >= 30:
                return df
        except Exception:
            continue
    return None


def get_precio(moneda):
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={moneda}USDT", timeout=6)
        return float(r.json()["price"])
    except Exception:
        pass
    try:
        r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={moneda}USDT", timeout=6)
        data = r.json()
        if data.get("retCode") == 0:
            return float(data["result"]["list"][0]["lastPrice"])
    except Exception:
        pass
    return None


# ── Indicadores ──────────────────────────────────────────────
def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return float((100 - 100 / (1 + g / l.replace(0, np.nan))).iloc[-1])


def calc_adx(df, p=14):
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1 / p, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / p, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / p, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / p, adjust=False).mean()
    return float(adx.iloc[-1])


# ── Detección de figuras (dispara la 1ra entrada + la simulación) ──
def detectar_canal(df, lookback=40, tolerancia_pct=0.5):
    ventana = df.iloc[-lookback:]
    banda_alta = ventana["high"].max()
    banda_baja = ventana["low"].min()
    precio_actual = df["close"].iloc[-1]

    toques_alta = (ventana["high"] >= banda_alta * (1 - tolerancia_pct / 100)).sum()
    toques_baja = (ventana["low"] <= banda_baja * (1 + tolerancia_pct / 100)).sum()

    cerca_de_alta = precio_actual >= banda_alta * (1 - tolerancia_pct / 100)
    cerca_de_baja = precio_actual <= banda_baja * (1 + tolerancia_pct / 100)

    if cerca_de_alta and toques_alta >= 2:
        return {"patron": "canal_techo", "direccion": "CORTO", "tp_objetivo": banda_baja}
    if cerca_de_baja and toques_baja >= 2:
        return {"patron": "canal_piso", "direccion": "LARGO", "tp_objetivo": banda_alta}
    return None


def _picos_locales(serie, ventana=3):
    picos = []
    for i in range(ventana, len(serie) - ventana):
        val = serie.iloc[i]
        if val == max(serie.iloc[i - ventana:i + ventana + 1]):
            picos.append((i, val))
    return picos


def _valles_locales(serie, ventana=3):
    valles = []
    for i in range(ventana, len(serie) - ventana):
        val = serie.iloc[i]
        if val == min(serie.iloc[i - ventana:i + ventana + 1]):
            valles.append((i, val))
    return valles


def detectar_doble_triple_techo(df, lookback=60, tolerancia_pct=1.0):
    ventana = df.iloc[-lookback:].reset_index(drop=True)
    picos = _picos_locales(ventana["high"])
    if len(picos) < 2:
        return None
    ultimos_picos = picos[-3:] if len(picos) >= 3 else picos[-2:]
    alturas = [p[1] for p in ultimos_picos]
    promedio = sum(alturas) / len(alturas)
    if max(alturas) / promedio - 1 > tolerancia_pct / 100:
        return None
    idx_inicio, idx_fin = ultimos_picos[0][0], ultimos_picos[-1][0]
    if idx_fin <= idx_inicio:
        return None
    cuello = ventana["low"].iloc[idx_inicio:idx_fin + 1].min()
    precio_actual = df["close"].iloc[-1]
    if precio_actual < cuello:
        altura_patron = promedio - cuello
        tp = cuello - altura_patron
        nombre = "triple_techo" if len(ultimos_picos) == 3 else "doble_techo"
        return {"patron": nombre, "direccion": "CORTO", "tp_objetivo": tp}
    return None


def detectar_doble_triple_piso(df, lookback=60, tolerancia_pct=1.0):
    ventana = df.iloc[-lookback:].reset_index(drop=True)
    valles = _valles_locales(ventana["low"])
    if len(valles) < 2:
        return None
    ultimos_valles = valles[-3:] if len(valles) >= 3 else valles[-2:]
    profundidades = [v[1] for v in ultimos_valles]
    promedio = sum(profundidades) / len(profundidades)
    if 1 - min(profundidades) / promedio > tolerancia_pct / 100:
        return None
    idx_inicio, idx_fin = ultimos_valles[0][0], ultimos_valles[-1][0]
    if idx_fin <= idx_inicio:
        return None
    cuello = ventana["high"].iloc[idx_inicio:idx_fin + 1].max()
    precio_actual = df["close"].iloc[-1]
    if precio_actual > cuello:
        altura_patron = cuello - promedio
        tp = cuello + altura_patron
        nombre = "triple_piso" if len(ultimos_valles) == 3 else "doble_piso"
        return {"patron": nombre, "direccion": "LARGO", "tp_objetivo": tp}
    return None


def analizar_moneda(moneda: str):
    df = get_velas_4h(moneda, 100)
    if df is None:
        return None

    precio = df["close"].iloc[-1]
    adx = calc_adx(df)
    rsi_actual = calc_rsi(df["close"])

    candidato = (detectar_canal(df) or detectar_doble_triple_techo(df) or detectar_doble_triple_piso(df))
    if candidato is None:
        db.guardar_gates_log(moneda, None, None, adx, rsi_actual, adx <= ADX_TECHO, False, False)
        return None

    direccion = candidato["direccion"]
    paso_adx = adx <= ADX_TECHO
    paso_rsi = (direccion == "CORTO" and rsi_actual > 70) or (direccion == "LARGO" and rsi_actual < 30)
    califico = paso_adx and paso_rsi
    db.guardar_gates_log(moneda, direccion, candidato["patron"], adx, rsi_actual, paso_adx, paso_rsi, califico)
    if not califico:
        return None

    return {
        "moneda": moneda, "direccion": direccion, "patron_tipo": candidato["patron"],
        "precio_entrada": precio, "tp_objetivo_original": candidato["tp_objetivo"],
        "adx": round(adx, 2), "rsi": round(rsi_actual, 2),
    }


# ── Apertura de ciclo real (Entrada 1) ───────────────────────
def abrir_ciclo_real(candidato: dict):
    moneda = candidato["moneda"]
    capital = bingx_api.consultar_balance(moneda)
    if not capital or capital <= 0:
        telegram_cmds.enviar(f"⚠️ No se pudo leer el balance de {moneda} en BingX — no se abre el ciclo. Revisar /probar_bingx.")
        return

    ciclo_id = db.crear_ciclo(moneda, candidato["direccion"], candidato["patron_tipo"],
                              candidato["precio_entrada"], capital, candidato["adx"], candidato["rsi"])
    _ejecutar_entrada(ciclo_id, moneda, candidato["direccion"], 1, candidato["precio_entrada"], capital)


def _ejecutar_entrada(ciclo_id: int, moneda: str, direccion: str, n_entrada: int, precio: float, capital_ciclo: float):
    margen_usd = capital_ciclo * gestion_riesgo.PCT_MARGEN_POR_ENTRADA
    side = "BUY" if direccion == "LARGO" else "SELL"
    position_side = "LONG" if direccion == "LARGO" else "SHORT"
    notional_usd = margen_usd * gestion_riesgo.LEVERAGE_FIJO
    quantity = round(notional_usd / precio, 4)

    resultado = bingx_api.crear_orden(f"{moneda}-USD", side, position_side, "MARKET", quantity)
    ok = resultado.get("code") == 0
    if not ok:
        telegram_cmds.enviar(f"⚠️ Falló la entrada {n_entrada} de {moneda} ({direccion})\n<code>{str(resultado)[:300]}</code>")
        return False

    order_id = str(resultado.get("data", {}).get("orderId", ""))
    db.guardar_entrada(ciclo_id, n_entrada, precio, margen_usd, order_id)

    entradas = db.obtener_entradas(ciclo_id)
    promedio = gestion_riesgo.calcular_promedio_ponderado(entradas)
    tp_nuevo = gestion_riesgo.calcular_tp(direccion, promedio)
    db.actualizar_ciclo(ciclo_id, n_entrada, promedio, tp_nuevo)

    telegram_cmds.enviar(
        f"✅ <b>{moneda} entrada {n_entrada}/{gestion_riesgo.MAX_ENTRADAS}</b> ({direccion})\n"
        f"Precio: {precio:.2f} | Margen: USD {margen_usd:.2f}\n"
        f"Promedio ponderado: {promedio:.2f} | TP actual: {tp_nuevo:.2f}"
    )
    return True


# ── Simulación paralela (sin capital real) ───────────────────
def abrir_simulacion(candidato: dict):
    db.crear_simulacion(candidato["moneda"], candidato["direccion"], candidato["patron_tipo"],
                        candidato["precio_entrada"], candidato["tp_objetivo_original"])


# ── Ciclo de selección (cada 15 min) ────────────────────────
def ciclo_seleccion():
    pausado = db.esta_pausado_global()
    try:
        candidato = analizar_moneda(MONEDA)
    except Exception as e:
        print(f"Error analizando {MONEDA}: {e}")
        return
    if not candidato:
        return

    if not db.ciclo_abierto(MONEDA) and not pausado:
        abrir_ciclo_real(candidato)
    if not db.simulacion_abierta(MONEDA):
        abrir_simulacion(candidato)


# ── Chequeo de riesgo — cada 30seg ───────────────────────────
def chequeo_riesgo():
    ciclo_n = 0
    while True:
        ciclo_n += 1
        try:
            if ciclo_n % 10 == 1:
                print(f"🔄 chequeo_riesgo activo (ciclo {ciclo_n})", flush=True)

            # ── Ciclo real ──
            ciclo = db.ciclo_abierto(MONEDA)
            if ciclo:
                precio_actual = get_precio(MONEDA)
                if precio_actual is not None:
                    direccion = ciclo["direccion"]
                    precio_1 = ciclo["precio_entrada_1"]

                    # 1. SL — siempre se chequea primero
                    if gestion_riesgo.precio_toca_sl(direccion, precio_1, precio_actual):
                        resultado_pct = ((precio_actual - ciclo["precio_promedio_actual"]) / ciclo["precio_promedio_actual"] * 100) if direccion == "LARGO" else ((ciclo["precio_promedio_actual"] - precio_actual) / ciclo["precio_promedio_actual"] * 100)
                        r = bingx_api.cerrar_todas_posiciones(f"{MONEDA}-USD")
                        if r.get("code") == 0:
                            db.cerrar_ciclo(ciclo["id"], resultado_pct, "stop_loss")
                            telegram_cmds.enviar(f"🔴 <b>{MONEDA} SL</b> — ciclo cerrado. Resultado: {resultado_pct:+.2f}%")
                        else:
                            print(f"⚠️ BingX rechazó el SL de {MONEDA}: {r}", flush=True)

                    # 2. TP — sobre el precio promedio actual
                    elif ciclo["tp_actual"] and gestion_riesgo.precio_toca_tp(direccion, precio_actual, ciclo["tp_actual"]):
                        resultado_pct = gestion_riesgo.TP_PCT_SOBRE_PROMEDIO
                        r = bingx_api.cerrar_todas_posiciones(f"{MONEDA}-USD")
                        if r.get("code") == 0:
                            db.cerrar_ciclo(ciclo["id"], resultado_pct, "tp_promedio")
                            telegram_cmds.enviar(f"🟢 <b>{MONEDA} TP</b> — ciclo cerrado. Resultado: {resultado_pct:+.2f}%")
                        else:
                            print(f"⚠️ BingX rechazó el TP de {MONEDA}: {r}", flush=True)

                    # 3. Salida parcial (desde la entrada 4, si recuperó el promedio y no se hizo todavía)
                    elif (ciclo["n_entradas_actuales"] >= gestion_riesgo.ENTRADA_ACTIVA_SALIDA_PARCIAL
                          and not ciclo["salida_parcial_hecha"]
                          and gestion_riesgo.precio_recupero_promedio(direccion, precio_actual, ciclo["precio_promedio_actual"])):
                        # Cierra el 50% de la posición vía orden reduce-only
                        entradas = db.obtener_entradas(ciclo["id"])
                        notional_total = sum(e["margen_usd"] for e in entradas) * gestion_riesgo.LEVERAGE_FIJO
                        qty_50pct = round((notional_total / 2) / precio_actual, 4)
                        r = bingx_api.cerrar_parcial(f"{MONEDA}-USD", "LONG" if direccion == "LARGO" else "SHORT", qty_50pct)
                        if r.get("code") == 0:
                            db.marcar_salida_parcial_hecha(ciclo["id"])
                            telegram_cmds.enviar(f"🟡 <b>{MONEDA}</b>: salida parcial (50%) en breakeven del promedio — resto sigue hasta TP normal")
                        else:
                            print(f"⚠️ BingX rechazó la salida parcial de {MONEDA}: {r}", flush=True)

                    # 4. Siguiente entrada escalonada
                    else:
                        siguiente_n = ciclo["n_entradas_actuales"] + 1
                        if siguiente_n <= gestion_riesgo.MAX_ENTRADAS and gestion_riesgo.precio_dispara_siguiente_entrada(direccion, precio_1, precio_actual, siguiente_n):
                            _ejecutar_entrada(ciclo["id"], MONEDA, direccion, siguiente_n, precio_actual, ciclo["capital_ciclo"])

            # ── Simulación paralela (sin capital real) ──
            sim = db.simulacion_abierta(MONEDA)
            if sim:
                precio_actual = get_precio(MONEDA)
                if precio_actual is not None:
                    direccion = sim["direccion"]
                    tocado = (direccion == "LARGO" and precio_actual >= sim["tp_objetivo_original"]) or \
                             (direccion == "CORTO" and precio_actual <= sim["tp_objetivo_original"])
                    if tocado:
                        entrada = sim["precio_entrada"]
                        resultado_pct = ((precio_actual - entrada) / entrada * 100) if direccion == "LARGO" else ((entrada - precio_actual) / entrada * 100)
                        db.cerrar_simulacion(sim["id"], resultado_pct)
                        print(f"ℹ️ Simulación {MONEDA} cerrada (TP figura): {resultado_pct:+.2f}%", flush=True)

        except Exception as e:
            print(f"⚠️ chequeo_riesgo: {e}", flush=True)
        time.sleep(30)


def main():
    db.init_db()
    telegram_cmds.inicializar_offset_telegram()
    telegram_cmds.enviar("🤖 <b>Bot BingX</b> arrancó — rediseño con 5 entradas escalonadas (12/09/2026). Solo ETH.")

    hilo_riesgo = threading.Thread(target=chequeo_riesgo, daemon=True)
    hilo_riesgo.start()

    schedule.every(15).minutes.do(ciclo_seleccion)

    ciclo_principal = 0
    while True:
        ciclo_principal += 1
        try:
            schedule.run_pending()
            telegram_cmds.revisar_updates()
        except Exception as e:
            print(f"⚠️ loop principal: {e}", flush=True)
        if ciclo_principal % 12 == 1:
            print(f"💓 loop principal activo (ciclo {ciclo_principal})", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
