"""
gestion_riesgo.py — Bot BingX (ETH, rediseño V4 — 13/09/2026)
──────────────────────────────────────────────────────────────
Basado en "Estrategia Avanzada COIN-M V4.pdf" (documento de referencia
cargado por Juanjo — cambio profundo respecto al diseño anterior, que
seguía "Análisis Inversión Futuros COIN-M-v2").

Diferencia clave frente a la versión anterior: los reingresos ya NO son
en % fijo del precio — son en MÚLTIPLOS DE ATR (se adaptan a la
volatilidad real de cada momento). El SL sigue en % fijo (coincide con
el caso de estudio del documento).

Nota: el documento V4 no menciona salida parcial (50% en breakeven) —
a diferencia del diseño anterior, esta versión NO la incluye, para
seguir la especificación tal cual está.
"""

PCT_MARGEN_POR_ENTRADA = 0.02  # 2% del capital del ciclo, por entrada (documento: "1% o 2%")
LEVERAGE_FIJO = 20
VALOR_CONTRATO_COINM_USD = 10  # 20/09: 1 contrato de ETH-USD Coin-M = 10 USD de valor nocional (convención estándar, confirmada por minTradeValue=10 en el propio contrato de BingX)
MAX_ENTRADAS = 5
ENTRADA_ACTIVA_SALIDA_PARCIAL = 4  # 13/09: re-agregado a pedido de Juanjo (se había perdido en la reescritura V4)

# 28/09 — Directiva: progresión geométrica estricta (antes escala más
# lineal 1.5/3.5/7.5/14.0) para que la entrada 2 no dispare con el puro
# ruido de los primeros minutos de la vela de 15m (caso real: disparó a
# los 8 minutos de la entrada 1, por una caída de apenas 1.5x ATR).
# Nota: la directiva solo dio valores de "largo" — los de "corto" se
# escalaron manteniendo la misma proporción que ya tenía el diseño
# anterior (~0.8x del valor de largo); confirmar si esa proporción
# sigue siendo la deseada.
NIVELES_ENTRADA_ATR = {
    2: {"largo": 3.0, "corto": 2.4},
    3: {"largo": 5.5, "corto": 4.4},
    4: {"largo": 8.5, "corto": 6.8},
    5: {"largo": 12.0, "corto": 9.6},
}

# ── Modo Oscilación (05/10) — sub-módulo alternativo, switch de FASE ──
# No es un gate nuevo sobre V5.0: es un "desviador de tráfico". El
# switch (ciclo_seleccion, main.py) calcula ADX(1h) ANTES de elegir
# estrategia — si es alto, sigue el camino tendencial de siempre
# (V5.0, sin tocar); si es bajo, desvía el flujo exclusivamente a este
# módulo. El ADX nunca bloquea operar, solo decide qué lógica maneja
# el trade (directiva explícita de Juanjo, 05/10).
ADX_UMBRAL_OSCILACION = 22      # ADX(1h) < esto -> Fase de Materialización (switch a Oscilación)
RSI_OSCILACION_LARGO_MAX = 32   # RSI(4h) <= esto dispara entrada LARGO en Modo Oscilación
RSI_OSCILACION_CORTO_MIN = 68   # RSI(4h) >= esto dispara entrada CORTO en Modo Oscilación

# Separación de DCA del Modo Oscilación: variable LOCAL y ENCAPSULADA,
# de uso EXCLUSIVO dentro de este modo — NO modifica ni reemplaza
# NIVELES_ENTRADA_ATR (que sigue intacta para la estrategia tendencial
# cuando ADX(1h) >= ADX_UMBRAL_OSCILACION). Directiva 05/10: cada
# re-entrada requiere que el precio se mueva en contra 1.5×ATR(1h)
# desde la ÚLTIMA entrada ejecutada (no desde la entrada 1, a
# diferencia de la tabla tendencial).
DCA_OSCILACION_ATR_MULT = 1.5

SL_RETROCESO_LARGO_PCT = -51.6  # V4 (antigua) — precio crudo desde entrada 1
SL_RETROCESO_CORTO_PCT = 31.0   # V4 (antigua)

# 28/09 — Directiva: umbral preventivo LOCAL a -38.5% (antes -40.0%),
# para ganarle de mano al -40% real de BingX si el funding rate o las
# comisiones lo empujan a liquidar antes de lo esperado. El -40.0%
# como tal ya no se usa en el cálculo, queda documentado el valor real
# del exchange como referencia.
SL_MARGEN_PCT_EXCHANGE_REAL = -40.0
SL_MARGEN_PCT = -38.5  # V4.2 — colchón preventivo local, sobre el margen TOTAL invertido

