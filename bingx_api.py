"""
bingx_api.py — Bot BingX (Coin-M, nuevo desde cero, 11/09/2026)
──────────────────────────────────────────────────────────────
Cliente para la API de BingX — Coin-M Perpetual Futures (contratos
inversos, margen y liquidación en la cripto misma: BTC-USD, ETH-USD).

Firma confirmada con múltiples fuentes independientes (documentación
oficial + ejemplos de código reales, mismo patrón usado en varios
exchanges derivados de Binance):
  1. Parámetros ordenados alfabéticamente
  2. Armar "key=value&key=value..." + "&timestamp=<ms>"
  3. Firmar ese string con HMAC-SHA256 (hex)
  4. Mandar la firma en la URL, la API key en el header X-BX-APIKEY

IMPORTANTE — primera vez con este exchange, a diferencia de Pionex
(que ya usamos y confirmamos varias veces): los endpoints de
creación/cancelación de orden y balance están confirmados por múltiples
fuentes; el endpoint exacto de PRECIO y BALANCE de Coin-M específicos
NO se pudieron confirmar con la misma certeza — hay que probarlos con
/probar_bingx antes de operar con dinero real, mismo criterio que
usamos con Pionex al principio.
"""
import os
import time
import hmac
import hashlib
import requests

BASE_URL = "https://open-api.bingx.com"
API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_API_SECRET", "")

COMISION_IDA_VUELTA_PCT = 0.10  # BingX: 0.02% maker / ~0.05% taker (estimación conservadora, no confirmada en detalle)


def _firmar(params: dict) -> str:
    """Arma el query string completo con timestamp y firma, según el patrón confirmado de BingX."""
    if not API_SECRET:
        raise RuntimeError("BINGX_API_SECRET no configurada (falta variable en Railway).")
    params = dict(params)
    params["timestamp"] = str(int(time.time() * 1000))
    query = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    firma = hmac.new(API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={firma}"


def _get(path: str, params: dict = None) -> dict:
    query = _firmar(params or {})
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        return requests.get(url, headers=headers, timeout=15).json()
    except Exception as e:
        print(f"⚠️ bingx_api GET {path}: {e}")
        return {}


def _post(path: str, params: dict = None) -> dict:
    query = _firmar(params or {})
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        return requests.post(url, headers=headers, timeout=15).json()
    except Exception as e:
        print(f"⚠️ bingx_api POST {path}: {e}")
        return {}


def _delete(path: str, params: dict = None) -> dict:
    query = _firmar(params or {})
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        return requests.delete(url, headers=headers, timeout=15).json()
    except Exception as e:
        print(f"⚠️ bingx_api DELETE {path}: {e}")
        return {}


def consultar_contrato(symbol: str) -> dict:
    """GET /cswap/v1/quote/contracts — specs del contrato (mínimos, precisión). Endpoint confirmado."""
    resp = _get("/openApi/cswap/v1/quote/contracts", {"symbol": symbol})
    data = resp.get("data", [])
    return data[0] if data else {}


def consultar_precio(symbol: str):
    """
    GET /cswap/v1/quote/ticker — precio actual. Endpoint NO confirmado
    con la misma certeza que el resto (no apareció en la documentación
    consultada con un ejemplo exacto) — VERIFICAR con /probar_bingx
    antes de confiar en esto para abrir con capital real.
    """
    resp = _get("/openApi/cswap/v1/quote/ticker", {"symbol": symbol})
    try:
        data = resp.get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}
        precio = data.get("lastPrice") or data.get("close") or data.get("price")
        return float(precio) if precio else None
    except Exception as e:
        print(f"⚠️ consultar_precio({symbol}): {e}")
        return None


def consultar_balance(moneda: str):
    """
    GET /cswap/v1/user/balance — balance de la cuenta Coin-M.
    Endpoint NO confirmado con certeza total — VERIFICAR con
    /probar_bingx antes de usar para calcular capital real.
    moneda: "BTC" o "ETH"
    """
    resp = _get("/openApi/cswap/v1/user/balance", {})
    try:
        data = resp.get("data", [])
        for b in data:
            if b.get("asset") == moneda or b.get("currency") == moneda:
                return float(b.get("balance") or b.get("availableMargin") or 0)
    except Exception as e:
        print(f"⚠️ consultar_balance({moneda}): {e}")
    return None


def crear_orden(symbol: str, side: str, position_side: str, tipo: str, quantity: float) -> dict:
    """
    POST /cswap/v1/trade/order — crea una orden real. Endpoint CONFIRMADO
    (múltiples fuentes independientes con ejemplos de código reales).
    side: "BUY" o "SELL" | position_side: "LONG" o "SHORT" | tipo: "MARKET"
    quantity: en contratos (Coin-M, no en USD ni en la cripto directamente
    — verificar el tamaño real de 1 contrato con consultar_contrato antes
    de calcular la cantidad).
    """
    params = {
        "symbol": symbol, "side": side, "positionSide": position_side,
        "type": tipo, "quantity": quantity,
    }
    return _post("/openApi/cswap/v1/trade/order", params)


def consultar_ordenes_abiertas(symbol: str = None) -> dict:
    """GET /cswap/v1/trade/openOrders — endpoint confirmado."""
    params = {"symbol": symbol} if symbol else {}
    return _get("/openApi/cswap/v1/trade/openOrders", params)


def cerrar_todas_posiciones(symbol: str) -> dict:
    """POST /cswap/v1/trade/closeAllPositions — endpoint confirmado, cierra TODO en ese símbolo."""
    return _post("/openApi/cswap/v1/trade/closeAllPositions", {"symbol": symbol})


def cerrar_parcial(symbol: str, position_side: str, quantity: float) -> dict:
    """
    12/09 — Cierre PARCIAL (para la salida del 50% en breakeven, desde
    la entrada 4/5): orden reduce-only en la dirección OPUESTA a la
    posición, por la cantidad exacta a cerrar. Usa el mismo endpoint de
    crear orden (confirmado), no el de "closeAllPositions" (que cierra
    el 100%, no serviría acá).
    """
    side = "SELL" if position_side == "LONG" else "BUY"
    params = {
        "symbol": symbol, "side": side, "positionSide": position_side,
        "type": "MARKET", "quantity": quantity, "reduceOnly": "true",
    }
    return _post("/openApi/cswap/v1/trade/order", params)


def cancelar_orden(symbol: str, order_id: str) -> dict:
    """DELETE /cswap/v1/trade/cancelOrder — endpoint confirmado."""
    return _delete("/openApi/cswap/v1/trade/cancelOrder", {"symbol": symbol, "orderId": order_id})
