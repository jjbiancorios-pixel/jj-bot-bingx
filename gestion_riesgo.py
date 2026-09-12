"""
gestion_riesgo.py — Bot BingX (ETH, rediseño 12/09/2026)
──────────────────────────────────────────────────────────
Basado en "Análisis Inversión Futuros COIN-M-v2.pdf" (documento de
referencia cargado por Juanjo, tomado como guía — con nuestra propia
impronta donde hizo falta, ej. el TP se calcula con el precio promedio
ponderado REAL de las entradas ejecutadas, no con la tabla ilustrativa
del documento, que no pudimos reproducir exactamente).

Estructura: hasta 5 entradas escalonadas por ciclo (martingala inversa/
DCA), TP recalculado en cada entrada nueva sobre el precio promedio
ponderado, SL real a los niveles del documento. Salida parcial (50% en
breakeven del promedio) activada desde la entrada 4, confirmada por
Juanjo el 12/09.
"""

PCT_MARGEN_POR_ENTRADA = 0.02  # 2% del capital del ciclo, por entrada
LEVERAGE_FIJO = 20
TP_PCT_SOBRE_PROMEDIO = 2.5  # confirmado por Juanjo (rango del documento: 2.0-3.5%)
MAX_ENTRADAS = 5
ENTRADA_ACTIVA_SALIDA_PARCIAL = 4  # desde esta entrada en adelante, aplica 50%/50%

# (n_entrada, retroceso_pct desde el precio de la 1ra entrada)
# LARGO: precio BAJA ese % -> dispara la siguiente entrada
# CORTO: precio SUBE ese % -> dispara la siguiente entrada
NIVELES_ENTRADA = {
    1: {"largo": 0.0, "corto": 0.0},
    2: {"largo": -2.0, "corto": 1.5},
    3: {"largo": -6.0, "corto": 4.5},
    4: {"largo": -14.0, "corto": 10.5},
    5: {"largo": -30.0, "corto": 22.5},
}

SL_RETROCESO_LARGO_PCT = -51.6  # desde el precio de la 1ra entrada
SL_RETROCESO_CORTO_PCT = 31.0


def precio_dispara_siguiente_entrada(direccion: str, precio_entrada_1: float, precio_actual: float, siguiente_n: int) -> bool:
    if siguiente_n not in NIVELES_ENTRADA:
        return False
    nivel = NIVELES_ENTRADA[siguiente_n]
    if direccion == "LARGO":
        umbral = precio_entrada_1 * (1 + nivel["largo"] / 100)
        return precio_actual <= umbral
    else:
        umbral = precio_entrada_1 * (1 + nivel["corto"] / 100)
        return precio_actual >= umbral


def precio_toca_sl(direccion: str, precio_entrada_1: float, precio_actual: float) -> bool:
    if direccion == "LARGO":
        umbral = precio_entrada_1 * (1 + SL_RETROCESO_LARGO_PCT / 100)
        return precio_actual <= umbral
    else:
        umbral = precio_entrada_1 * (1 + SL_RETROCESO_CORTO_PCT / 100)
        return precio_actual >= umbral


def calcular_promedio_ponderado(entradas: list):
    """entradas: lista de dicts con 'precio' y 'margen_usd' de cada entrada ya ejecutada."""
    total_margen = sum(e["margen_usd"] for e in entradas)
    if total_margen <= 0:
        return None
    return sum(e["precio"] * e["margen_usd"] for e in entradas) / total_margen


def calcular_tp(direccion: str, precio_promedio: float) -> float:
    if direccion == "LARGO":
        return precio_promedio * (1 + TP_PCT_SOBRE_PROMEDIO / 100)
    else:
        return precio_promedio * (1 - TP_PCT_SOBRE_PROMEDIO / 100)


def precio_toca_tp(direccion: str, precio_actual: float, tp: float) -> bool:
    if direccion == "LARGO":
        return precio_actual >= tp
    else:
        return precio_actual <= tp


def precio_recupero_promedio(direccion: str, precio_actual: float, precio_promedio: float) -> bool:
    """Para la salida parcial: ¿el precio ya volvió a tocar el promedio (breakeven)?"""
    if direccion == "LARGO":
        return precio_actual >= precio_promedio
    else:
        return precio_actual <= precio_promedio
