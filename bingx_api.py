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


def consultar_margen_actual(symbol: str) -> dict:
    """20/09 — GET Coin-M, consulta el modo de margen actual (para verificar, no cambiar). Mismo nivel de confirmación que fijar_margen_aislado."""
    return _get("/openApi/cswap/v1/trade/marginType", {"symbol": symbol})


def consultar_margen_actual_usdtm(symbol: str) -> dict:
    """20/09 — GET USDT-M, consulta el modo de margen actual. Endpoint confirmado (mismo patrón que fijar_margen_aislado_usdtm)."""
    return _get("/openApi/swap/v2/trade/marginType", {"symbol": symbol})


def consultar_contrato(symbol: str) -> dict:
    """
    GET /cswap/v1/market/contracts — specs del contrato. FIX 14/09: el
    endpoint real es /market/contracts, NO /quote/contracts (el que
    tenía antes) — confirmado por la documentación oficial de BingX,
    causaba respuesta vacía en producción.
    """
    resp = _get("/openApi/cswap/v1/market/contracts", {"symbol": symbol})
    data = resp.get("data", [])
    return data[0] if data else {}


def consultar_precio(symbol: str):
    """
    GET /cswap/v1/market/premiumIndex — precio de referencia (markPrice)
    + funding rate. FIX 14/09: antes usaba /quote/ticker (no confirmado,
    devolvía None en producción) — /market/premiumIndex SÍ está
    confirmado en la documentación oficial.
    """
    resp = _get("/openApi/cswap/v1/market/premiumIndex", {"symbol": symbol})
    try:
        data = resp.get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}
        precio = data.get("markPrice") or data.get("indexPrice")
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


def fijar_margen_aislado(symbol: str) -> dict:
    """
    20/09 — Fija margen AISLADO para Coin-M, a pedido de Juanjo (el
    bot nunca lo configuraba, dependía de lo que estuviera puesto
    manualmente en la cuenta). Endpoint MENOS confirmado que el de
    crear orden — no encontré un ejemplo directo específico para
    Coin-M, infiero el path por el mismo patrón que USDT-M (que sí
    está confirmado). Verificar con /probar_bingx o mirando la
    respuesta cruda la primera vez que se use con capital real.
    Si BingX devuelve error porque ya hay una posición abierta (no se
    puede cambiar el modo con posición viva) o porque ya está en
    aislado, no es necesariamente un problema — igual se reporta.
    """
    params = {"symbol": symbol, "marginType": "ISOLATED"}
    return _post("/openApi/cswap/v1/trade/marginType", params)


def fijar_margen_aislado_usdtm(symbol: str) -> dict:
    """
    20/09 — Fija margen AISLADO para USDT-M. Endpoint CONFIRMADO
    (múltiples fuentes independientes, incluida documentación oficial
    referenciada por SDKs de terceros): POST /swap/v2/trade/marginType.
    """
    params = {"symbol": symbol, "marginType": "ISOLATED"}
    return _post("/openApi/swap/v2/trade/marginType", params)


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


def crear_orden_usdtm(symbol: str, side: str, position_side: str, tipo: str, quantity: float) -> dict:
    """
    13/09 — Contratos USDT-M (BTC-USDT, no BTC-USD), para las posiciones
    CORTO (prohibido usar colateral cripto según el documento V4).
    Endpoint por el mismo patrón que Coin-M pero bajo /openApi/swap/v2/
    en vez de /openApi/cswap/v1/ — MENOS confirmado que Coin-M (no
    encontré un ejemplo tan directo), verificar con /probar_bingx antes
    de operar con capital real.
    """
    params = {
        "symbol": symbol, "side": side, "positionSide": position_side,
        "type": tipo, "quantity": quantity,
    }
    return _post("/openApi/swap/v2/trade/order", params)


def consultar_balance_usdtm():
    """GET /openApi/swap/v2/user/balance — balance de la cuenta USDT-M. Verificar con /probar_bingx."""
    resp = _get("/openApi/swap/v2/user/balance", {})
    try:
        data = resp.get("data", {})
        if isinstance(data, dict) and "balance" in data:
            return float(data["balance"].get("balance", 0))
        if isinstance(data, list):
            for b in data:
                if b.get("asset") == "USDT":
                    return float(b.get("balance", 0))
    except Exception as e:
        print(f"⚠️ consultar_balance_usdtm: {e}")
    return None


def consultar_precio_usdtm(symbol: str):
    """GET /openApi/swap/v2/quote/price — precio USDT-M. Verificar con /probar_bingx."""
    resp = _get("/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        data = resp.get("data", {})
        precio = data.get("price")
        return float(precio) if precio else None
    except Exception as e:
        print(f"⚠️ consultar_precio_usdtm({symbol}): {e}")
        return None


def cerrar_todas_posiciones_usdtm(symbol: str) -> dict:
    """POST /openApi/swap/v2/trade/closeAllPositions"""
    return _post("/openApi/swap/v2/trade/closeAllPositions", {"symbol": symbol})


def cerrar_parcial_usdtm(symbol: str, position_side: str, quantity: float) -> dict:
    side = "SELL" if position_side == "LONG" else "BUY"
    params = {
        "symbol": symbol, "side": side, "positionSide": position_side,
        "type": "MARKET", "quantity": quantity, "reduceOnly": "true",
    }
    return _post("/openApi/swap/v2/trade/order", params)


def consultar_velas_usdtm(symbol: str, interval: str = "1h", limit: int = 200):
    """GET /openApi/swap/v3/quote/klines — velas USDT-M (público, sin firma). Verificar con /probar_bingx."""
    url = f"{BASE_URL}/openApi/swap/v3/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        return requests.get(url, timeout=10).json()
    except Exception as e:
        print(f"⚠️ consultar_velas_usdtm: {e}")
        return {}


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
