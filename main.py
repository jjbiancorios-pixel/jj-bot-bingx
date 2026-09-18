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


def get_velas_15m(moneda, n=100):
    """17/09 — nuevo para V4.1: ATR/RSI/Bollinger pasan a calcularse en 15min, no 1h."""
    symbol = f"{moneda}USDT"
    for f, kw in ((_velas_binance, {"interval": "15m"}), (_velas_bybit, {"interval": "15"})):
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


# ── Gate de entrada V4 (ANTIGUA — queda solo para la simulación de comparación) ──
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

    permitido_largo = precio > ema200
    permitido_corto = precio < ema200
    direccion_candidata = "LARGO" if permitido_largo else ("CORTO" if permitido_corto else None)
    if direccion_candidata is None:
        return None

    ventana_reciente = df1h.iloc[-6:-1]

    if direccion_candidata == "LARGO":
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
    # 17/09: ya NO loguea en gates_log (eso queda reservado para V4.1,
    # la principal ahora) — esta versión solo alimenta su propia
    # simulación de comparación.
    if not califico:
        return None

    return {
        "moneda": moneda, "direccion": direccion_candidata, "precio_entrada": precio,
        "atr_abs": round(atr_abs, 6), "df4h": df4h,
    }


# ── Gate de entrada V4.1 (NUEVA — principal) ─────────────────
def evaluar_entrada_v41(moneda: str):
    """
    17/09 — Directiva V4.1, reemplaza a V4 como principal:
    1. ATR/RSI/Bollinger ahora en 15min (antes 1h)
    2. EMA200 se mantiene aislada en 4h (sin cambios)
    3. Vela grande: techo bajado a >1.5x ATR (antes 2x), ventana de
       5 velas de 15min (antes 1h)
    4. SL cambia a control de margen flotante (-40% del margen total
       invertido) — ver gestion_riesgo.precio_toca_sl_margen, se aplica
       en el loop de riesgo, no acá en la entrada.
    """
    df4h = get_velas_4h(moneda, 250)
    df15m = get_velas_15m(moneda, 200)
    if df4h is None or df15m is None:
        return None

    precio = df15m["close"].iloc[-1]
    ema200 = calc_ema(df4h["close"], 200).iloc[-1]
    atr_abs = calc_atr(df15m, 14).iloc[-1]
    rsi_serie = calc_rsi(df15m["close"], 14)
    rsi_actual = rsi_serie.iloc[-1]
    banda_sup, banda_inf = calc_bollinger(df15m["close"], 20, 2)

    permitido_largo = precio > ema200
    permitido_corto = precio < ema200
    direccion_candidata = "LARGO" if permitido_largo else ("CORTO" if permitido_corto else None)
    if direccion_candidata is None:
        return None

    ventana_reciente = df15m.iloc[-6:-1]  # últimas 5 velas de 15min

    if direccion_candidata == "LARGO":
        cuerpos_bajistas = ventana_reciente[ventana_reciente["close"] < ventana_reciente["open"]]
        paso_atr_vela = atr_abs > 0 and bool((abs(cuerpos_bajistas["close"] - cuerpos_bajistas["open"]) > 1.5 * atr_abs).any())
        tramo_reciente = rsi_serie.iloc[-6:-1]
        toco_sobreventa = bool((tramo_reciente < 30).any())
        paso_rsi = toco_sobreventa and rsi_actual > 30
        paso_bollinger = bool(df15m["close"].iloc[-2] < banda_inf.iloc[-2] and precio > banda_inf.iloc[-1])
    else:
        cuerpos_alcistas = ventana_reciente[ventana_reciente["close"] > ventana_reciente["open"]]
        paso_atr_vela = atr_abs > 0 and bool((abs(cuerpos_alcistas["close"] - cuerpos_alcistas["open"]) > 1.5 * atr_abs).any())
        tramo_reciente = rsi_serie.iloc[-6:-1]
        toco_sobrecompra = bool((tramo_reciente > 70).any())
        paso_rsi = toco_sobrecompra and rsi_actual < 70
        paso_bollinger = bool(df15m["close"].iloc[-2] > banda_sup.iloc[-2] and precio < banda_sup.iloc[-1])

    califico = paso_atr_vela and paso_rsi and paso_bollinger
    db.guardar_gates_log(moneda, direccion_candidata, True, paso_atr_vela, paso_rsi, paso_bollinger, califico)

    if not califico:
        return None

    return {
        "moneda": moneda, "direccion": direccion_candidata, "precio_entrada": precio,
        "atr_abs": round(atr_abs, 6), "df4h": df4h,
    }


