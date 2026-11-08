"""Resultados de PyPSA-Eur en el formato que leen los paneles y las proyecciones de EDGE.

Uso: python railway/edge_export.py <solved_AAAA.nc> <paquete.json> <año> <dir_salida> [ajustes_AAAA.json]
Escribe en <dir_salida>/series/:
  precio_horario_<año>.csv  'YYYY-MM-DDTHH' (hora de Madrid, año meteorológico) y una columna por país (€/MWh):
                            media de los precios marginales de sus nudos ponderada por la carga; las horas que
                            agrupa la resolución temporal (p. ej. 3h) repiten el mismo valor.
  flujos_es_<año>.csv       flujo neto de cada vecino hacia España (MW, + = España importa)
  mix_<año>.csv             país, tecnología, potencia (MW) y energía (TWh)
y <dir_salida>/por_anio_<año>.json con los indicadores del año y la comprobación de supuestos
(lo que el modelo tiene frente a lo que pedía el paquete).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

TOLERANCIA = 0.02     # desvío relativo admitido entre el modelo y el paquete en la comprobación


def precio_por_pais(n):
    ac = n.buses[n.buses.carrier == "AC"]
    mp = n.buses_t.marginal_price[ac.index]
    carga = n.loads_t.p_set.T.groupby(n.loads.bus).sum().T.reindex(columns=ac.index).fillna(0)
    out = {}
    for c, buses in ac.groupby("country").groups.items():
        w = carga[buses].sum()
        out[c] = (mp[buses] * (w / w.sum())).sum(axis=1) if w.sum() > 0 else mp[buses].mean(axis=1)
    return pd.DataFrame(out)


def a_horario(df):
    """Índice UTC de la resolución del modelo -> horas de Madrid 'YYYY-MM-DDTHH' (relleno hacia delante)."""
    idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
    h = pd.date_range(idx[0], idx[-1] + (idx[1] - idx[0]) - pd.Timedelta("1h"), freq="1h")
    d = df.set_axis(idx).reindex(h, method="ffill")
    d.index = d.index.tz_convert("Europe/Madrid").strftime("%Y-%m-%dT%H")
    return d[~d.index.duplicated()]


def flujos_espana(n):
    out = {}
    for comp in ("Line", "Link"):
        df = n.static(comp) if hasattr(n, "static") else n.df(comp)
        if comp == "Link":
            df = df[df.carrier.isin(["DC", "AC"])] if "carrier" in df else df
        p0 = n.dynamic(comp)["p0"] if hasattr(n, "dynamic") else n.pnl(comp)["p0"]
        c0, c1 = df.bus0.map(n.buses.country), df.bus1.map(n.buses.country)
        for i in df.index[(c0 == "ES") ^ (c1 == "ES")]:
            vecino = c1[i] if c0[i] == "ES" else c0[i]
            f = p0[i] if i in p0 else pd.Series(0.0, index=n.snapshots)
            entra = -f if c0[i] == "ES" else f        # p0 > 0: sale de bus0
            out[vecino] = out.get(vecino, 0) + entra
    return pd.DataFrame(out, index=n.snapshots)


def mw(x):
    """Potencia en MW redondeada; los generadores de corte de carga tienen p_nom infinito y cuentan como 0."""
    v = pd.to_numeric(x, errors="coerce")
    return int(round(float(v[np.isfinite(v)].sum())))


def mix(n):
    w = n.snapshot_weightings.generators
    filas = []
    g = n.generators.assign(pais=n.generators.bus.map(n.buses.country))
    e = n.generators_t.p.mul(w, axis=0).sum()
    for (c, car), x in g.groupby(["pais", "carrier"]):
        filas.append({"pais": c, "tecnologia": car, "potencia_mw": mw(x.p_nom_opt),
                      "energia_twh": round(float(e.reindex(x.index).fillna(0).sum()) / 1e6, 2)})
    s = n.storage_units.assign(pais=n.storage_units.bus.map(n.buses.country))
    es = n.storage_units_t.p.clip(lower=0).mul(w, axis=0).sum() if len(s) else pd.Series(dtype=float)
    for (c, car), x in s.groupby(["pais", "carrier"]):
        filas.append({"pais": c, "tecnologia": car, "potencia_mw": mw(x.p_nom_opt),
                      "energia_twh": round(float(es.reindex(x.index).fillna(0).sum()) / 1e6, 2)})
    d = n.links[n.links.carrier == "battery discharger"]
    if len(d):
        ed = n.links_t.p1[d.index].mul(-1).clip(lower=0).mul(w, axis=0).sum() if len(n.links_t.p1.columns) else 0
        for c, x in d.assign(pais=d.bus1.map(n.buses.country)).groupby("pais"):
            filas.append({"pais": c, "tecnologia": "battery", "potencia_mw": mw(x.p_nom_opt),
                          "energia_twh": round(float(ed.reindex(x.index).fillna(0).sum()) / 1e6, 2)})
    return pd.DataFrame(filas).sort_values(["pais", "energia_twh"], ascending=[True, False])


def comprobar(n, mx, p, anio):
    """Modelo frente a paquete: España y países del plan [europa]."""
    from edge_ajustes import GRUPOS, objetivos     # mismo directorio
    w = n.snapshot_weightings.generators
    filas = []
    en_red = set(n.buses.country)
    for c, t in objetivos(p, anio).items():
        if c not in en_red:            # país del plan fuera del modelo: no se comprueba
            continue
        m = mx[mx.pais == c]
        for tec, cars in list(GRUPOS.items()) + [("hydro", ["hydro", "ror"]), ("PHS", ["PHS"]), ("battery", ["battery"])]:
            if tec in t:
                v = float(m[m.tecnologia.isin(cars)].potencia_mw.sum())
                filas.append({"pais": c, "supuesto": tec + "_mw", "paquete": round(float(t[tec])), "modelo": round(v)})
        if t.get("demanda_twh"):
            cols = [l for l in n.loads.index[n.loads.bus.map(n.buses.country) == c] if l in n.loads_t.p_set]
            v = float(n.loads_t.p_set[cols].mul(w, axis=0).sum().sum()) / 1e6
            filas.append({"pais": c, "supuesto": "demanda_twh", "paquete": t["demanda_twh"], "modelo": round(v, 1)})
    for f in filas:
        ref = max(abs(f["paquete"]), 1.0)
        f["ok"] = abs(f["modelo"] - f["paquete"]) <= TOLERANCIA * ref + 1.0
    return filas


def main():
    red, ruta_p, anio, salida = sys.argv[1], sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
    ajustes = json.load(open(sys.argv[5])) if len(sys.argv) > 5 and Path(sys.argv[5]).exists() else {}
    (salida / "series").mkdir(parents=True, exist_ok=True)
    p = json.load(open(ruta_p, encoding="utf-8"))
    n = pypsa.Network(red)
    pr = a_horario(precio_por_pais(n))
    pr.round(2).to_csv(salida / "series" / ("precio_horario_%d.csv" % anio), index_label="hora")
    fl = flujos_espana(n)
    a_horario(fl).round(1).to_csv(salida / "series" / ("flujos_es_%d.csv" % anio), index_label="hora")
    mx = mix(n)
    mx.to_csv(salida / "series" / ("mix_%d.csv" % anio), index=False)
    w = n.snapshot_weightings.generators
    comp = comprobar(n, mx, p, anio)
    ind = {"precio_medio_" + c: round(float(pr[c].mean()), 2) for c in ("ES", "FR", "PT") if c in pr}
    ind["importacion_neta_es_twh"] = round(float(fl.sum(axis=1).mul(w).sum()) / 1e6, 2) if len(fl.columns) else 0.0
    ind["coste_total_meur"] = round(float(n.objective) / 1e6, 1) if n.objective is not None else None
    # Energía no suministrada (generadores de corte de carga, carrier "load" en PyPSA-Eur)
    ens = mx[mx.tecnologia.astype(str).str.contains("load", case=False)]
    ind["energia_no_suministrada_twh"] = round(float(ens.energia_twh.sum()), 3)
    ind["energia_no_suministrada_es_twh"] = round(float(ens[ens.pais == "ES"].energia_twh.sum()), 3)
    ind["supuestos_ok"] = all(f["ok"] for f in comp)
    ind["supuestos_desviados"] = sum(1 for f in comp if not f["ok"])
    out = {"indicadores": ind, "comprobacion_supuestos": comp,
           "avisos": [("%s: %s" % (c, a)) for c, v in ajustes.get("paises", {}).items() for a in v.get("avisos", [])],
           "ajustes": ajustes}
    json.dump(out, open(salida / ("por_anio_%d.json" % anio), "w"), ensure_ascii=False, indent=1, default=float)
    print("edge_export", anio, ind)


if __name__ == "__main__":
    main()
