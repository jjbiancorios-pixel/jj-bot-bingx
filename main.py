"""main.py — Bot Oscilación SHADOW. Solo observa y registra ciclos simulados (4 configuraciones en paralelo). No opera."""
import time, traceback
from datetime import datetime, timezone, timedelta
import pandas as pd
import config, db, data, engine, reporte, tg

def log(*a): print(datetime.now(db.TZ_ARG).strftime("%d/%m %H:%M:%S"), *a, flush=True)

def procesar_moneda(m):
    df, fuente = data.velas_cerradas(m)
    P = engine.prep(df)
    n = len(df)
    ult = db.ultima_vela(m)
    ts_last = str(df.index[-1])
    if ult is None:
        # arranque: no se reconstruye el pasado (evita ciclos falsos); se empieza desde la última vela cerrada
        db.set_ultima_vela(m, ts_last, fuente)
        for k in config.CONFIGS: db.estado_set(m, k, engine.nuevo_estado())
        return 0
    nuevas = [j for j in range(n) if str(df.index[j]) > ult]
    if not nuevas: return 0
    if str(df.index[nuevas[0]]) != str(pd.Timestamp(ult) + pd.Timedelta(hours=1)):
        log(f"⚠️ {m}: hueco entre {ult} y {df.index[nuevas[0]]} — se reinician sus ciclos abiertos sin contarlos")
        for k in config.CONFIGS: db.estado_set(m, k, engine.nuevo_estado())
    cerrados = 0
    for k, cfg in config.CONFIGS.items():
        st = db.estado_get(m, k) or engine.nuevo_estado()
        for j in nuevas:
            c = engine.paso(st, P, j, cfg, config.L_FLUJO)
            if c:
                pn = {L: (-100.0 if c['res'] == 'liq' else float(engine.pnl_puntos(c['ent'], c['salida'], c['side'], L, cfg['fee']))) for L in config.L_REPORTE}
                db.guardar_ciclo(k, m, c, pn[3], pn[5]); cerrados += 1
                if c['res'] in ('sl', 'liq'):
                    tg.enviar(f"🔴 SHADOW {k} {m} {'LARGO' if c['side']==1 else 'CORTO'} cerró por {c['res'].upper()} con {c['n_ent']} entradas: {pn[5]:+.1f}% margen a 5x")
        db.estado_set(m, k, st)
    db.set_ultima_vela(m, ts_last, fuente)
    return cerrados

def ciclo_horario():
    ok = cerrados = 0; fallidas = {}
    def intentar(mon):
        nonlocal ok, cerrados
        try: cerrados += procesar_moneda(mon); ok += 1; fallidas.pop(mon, None); return True
        except Exception as e: fallidas[mon] = str(e)[:90]; return False
    for mon in config.MONEDAS:
        intentar(mon); time.sleep(config.PAUSA_ENTRE_MONEDAS_S)
    if fallidas:
        log(f"primera ronda: {len(fallidas)} monedas con error, se reintentan en {config.PAUSA_SEGUNDA_RONDA_S}s")
        time.sleep(config.PAUSA_SEGUNDA_RONDA_S)
        for mon in list(fallidas):
            intentar(mon); time.sleep(config.PAUSA_ENTRE_MONEDAS_S)
    # contador de pasadas seguidas con fallo por moneda; aviso solo al llegar al umbral (una vez)
    nuevas_alertas = []
    for mon in config.MONEDAS:
        k = "fallos_" + mon
        if mon in fallidas:
            n = int(db.meta_get(k, 0)) + 1; db.meta_set(k, n)
            if n == config.ALERTA_FALLOS_SEGUIDOS: nuevas_alertas.append(f"{mon}: {fallidas[mon]}")
        elif int(db.meta_get(k, 0)) > 0: db.meta_set(k, 0)
    if nuevas_alertas:
        tg.enviar("⚠️ SHADOW: sin datos de BingX en %d pasadas seguidas para:\n%s" % (config.ALERTA_FALLOS_SEGUIDOS, "\n".join(nuevas_alertas[:10])))
    log(f"ciclo: {ok} monedas ok, {len(fallidas)} con error, {cerrados} ciclos cerrados")
    if fallidas: log("errores:", " | ".join(f"{k}: {v}" for k, v in list(fallidas.items())[:10]))
    return ok, len(fallidas), [f"{k}: {v}" for k, v in fallidas.items()]