# ── Gate de entrada V5.0 (NUEVA — principal, reemplaza a V4.1) ──
def evaluar_entrada_v5(moneda: str):
    """
    17/09 — Directiva V5.0: idéntica a V4.1 (15m, umbral 1,5x ATR, SL
    por margen -40%, colateral dual en LARGO — ninguno de esos cambia)
    + 1 gate NUEVO: Filtro BTC-Anchor — BTC también debe estar del
    mismo lado de SU PROPIA EMA200 (4h), no solo ETH. V4.1 (sin este
    filtro) queda corriendo sin tocar, como comparación.
    """
    df4h = get_velas_4h(moneda, 250)
    df15m = get_velas_15m(moneda, 200)
    if df4h is None or df15m is None:
        return None

    precio = df15m["close"].iloc[-1]
    ema200 = calc_ema(df4h["close"], 200).iloc[-1]

    permitido_largo = precio > ema200
    permitido_corto = precio < ema200
    direccion_candidata = "LARGO" if permitido_largo else ("CORTO" if permitido_corto else None)
    if direccion_candidata is None:
        return None

    # NUEVO — Filtro BTC-Anchor: BTC también del mismo lado de SU propia EMA200 (4h)
    df4h_btc = get_velas_4h("BTC", 250)
    if df4h_btc is None:
        return None
    precio_btc = df4h_btc["close"].iloc[-1]
    ema200_btc = calc_ema(df4h_btc["close"], 200).iloc[-1]
    btc_alineado = (precio_btc > ema200_btc) if direccion_candidata == "LARGO" else (precio_btc < ema200_btc)
    if not btc_alineado:
        return None

    atr_abs = calc_atr(df15m, 14).iloc[-1]
    rsi_serie = calc_rsi(df15m["close"], 14)
    rsi_actual = rsi_serie.iloc[-1]
    banda_sup, banda_inf = calc_bollinger(df15m["close"], 20, 2)

    ventana_reciente = df15m.iloc[-6:-1]

    if direccion_candidata == "LARGO":
        cuerpos_bajistas = ventana_reciente[ventana_reciente["close"] < ventana_reciente["open"]]
        paso_atr_vela = atr_abs > 0 and bool((abs(cuerpos_bajistas["close"] - cuerpos_bajistas["open"]) > 1.5 * atr_abs).any())
        tramo_reciente = rsi_serie.iloc[-6:-1]
        toco_sobreventa = bool((tramo_reciente < 30).any())
        paso_rsi = toco_sobreventa and rsi_actual > 30
        paso_bollinger = bool(df15m["close"].iloc[-2] < banda_inf.iloc[-2] and precio > banda_inf.iloc[-1])
    else:
        cuerpos_alcistas = ventana_reciente[ventana_reciente["close"] > ventana_reciente["open"]]
        paso_atr_vela = atr_abs > 0 and bool((abs(cuerpos_alcistas["close"] - cuerpos_alcistas["open"]) > 1.5 * atr_abs).any())
        tramo_reciente = rsi_serie.iloc[-6:-1]
        toco_sobrecompra = bool((tramo_reciente > 70).any())
        paso_rsi = toco_sobrecompra and rsi_actual < 70
        paso_bollinger = bool(df15m["close"].iloc[-2] > banda_sup.iloc[-2] and precio < banda_sup.iloc[-1])

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

    # ── V5.0 (PRINCIPAL, 17/09) — real si no está pausado ──
    try:
        candidato_v5 = evaluar_entrada_v5(MONEDA)
    except Exception as e:
        print(f"Error analizando {MONEDA} (V5.0): {e}")
        candidato_v5 = None

    if candidato_v5 and not pausado:
        abrir_ciclo_real(candidato_v5)

    # ── "V5.0 fiel" — SIEMPRE recopila, sin importar la pausa ──
    if candidato_v5 and not db.sim_ciclo_abierto("simulaciones_v5_fiel", MONEDA):
        capital_sim = _capital_estimado_para_simulacion(MONEDA, candidato_v5["direccion"])
        sim_id = db.sim_crear_ciclo("simulaciones_v5_fiel", MONEDA, candidato_v5["direccion"],
                                     candidato_v5["precio_entrada"], candidato_v5["atr_abs"], capital_sim)
        _ejecutar_entrada_simulada("simulaciones_v5_fiel", sim_id, MONEDA, candidato_v5["direccion"],
                                    1, candidato_v5["precio_entrada"], capital_sim, candidato_v5["df4h"])

    # ── V4.1 (ahora COMPARACIÓN — sin filtro BTC-Anchor) — SIEMPRE recopila ──
    try:
        candidato_v41 = evaluar_entrada_v41(MONEDA)
    except Exception as e:
        print(f"Error analizando {MONEDA} (V4.1): {e}")
        candidato_v41 = None

    # ── "V4.1 fiel" — SIEMPRE recopila, sin importar la pausa
    # (misma señal que la real, réplica exacta) ──
    if candidato_v41 and not db.sim_ciclo_abierto("simulaciones_v41_fiel", MONEDA):
        capital_sim = _capital_estimado_para_simulacion(MONEDA, candidato_v41["direccion"])
        sim_id = db.sim_crear_ciclo("simulaciones_v41_fiel", MONEDA, candidato_v41["direccion"],
                                     candidato_v41["precio_entrada"], candidato_v41["atr_abs"], capital_sim)
        _ejecutar_entrada_simulada("simulaciones_v41_fiel", sim_id, MONEDA, candidato_v41["direccion"],
                                    1, candidato_v41["precio_entrada"], capital_sim, candidato_v41["df4h"])

    # ── V4 (antigua) — SIEMPRE recopila, sin importar la pausa,
    # para comparar contra V4.1 ──
    try:
        candidato_v4 = evaluar_entrada_v4(MONEDA)
    except Exception as e:
        print(f"Error analizando {MONEDA} (V4 antigua): {e}")
        candidato_v4 = None

    if candidato_v4 and not db.sim_ciclo_abierto("simulaciones_v4_antigua", MONEDA):
        capital_sim = _capital_estimado_para_simulacion(MONEDA, candidato_v4["direccion"])
        sim_id = db.sim_crear_ciclo("simulaciones_v4_antigua", MONEDA, candidato_v4["direccion"],
                                     candidato_v4["precio_entrada"], candidato_v4["atr_abs"], capital_sim)
        _ejecutar_entrada_simulada("simulaciones_v4_antigua", sim_id, MONEDA, candidato_v4["direccion"],
                                    1, candidato_v4["precio_entrada"], capital_sim, candidato_v4["df4h"])

    # ── Simulación paralela ORIGINAL (canal/doble-triple techo): SIEMPRE corre ──
    try:
        sim_candidato = analizar_simulacion_original(MONEDA)
    except Exception as e:
        print(f"Error analizando simulación {MONEDA}: {e}")
        sim_candidato = None
    if sim_candidato and not db.simulacion_abierta(MONEDA):
        abrir_simulacion(sim_candidato)