TP_OFFSET_VPVR_PCT = 0.5


def calcular_pnl_pct_margen(entradas: list, precio_actual: float, direccion: str, leverage: int) -> float:
    """
    V4.1 — PNL apalancado como % del margen TOTAL invertido (sumando
    todas las entradas ejecutadas), no un % de retroceso de precio
    crudo desde la 1ra entrada (que era V4).

    Cada entrada aporta su propia ganancia/pérdida en USD según SU
    PROPIO precio de entrada (no un promedio simplificado) — la suma
    de esas ganancias en USD, dividida por el margen total invertido,
    da el PNL% real sobre el margen.
    """
    signo = 1 if direccion == "LARGO" else -1
    margen_total = sum(e["margen_usd"] for e in entradas)
    if margen_total <= 0:
        return 0.0
    ganancia_usd_total = 0.0
    for e in entradas:
        cambio_pct = (precio_actual - e["precio"]) / e["precio"] * 100
        ganancia_usd_total += (cambio_pct * signo * leverage / 100) * e["margen_usd"]
    return round(ganancia_usd_total / margen_total * 100, 4)


def precio_toca_sl_margen(entradas: list, precio_actual: float, direccion: str, leverage: int) -> bool:
    """V4.1 — cierre inmediato si el PNL apalancado cae a -40% (o peor) del margen total invertido."""
    pnl_pct = calcular_pnl_pct_margen(entradas, precio_actual, direccion, leverage)
    return pnl_pct <= SL_MARGEN_PCT


def calcular_promedio_ponderado(entradas: list):
    """entradas: lista de dicts con 'precio' y 'margen_usd' de cada entrada ya ejecutada. Usado para la salida parcial (breakeven), no para el TP (que es VPVR)."""
    total_margen = sum(e["margen_usd"] for e in entradas)
    if total_margen <= 0:
        return None
    return sum(e["precio"] * e["margen_usd"] for e in entradas) / total_margen


def precio_recupero_promedio(direccion: str, precio_actual: float, precio_promedio: float) -> bool:
    if direccion == "LARGO":
        return precio_actual >= precio_promedio
    else:
        return precio_actual <= precio_promedio


def precio_dispara_siguiente_entrada(direccion: str, precio_entrada_1: float, atr_abs: float, precio_actual: float, siguiente_n: int) -> bool:
    if siguiente_n not in NIVELES_ENTRADA_ATR or atr_abs is None or atr_abs <= 0:
        return False
    mult = NIVELES_ENTRADA_ATR[siguiente_n]["largo" if direccion == "LARGO" else "corto"]
    if direccion == "LARGO":
        umbral = precio_entrada_1 - mult * atr_abs
        return precio_actual <= umbral
    else:
        umbral = precio_entrada_1 + mult * atr_abs
        return precio_actual >= umbral


def precio_dispara_siguiente_entrada_oscilacion(direccion: str, precio_ultima_entrada: float, atr_1h: float, precio_actual: float) -> bool:
    """
    05/10 — DCA exclusivo del Modo Oscilación: separación fija de
    DCA_OSCILACION_ATR_MULT × ATR(1h), medida desde la ÚLTIMA entrada
    ejecutada (no desde la entrada 1 como la tabla tendencial
    NIVELES_ENTRADA_ATR, que esta función no usa ni toca).
    """
    if atr_1h is None or atr_1h <= 0:
        return False
    if direccion == "LARGO":
        umbral = precio_ultima_entrada - DCA_OSCILACION_ATR_MULT * atr_1h
        return precio_actual <= umbral
    else:
        umbral = precio_ultima_entrada + DCA_OSCILACION_ATR_MULT * atr_1h
        return precio_actual >= umbral


def precio_toca_sl(direccion: str, precio_entrada_1: float, precio_actual: float) -> bool:
    if direccion == "LARGO":
        umbral = precio_entrada_1 * (1 + SL_RETROCESO_LARGO_PCT / 100)
        return precio_actual <= umbral
    else:
        umbral = precio_entrada_1 * (1 + SL_RETROCESO_CORTO_PCT / 100)
        return precio_actual >= umbral


def calcular_tp_vpvr(direccion: str, hvn_precio: float) -> float:
    if direccion == "LARGO":
        return hvn_precio * (1 - TP_OFFSET_VPVR_PCT / 100)
    else:
        return hvn_precio * (1 + TP_OFFSET_VPVR_PCT / 100)


def precio_toca_tp(direccion: str, precio_actual: float, tp: float) -> bool:
    if direccion == "LARGO":
        return precio_actual >= tp
    else:
        return precio_actual <= tp