def recolectar_mstrx():
    """Recolector aparte (solo lectura). Cualquier error se registra y NO afecta al Shadow."""
    try:
        import mstrx_datos
        avisos = mstrx_datos.recolectar()
        try:
            import mstrx_sim
            nc = mstrx_sim.procesar()
            if nc: log(f"mstrx_sim: {nc} ciclos cerrados")
        except Exception:
            log("mstrx_sim ERROR (no afecta al Shadow):", traceback.format_exc()[-400:])
        if avisos:
            k = "mstrx_aviso"; hoy = datetime.now(db.TZ_ARG).strftime("%Y%m%d%H")
            if db.meta_get(k) != hoy[:8]:
                db.meta_set(k, hoy[:8]); tg.enviar("⚠️ MSTRX datos: " + " | ".join(avisos[:4]))
        log("mstrx:", "ok" if not avisos else "; ".join(avisos[:3]))
    except Exception:
        log("mstrx ERROR (no afecta al Shadow):", traceback.format_exc()[-400:])

def manejar(cmd):
    p = cmd.split(); c = p[0].lower().split("@")[0]
    if c in ("/estado", "/resumen"):
        d = None
        if len(p) > 1 and p[1].isdigit(): d = str(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(p[1])))
        return reporte.texto_resumen(d, f"últimos {p[1]} días" if d else "acumulado")
    if c == "/abiertos": return reporte.texto_abiertos()
    if c == "/csv": tg.enviar_archivo("ciclos_shadow.csv", reporte.csv_ciclos(), "Ciclos cerrados"); return "CSV enviado."
    if c == "/mstrx":
        import mstrx_datos; return mstrx_datos.resumen()
    if c == "/mstrx_res":
        import mstrx_sim; return mstrx_sim.texto_resumen()
    if c == "/mstrx_ciclos":
        import mstrx_sim; tg.enviar_archivo("mstrx_ciclos.csv", mstrx_sim.csv_ciclos(), "Ciclos simulados MSTRX/BTC"); return "CSV de ciclos MSTRX enviado."
    if c == "/mstrx_csv":
        import mstrx_datos
        for par, tf in (("MSTRX_BTC", "15M"), ("MSTRX_BTC", "60M"), ("MSTRX_BTC", "4H"), ("BTC_USDT", "60M"), ("BTC_USDT", "4H")):
            tg.enviar_archivo(f"{par}_{tf}.csv", mstrx_datos.csv_velas(par, tf), f"{par} {tf}")
        return "CSV de MSTRX enviados."
    if c == "/ayuda": return "/resumen [días] · /abiertos · /csv · /mstrx · /mstrx_csv · /mstrx_res · /mstrx_ciclos"
    return None

def resumen_diario():
    hoy = datetime.now(db.TZ_ARG).strftime("%Y%m%d")
    if datetime.now(db.TZ_ARG).hour >= config.HORA_RESUMEN_ARG and db.meta_get("resumen_enviado") != hoy:
        d = str(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24))
        tg.enviar(reporte.texto_resumen(d, "últimas 24h") + "\n\n" + reporte.texto_resumen(None, "acumulado"))
        tg.enviar_archivo(f"ciclos_shadow_{hoy}.csv", reporte.csv_ciclos(), "Ciclos cerrados (acumulado)")
        try:
            import mstrx_sim; tg.enviar(mstrx_sim.texto_resumen())
        except Exception: log("resumen mstrx ERROR:", traceback.format_exc()[-300:])
        db.meta_set("resumen_enviado", hoy)

def main():
    db.init()
    log("Bot Oscilación SHADOW arrancando. DB:", config.DB_PATH)
    ok, fallos, errores = ciclo_horario()
    tg.enviar(f"🧪 <b>Bot Oscilación SHADOW</b> arrancó (sin dinero, sin órdenes).\nMonedas leídas: {ok}/{len(config.MONEDAS)}. Configs: A, B, C, D en paralelo (D = B + trailing).\n"
              + ("Con error: " + "; ".join(errores[:8]) if errores else "")
              + ("\nComandos: /resumen [días] /abiertos /csv" if config.ACEPTA_COMANDOS else "\nSolo envía (resumen diario 21:00 + CSV). Para comandos definí OSC_TELEGRAM_TOKEN."))
    recolectar_mstrx()
    proximo = None
    while True:
        try:
            ahora = datetime.now(timezone.utc)
            objetivo = ahora.replace(minute=0, second=25, microsecond=0)
            if objetivo <= ahora: objetivo += timedelta(hours=1)
            while datetime.now(timezone.utc) < objetivo:
                for cmd in tg.comandos():
                    r = manejar(cmd)
                    if r: tg.enviar(r)
                time.sleep(3 if config.ACEPTA_COMANDOS else 20)
            ciclo_horario(); resumen_diario(); recolectar_mstrx()
        except Exception:
            log("ERROR en bucle:", traceback.format_exc()[-600:]); time.sleep(30)

if __name__ == "__main__":
    main()
