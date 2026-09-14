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
MAX_ENTRADAS = 5
ENTRADA_ACTIVA_SALIDA_PARCIAL = 4  # 13/09: re-agregado a pedido de Juanjo (se había perdido en la reescritura V4)

# Múltiplos de ATR desde el precio de la 1ra entrada (caso de estudio del documento)
NIVELES_ENTRADA_ATR = {
    2: {"largo": 1.5, "corto": 1.2},
    3: {"largo": 3.5, "corto": 2.8},
    4: {"largo": 7.0, "corto": 5.5},
    5: {"largo": 12.0, "corto": 8.5},
}

SL_RETROCESO_LARGO_PCT = -51.6
SL_RETROCESO_CORTO_PCT = 31.0

TP_OFFSET_VPVR_PCT = 0.5


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