def _capital_estimado_para_simulacion(moneda: str, direccion: str) -> float:
    """17/09 — capital de referencia para las simulaciones (mismo criterio que usaría una apertura real, sin depender de que haya capital real disponible ahora)."""
    try:
        if direccion == "LARGO":
            cap = bingx_api.consultar_balance(moneda)
        else:
            cap = bingx_api.consultar_balance_usdtm()
        return cap if cap else 100.0  # valor de referencia si no se pudo leer balance real
    except Exception:
        return 100.0


def _ejecutar_entrada_simulada(tabla: str, sim_id: int, moneda: str, direccion: str, n_entrada: int,
                                precio: float, capital_ciclo: float, df4h):
    """17/09 — versión SIN capital real de _ejecutar_entrada, para las simulaciones de comparación."""
    margen_usd = capital_ciclo * gestion_riesgo.PCT_MARGEN_POR_ENTRADA
    db.sim_guardar_entrada(tabla, sim_id, n_entrada, precio, margen_usd)
    db.sim_actualizar_entrada(tabla, sim_id, n_entrada)
    hvn = calcular_vpvr_hvn(df4h, precio, direccion)
    if hvn is not None:
        tp_nuevo = gestion_riesgo.calcular_tp_vpvr(direccion, hvn)
        db.sim_actualizar_tp(tabla, sim_id, hvn, tp_nuevo)


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

                if gestion_riesgo.precio_toca_sl_margen(db.obtener_entradas(ciclo["id"]), precio_actual, direccion, gestion_riesgo.LEVERAGE_FIJO):
                    pnl_margen = gestion_riesgo.calcular_pnl_pct_margen(db.obtener_entradas(ciclo["id"]), precio_actual, direccion, gestion_riesgo.LEVERAGE_FIJO)
                    r = bingx_api.cerrar_todas_posiciones(symbol) if contrato_tipo == "COIN-M" else bingx_api.cerrar_todas_posiciones_usdtm(symbol)
                    if r.get("code") == 0:
                        db.cerrar_ciclo(ciclo["id"], pnl_margen, "stop_loss_margen")
                        telegram_cmds.enviar(f"🔴 <b>{MONEDA} SL (V4.1, por margen)</b> ({contrato_tipo}) — ciclo cerrado. PNL: {pnl_margen:+.2f}% del margen")
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

            # ── 17/09: chequeo de las 2 simulaciones de comparación (V4 antigua y V4.1 fiel) ──
            for tabla_sim, usa_sl_margen in (("simulaciones_v4_antigua", False), ("simulaciones_v41_fiel", True), ("simulaciones_v5_fiel", True)):
                for sim in db.sim_ciclos_abiertos(tabla_sim, MONEDA):
                    precio_sim = get_precio(MONEDA)
                    if precio_sim is None:
                        continue
                    direccion_sim = sim["direccion"]
                    entradas_sim = db.sim_obtener_entradas(tabla_sim, sim["id"])

                    if usa_sl_margen:
                        cierra_sl = gestion_riesgo.precio_toca_sl_margen(entradas_sim, precio_sim, direccion_sim, gestion_riesgo.LEVERAGE_FIJO)
                        resultado_sl = gestion_riesgo.calcular_pnl_pct_margen(entradas_sim, precio_sim, direccion_sim, gestion_riesgo.LEVERAGE_FIJO)
                    else:
                        cierra_sl = gestion_riesgo.precio_toca_sl(direccion_sim, sim["precio_entrada_1"], precio_sim)
                        resultado_sl = gestion_riesgo.SL_RETROCESO_LARGO_PCT if direccion_sim == "LARGO" else -gestion_riesgo.SL_RETROCESO_CORTO_PCT

                    if cierra_sl:
                        db.sim_cerrar_ciclo(tabla_sim, sim["id"], resultado_sl, "stop_loss")
                        continue

                    if sim["tp_actual"] and gestion_riesgo.precio_toca_tp(direccion_sim, precio_sim, sim["tp_actual"]):
                        resultado_tp = abs((precio_sim - sim["precio_entrada_1"]) / sim["precio_entrada_1"] * 100) * gestion_riesgo.LEVERAGE_FIJO
                        db.sim_cerrar_ciclo(tabla_sim, sim["id"], resultado_tp, "tp_vpvr")
                        continue

                    siguiente_n_sim = sim["n_entradas_actuales"] + 1
                    if siguiente_n_sim <= gestion_riesgo.MAX_ENTRADAS and gestion_riesgo.precio_dispara_siguiente_entrada(direccion_sim, sim["precio_entrada_1"], sim["atr_abs"], precio_sim, siguiente_n_sim):
                        df4h_sim = get_velas_4h(MONEDA, 250)
                        if df4h_sim is not None:
                            _ejecutar_entrada_simulada(tabla_sim, sim["id"], MONEDA, direccion_sim, siguiente_n_sim, precio_sim, sim["capital_ciclo"], df4h_sim)

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
