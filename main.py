"""
main.py — Bot BingX (ETH, rediseño V4 — 13/09/2026)
──────────────────────────────────────────────────────────────
Basado en "Estrategia Avanzada COIN-M V4.pdf" — cambio profundo
respecto al diseño anterior.

Reglas clave del documento:
- EMA200 (4h/diario) define la dirección PERMITIDA: LARGO solo si
  precio > EMA200, CORTO solo si precio < EMA200.
- LARGO permitido puede usar colateral ETH (Coin-M) — CORTO SIEMPRE
  usa USDT-M, nunca colateral cripto (protección lineal).
- Entrada 1 exige que se den JUNTAS 4 condiciones: EMA200 (dirección
  permitida), vela grande vs ATR (>2x, en contra de la dirección
  buscada), RSI (cruce de 30/70 con quiebre de confirmación), y
  Bollinger (cierre fuera de banda + reingreso en la vela siguiente).
- Reingresos en múltiplos de ATR (no % fijo) — ver gestion_riesgo.py.
- TP con VPVR: 0.5% del Nodo de Alto Volumen (HVN) más cercano.

Se mantiene en paralelo, SIN CAPITAL REAL, la simulación de la
estrategia ORIGINAL (canal/doble-triple techo-piso) — pedido explícito
de Juanjo para seguir recolectando datos comparativos mientras se
prueba este nuevo diseño.
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


# ── Datos: cascada Binance → Bybit (ETH, nunca BingX) ───────
def _velas_binance(symbol, n=250, interval="4h"):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if not isinstance(data, list) or len(data) < 30:
        raise ValueError("binance empty")
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "ct", "qav", "trades", "tbbav", "tbqav", "ignore"])
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    return df


def _velas_bybit(symbol, n=250, interval="240"):
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


def get_velas_4h(moneda, n=250):
    symbol = f"{moneda}USDT"
    for f, kw in ((_velas_binance, {"interval": "4h"}), (_velas_bybit, {"interval": "240"})):
        try:
            df = f(symbol, n, **kw)
            if df is not None and len(df) >= 210:  # necesita margen para EMA200
                return df
        except Exception:
            continue
    return None


def get_velas_1h(moneda, n=100):
    symbol = f"{moneda}USDT"
    for f, kw in ((_velas_binance, {"interval": "1h"}), (_velas_bybit, {"interval": "60"})):
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
def calc_ema(s, p):
    return s.ewm(span=p).mean()


def calc_atr(df, p=14):
    hl = df["high"] - df["low"]
    hcp = (df["high"] - df["close"].shift()).abs()
    lcp = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
    return tr.rolling(p).mean()


def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def calc_bollinger(s, p=20, num_std=2):
    media = s.rolling(p).mean()
    desvio = s.rolling(p).std()
    banda_sup = media + num_std * desvio
    banda_inf = media - num_std * desvio
    return banda_sup, banda_inf


def calcular_vpvr_hvn(df, precio_actual, direccion, n_bins=24):
    """
    VPVR simplificado: histograma de volumen por nivel de precio sobre
    el lookback completo del df. Los "nodos" son máximos locales del
    histograma. Devuelve el nodo más CERCANO al precio actual, del lado
    correcto (por encima para LARGO, por debajo para CORTO) — el TP se
    calcula 0.5% por dentro de ese nodo (gestion_riesgo.calcular_tp_vpvr).
    """
    precios = df["close"]
    volumenes = df["vol"]
    precio_min, precio_max = precios.min(), precios.max()
    if precio_max <= precio_min:
        return None
    ancho_bin = (precio_max - precio_min) / n_bins
    bins_volumen = [0.0] * n_bins
    for p, v in zip(precios, volumenes):
        idx = min(int((p - precio_min) / ancho_bin), n_bins - 1)
        idx = max(idx, 0)
        bins_volumen[idx] += v

    nodos = []
    for i in range(1, n_bins - 1):
        if bins_volumen[i] > bins_volumen[i - 1] and bins_volumen[i] > bins_volumen[i + 1] and bins_volumen[i] > 0:
            precio_nodo = precio_min + (i + 0.5) * ancho_bin
            nodos.append(precio_nodo)

    if not nodos:
        return None

    candidatos = [n for n in nodos if n > precio_actual] if direccion == "LARGO" else [n for n in nodos if n < precio_actual]
    if not candidatos:
        return None
    return min(candidatos, key=lambda n: abs(n - precio_actual))


# ── Gate de entrada: 4 condiciones juntas (Entrada 1) ────────
def evaluar_entrada_v4(moneda: str):
    df4h = get_velas_4h(moneda, 250)
    df1h = get_velas_1h(moneda, 100)
    if df4h is None or df1h is None:
        return None

    precio = df1h["close"].iloc[-1]
    ema200 = calc_ema(df4h["close"], 200).iloc[-1]
    atr_abs = calc_atr(df1h, 14).iloc[-1]
    rsi_serie = calc_rsi(df1h["close"], 14)
    rsi_actual = rsi_serie.iloc[-1]
    banda_sup, banda_inf = calc_bollinger(df1h["close"], 20, 2)

    # EMA200: define la dirección permitida
    permitido_largo = precio > ema200
    permitido_corto = precio < ema200
    direccion_candidata = "LARGO" if permitido_largo else ("CORTO" if permitido_corto else None)
    if direccion_candidata is None:
        return None

    ultima_vela = df1h.iloc[-1]
    ventana_reciente = df1h.iloc[-6:-1]  # últimas 5 velas antes de la actual (misma ventana que el RSI)

    if direccion_candidata == "LARGO":
        # ATR: alguna vela bajista grande (>2x ATR) en la ventana reciente
        # (el documento dice "gatillada TRAS vela bajista larga" — el
        # proceso de confirmación con RSI/Bollinger llega en las velas
        # siguientes, no necesariamente en la misma vela)
        cuerpos_bajistas = ventana_reciente[ventana_reciente["close"] < ventana_reciente["open"]]
        paso_atr_vela = atr_abs > 0 and bool((abs(cuerpos_bajistas["close"] - cuerpos_bajistas["open"]) > 2 * atr_abs).any())
        tramo_reciente = rsi_serie.iloc[-6:-1]
        toco_sobreventa = bool((tramo_reciente < 30).any())
        paso_rsi = toco_sobreventa and rsi_actual > 30
        paso_bollinger = bool(df1h["close"].iloc[-2] < banda_inf.iloc[-2] and precio > banda_inf.iloc[-1])
    else:
        cuerpos_alcistas = ventana_reciente[ventana_reciente["close"] > ventana_reciente["open"]]
        paso_atr_vela = atr_abs > 0 and bool((abs(cuerpos_alcistas["close"] - cuerpos_alcistas["open"]) > 2 * atr_abs).any())
        tramo_reciente = rsi_serie.iloc[-6:-1]
        toco_sobrecompra = bool((tramo_reciente > 70).any())
        paso_rsi = toco_sobrecompra and rsi_actual < 70
        paso_bollinger = bool(df1h["close"].iloc[-2] > banda_sup.iloc[-2] and precio < banda_sup.iloc[-1])

    califico = paso_atr_vela and paso_rsi and paso_bollinger
    db.guardar_gates_log(moneda, direccion_candidata, True, paso_atr_vela, paso_rsi, paso_bollinger, califico)

    if not califico:
        return None

    return {
        "moneda": moneda, "direccion": direccion_candidata, "precio_entrada": precio,
        "atr_abs": round(atr_abs, 6), "df4h": df4h,
    }


# ── Detectores VIEJOS (solo para la simulación, sin capital real) ──
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


def analizar_simulacion_original(moneda: str):
    df = get_velas_4h(moneda, 100)
    if df is None:
        return None
    candidato = (detectar_canal(df) or detectar_doble_triple_techo(df) or detectar_doble_triple_piso(df))
    if candidato is None:
        return None
    precio = df["close"].iloc[-1]
    return {"moneda": moneda, "direccion": candidato["direccion"], "patron_tipo": candidato["patron"],
            "precio_entrada": precio, "tp_objetivo_original": candidato["tp_objetivo"]}


# ── Apertura de ciclo real — rutea Coin-M o USDT-M según dirección ──
def abrir_ciclo_real(candidato: dict):
    """
    13/09: en LARGO se abren 2 ciclos INDEPENDIENTES y simultáneos —
    uno con colateral ETH (Coin-M) y otro con colateral USDT (USDT-M),
    cada uno con su propio capital y sus propias entradas (confirmado
    por Juanjo — no es un reparto dentro del mismo ciclo, son
    posiciones separadas). En CORTO, solo USDT-M (regla del documento).
    """
    moneda = candidato["moneda"]
    direccion = candidato["direccion"]

    tipos_a_abrir = ["COIN-M", "USDT-M"] if direccion == "LARGO" else ["USDT-M"]

    for contrato_tipo in tipos_a_abrir:
        if db.ciclo_abierto(moneda, contrato_tipo):
            continue  # ya hay uno de este tipo abierto, no duplicar

        capital = bingx_api.consultar_balance(moneda) if contrato_tipo == "COIN-M" else bingx_api.consultar_balance_usdtm()
        if not capital or capital <= 0:
            telegram_cmds.enviar(f"⚠️ No se pudo leer el balance ({contrato_tipo}) para {moneda} — no se abre ese ciclo.")
            continue

        ciclo_id = db.crear_ciclo(moneda, direccion, contrato_tipo, candidato["precio_entrada"], candidato["atr_abs"], capital)
        _ejecutar_entrada(ciclo_id, moneda, direccion, contrato_tipo, 1, candidato["precio_entrada"], capital, candidato["df4h"])


def _ejecutar_entrada(ciclo_id, moneda, direccion, contrato_tipo, n_entrada, precio, capital_ciclo, df4h):
    margen_usd = capital_ciclo * gestion_riesgo.PCT_MARGEN_POR_ENTRADA
    side = "BUY" if direccion == "LARGO" else "SELL"
    position_side = "LONG" if direccion == "LARGO" else "SHORT"
    notional_usd = margen_usd * gestion_riesgo.LEVERAGE_FIJO
    quantity = round(notional_usd / precio, 4)

    if contrato_tipo == "COIN-M":
        symbol = f"{moneda}-USD"
        resultado = bingx_api.crear_orden(symbol, side, position_side, "MARKET", quantity)
    else:
        symbol = f"{moneda}-USDT"
        resultado = bingx_api.crear_orden_usdtm(symbol, side, position_side, "MARKET", quantity)

    ok = resultado.get("code") == 0
    if not ok:
        telegram_cmds.enviar(f"⚠️ Falló la entrada {n_entrada} de {moneda} ({direccion}, {contrato_tipo})\n<code>{str(resultado)[:300]}</code>")
        return False

    order_id = str(resultado.get("data", {}).get("orderId", ""))
    db.guardar_entrada(ciclo_id, n_entrada, precio, margen_usd, order_id)
    db.actualizar_ciclo_entrada(ciclo_id, n_entrada)

    # Recalcular TP (VPVR) con cada entrada nueva
    hvn = calcular_vpvr_hvn(df4h, precio, direccion)
    if hvn is not None:
        tp_nuevo = gestion_riesgo.calcular_tp_vpvr(direccion, hvn)
        db.actualizar_tp(ciclo_id, hvn, tp_nuevo)
        tp_txt = f"{tp_nuevo:.2f}"
    else:
        tp_txt = "sin nodo de volumen claro todavía"

    telegram_cmds.enviar(
        f"✅ <b>{moneda} entrada {n_entrada}/{gestion_riesgo.MAX_ENTRADAS}</b> ({direccion}, {contrato_tipo})\n"
        f"Precio: {precio:.2f} | Margen: USD {margen_usd:.2f}\nTP (VPVR): {tp_txt}"
    )
    return True


def abrir_simulacion(candidato: dict):
    db.crear_simulacion(candidato["moneda"], candidato["direccion"], candidato["patron_tipo"],
                        candidato["precio_entrada"], candidato["tp_objetivo_original"])


# ── Ciclo de selección (cada 15 min) ────────────────────────
def ciclo_seleccion():
    pausado = db.esta_pausado_global()

    try:
        candidato = evaluar_entrada_v4(MONEDA)
    except Exception as e:
        print(f"Error analizando {MONEDA} (V4): {e}")
        candidato = None

    if candidato and not pausado:
        abrir_ciclo_real(candidato)  # el chequeo de duplicados por tipo de contrato ya está adentro

    # Simulación paralela: SIEMPRE corre, sin importar la pausa
    try:
        sim_candidato = analizar_simulacion_original(MONEDA)
    except Exception as e:
        print(f"Error analizando simulación {MONEDA}: {e}")
        sim_candidato = None
    if sim_candidato and not db.simulacion_abierta(MONEDA):
        abrir_simulacion(sim_candidato)


# ── Chequeo de riesgo — cada 30seg ───────────────────────────
def chequeo_riesgo():
    ciclo_n = 0
    while True:
        ciclo_n += 1
        try:
            if ciclo_n % 10 == 1:
                print(f"🔄 chequeo_riesgo activo (ciclo {ciclo_n})", flush=True)

            for ciclo in db.ciclos_abiertos(MONEDA):
                precio_actual = get_precio(MONEDA)
                if precio_actual is None:
                    continue
                direccion = ciclo["direccion"]
                precio_1 = ciclo["precio_entrada_1"]
                contrato_tipo = ciclo["contrato_tipo"]
                symbol = f"{MONEDA}-USD" if contrato_tipo == "COIN-M" else f"{MONEDA}-USDT"

                if gestion_riesgo.precio_toca_sl(direccion, precio_1, precio_actual):
                    resultado_pct = gestion_riesgo.SL_RETROCESO_LARGO_PCT if direccion == "LARGO" else -gestion_riesgo.SL_RETROCESO_CORTO_PCT
                    r = bingx_api.cerrar_todas_posiciones(symbol) if contrato_tipo == "COIN-M" else bingx_api.cerrar_todas_posiciones_usdtm(symbol)
                    if r.get("code") == 0:
                        db.cerrar_ciclo(ciclo["id"], resultado_pct, "stop_loss")
                        telegram_cmds.enviar(f"🔴 <b>{MONEDA} SL</b> ({contrato_tipo}) — ciclo cerrado. Resultado: {resultado_pct:+.2f}%")
                    else:
                        print(f"⚠️ BingX rechazó el SL de {MONEDA} ({contrato_tipo}): {r}", flush=True)
                    continue

                if ciclo["tp_actual"] and gestion_riesgo.precio_toca_tp(direccion, precio_actual, ciclo["tp_actual"]):
                    r = bingx_api.cerrar_todas_posiciones(symbol) if contrato_tipo == "COIN-M" else bingx_api.cerrar_todas_posiciones_usdtm(symbol)
                    if r.get("code") == 0:
                        resultado_pct = abs((precio_actual - precio_1) / precio_1 * 100) * gestion_riesgo.LEVERAGE_FIJO
                        db.cerrar_ciclo(ciclo["id"], resultado_pct, "tp_vpvr")
                        telegram_cmds.enviar(f"🟢 <b>{MONEDA} TP</b> ({contrato_tipo}) — ciclo cerrado. Resultado: {resultado_pct:+.2f}%")
                    else:
                        print(f"⚠️ BingX rechazó el TP de {MONEDA} ({contrato_tipo}): {r}", flush=True)
                    continue

                # 13/09: salida parcial (50% en breakeven del promedio), re-agregada a pedido de Juanjo
                if ciclo["n_entradas_actuales"] >= gestion_riesgo.ENTRADA_ACTIVA_SALIDA_PARCIAL and not ciclo["salida_parcial_hecha"]:
                    entradas = db.obtener_entradas(ciclo["id"])
                    promedio = gestion_riesgo.calcular_promedio_ponderado(entradas)
                    if promedio and gestion_riesgo.precio_recupero_promedio(direccion, precio_actual, promedio):
                        notional_total = sum(e["margen_usd"] for e in entradas) * gestion_riesgo.LEVERAGE_FIJO
                        qty_50pct = round((notional_total / 2) / precio_actual, 4)
                        position_side = "LONG" if direccion == "LARGO" else "SHORT"
                        r = bingx_api.cerrar_parcial(symbol, position_side, qty_50pct) if contrato_tipo == "COIN-M" else bingx_api.cerrar_parcial_usdtm(symbol, position_side, qty_50pct)
                        if r.get("code") == 0:
                            db.marcar_salida_parcial_hecha(ciclo["id"])
                            telegram_cmds.enviar(f"🟡 <b>{MONEDA}</b> ({contrato_tipo}): salida parcial (50%) en breakeven del promedio")
                        else:
                            print(f"⚠️ BingX rechazó la salida parcial de {MONEDA} ({contrato_tipo}): {r}", flush=True)
                        continue

                siguiente_n = ciclo["n_entradas_actuales"] + 1
                if siguiente_n <= gestion_riesgo.MAX_ENTRADAS and gestion_riesgo.precio_dispara_siguiente_entrada(direccion, precio_1, ciclo["atr_abs"], precio_actual, siguiente_n):
                    df4h = get_velas_4h(MONEDA, 250)
                    if df4h is not None:
                        _ejecutar_entrada(ciclo["id"], MONEDA, direccion, contrato_tipo, siguiente_n, precio_actual, ciclo["capital_ciclo"], df4h)

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
    telegram_cmds.enviar("🤖 <b>Bot BingX</b> arrancó — rediseño V4 (13/09/2026). EMA200+ATR+RSI+Bollinger+VPVR. Solo ETH.")

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
