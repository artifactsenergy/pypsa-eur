"""Aplica las potencias y la demanda del paquete de EDGE sobre la red compuesta, antes de resolver.

Uso: python railway/edge_ajustes.py <composed_AAAA.nc> <paquete.json> <año> <dir_salida>
Escribe en <dir_salida>: ajustes_<año>.json (qué se cambió y qué no se pudo) y, si hace falta,
ccl_<año>.csv + ccl_<año>.yaml (restricción de PyPSA-Eur por país y tecnología, CCL).

Reglas, por país con objetivo (España del TOML de EDGE; el resto del plan [europa]):
  - Demanda (demanda_twh): se escalan las cargas del país para que sumen esa energía en el año.
  - Generadores (solar, onwind, offwind, CCGT, OCGT, nuclear, coal, lignite, biomass):
      objetivo <= parque existente  -> se escala el existente a la baja y no se deja invertir;
      objetivo >  parque existente  -> si la tecnología es ampliable, CCL fija el total del país
                                        (el modelo elige dónde); si no lo es, se avisa y queda el existente.
  - hydro (embalse + fluyente) y PHS: se escala la potencia existente (mismas horas de embalse).
  - battery: potencia de descarga fija repartida por la carga de cada nudo y energía = potencia × horas
    (almacenamiento.baterias_horas del TOML; 4 h si no hay dato); la carga la ajusta la restricción de PyPSA-Eur.
  - solar_term, cogeneración y electrolizadores de España: sin equivalente directo; quedan en avisos.
  - Perfiles de España (si hay <dir_salida>/meteo/): los p_max_pu de onwind y solar de España se escalan para que
    cada generador tenga el factor de capacidad anual real del año meteorológico (T.Real peninsular / potencia
    instalada media, los mismos datos que usan EDGE y openTEPES). Se conserva la forma horaria y espacial de atlite.
    Desactivable con CALIBRAR_PERFILES=0.
"""
import os
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
import yaml

GRUPOS = {"solar": ["solar", "solar-hsat"], "onwind": ["onwind"],
          "offwind": ["offwind-ac", "offwind-dc", "offwind-float"],
          "CCGT": ["CCGT"], "OCGT": ["OCGT"], "nuclear": ["nuclear"], "coal": ["coal"], "lignite": ["lignite"],
          "biomass": ["biomass"]}
CCL_NOMBRE = {"solar": "solar-all", "offwind": "offwind-all"}     # con agg_solar y agg_offwind
SIN_EQUIVALENTE = ("solar_term", "cogeneracion", "electrolizadores")


def objetivos(p, anio):
    a = p["por_anio"][str(anio)]
    out = {"ES": dict(a["espana"]["potencia_mw"], demanda_twh=a["espana"]["demanda_twh"],
                      _horas_bat=a["espana"].get("almacenamiento", {}).get("baterias_horas", 4.0))}
    for pais, v in (a.get("europa") or {}).items():
        out[pais] = dict(v, _horas_bat=4.0)
    return out


def pais(n, df, col="bus"):
    return df[col].map(n.buses.country)


def ajustar(n, obj, anio):
    w = n.snapshot_weightings.generators
    informe, ccl = {}, []
    for c, t in obj.items():
        inf = informe.setdefault(c, {"cambios": {}, "avisos": []})
        if c not in set(n.buses.country):
            inf["avisos"].append("país fuera de la red del modelo: objetivos ignorados")
            continue
        # --- demanda
        if t.get("demanda_twh"):
            cols = [l for l in n.loads.index[pais(n, n.loads) == c] if l in n.loads_t.p_set.columns]
            actual = float(n.loads_t.p_set[cols].mul(w, axis=0).sum().sum()) / 1e6
            if actual > 0:
                f = float(t["demanda_twh"]) / actual
                n.loads_t.p_set[cols] *= f
                inf["cambios"]["demanda_twh"] = {"antes": round(actual, 2), "despues": round(float(t["demanda_twh"]), 2)}
        # --- generadores
        for tec, carriers in GRUPOS.items():
            if tec not in t:
                continue
            T = float(t[tec])
            g = n.generators[(pais(n, n.generators) == c) & n.generators.carrier.isin(carriers)]
            fijos, ampl = g[~g.p_nom_extendable], g[g.p_nom_extendable]
            existente = float(fijos.p_nom.sum())
            if T <= existente + 1e-6:
                if existente > 0:
                    n.generators.loc[fijos.index, "p_nom"] *= T / existente
                n.generators.loc[ampl.index, ["p_nom", "p_nom_min", "p_nom_max"]] = 0.0
                inf["cambios"][tec] = {"existente_mw": round(existente), "objetivo_mw": round(T), "modo": "escalado"}
            elif len(ampl):
                n.generators.loc[ampl.index, "p_nom_min"] = 0.0     # el CCL manda sobre los mínimos estimados
                potencial = existente + float(ampl.p_nom_max.replace(np.inf, 1e12).sum())
                T_ccl = T
                if T > potencial:   # más objetivo que potencial con esta resolución: se recorta (si no, es infactible)
                    T_ccl = potencial * 0.999
                    inf["avisos"].append("%s: objetivo %d MW > potencial del modelo %d MW con estos nudos; se fija %d MW"
                                         % (tec, T, potencial, T_ccl))
                ccl.append((c, CCL_NOMBRE.get(tec, tec), T_ccl))
                inf["cambios"][tec] = {"existente_mw": round(existente), "objetivo_mw": round(T), "modo": "CCL",
                                       "potencial_mw": round(min(potencial, 1e9)), "fijado_mw": round(T_ccl)}
                print("CCL %s %-10s objetivo %8.0f | existente %8.0f | potencial %10.0f" % (c, tec, T, existente,
                                                                                              min(potencial, 1e9)))
            else:
                inf["avisos"].append("%s: objetivo %d MW > existente %d MW y no es ampliable en esta configuración; "
                                     "queda el existente" % (tec, T, existente))
        # --- hidráulica y bombeo (StorageUnit + fluyente)
        for tec, cars in (("hydro", ["hydro"]), ("PHS", ["PHS"])):
            if tec not in t:
                continue
            su = n.storage_units[(pais(n, n.storage_units) == c) & n.storage_units.carrier.isin(cars)]
            ror = n.generators[(pais(n, n.generators) == c) & (n.generators.carrier == "ror")] if tec == "hydro" \
                else n.generators.iloc[:0]
            existente = float(su.p_nom.sum() + ror.p_nom.sum())
            if existente > 0:
                f = float(t[tec]) / existente
                n.storage_units.loc[su.index, "p_nom"] *= f
                n.generators.loc[ror.index, "p_nom"] *= f
                inf["cambios"][tec] = {"existente_mw": round(existente), "objetivo_mw": round(float(t[tec]))}
            elif float(t[tec]) > 0:
                inf["avisos"].append("%s: sin parque existente en el modelo; no se añade" % tec)
        # --- baterías
        if "battery" in t:
            T = float(t["battery"])
            su = n.storage_units[(pais(n, n.storage_units) == c) & (n.storage_units.carrier == "battery")]
            resto = max(0.0, T - float(su.p_nom.sum()))
            des = n.links[(pais(n, n.links, "bus1") == c) & (n.links.carrier == "battery discharger")]
            sto = n.stores[n.stores.bus.isin(des.bus0) & (n.stores.carrier == "battery")]   # bus de la batería
            if len(des):
                carga = n.loads_t.p_set.mul(w, axis=0).sum().groupby(n.loads.bus).sum()
                reparto = carga.reindex(des.bus1.values).fillna(0).values
                reparto = reparto / reparto.sum() if reparto.sum() > 0 else np.full(len(des), 1 / len(des))
                mw = resto * reparto
                n.links.loc[des.index, ["p_nom_min", "p_nom_max"]] = np.c_[mw, mw]
                horas = float(t.get("_horas_bat", 4.0))
                e = pd.Series(mw * horas, index=des.bus0.values)
                n.stores.loc[sto.index, "e_nom_min"] = e.reindex(sto.bus.values).fillna(0).values
                n.stores.loc[sto.index, "e_nom_max"] = e.reindex(sto.bus.values).fillna(0).values
                inf["cambios"]["battery"] = {"objetivo_mw": round(T), "existente_mw": round(float(su.p_nom.sum())),
                                             "horas": horas}
            elif T > 0:
                inf["avisos"].append("battery: el modelo no tiene baterías ampliables en %s" % c)
        for tec in SIN_EQUIVALENTE:
            if t.get(tec):
                inf["avisos"].append("%s (%d MW): sin equivalente en PyPSA-Eur; no se aplica" % (tec, t[tec]))
    return informe, ccl


def calibrar_perfiles_es(n, dir_meteo, anio_meteo):
    """Escala p_max_pu de onwind y solar de España al factor de capacidad real del año meteorológico."""
    h = pd.read_csv(dir_meteo / ("es_hourly_%d.csv" % anio_meteo))
    ins = json.load(open(dir_meteo / ("instalada_%d.json" % anio_meteo)))["peninsular"]
    objetivo = {"onwind": h["eolica"].clip(lower=0).sum() / (len(h) * float(ins["eolica"])),
                "solar": h["solar_fv"].clip(lower=0).sum() / (len(h) * float(ins["solar_fv"]))}
    w = n.snapshot_weightings.generators
    out = {}
    for tec, cf_obj in objetivo.items():
        gens = n.generators.index[(n.generators.bus.map(n.buses.country) == "ES") & n.generators.carrier.isin(GRUPOS[tec])]
        gens = [g for g in gens if g in n.generators_t.p_max_pu.columns]
        if not gens:
            continue
        pm = n.generators_t.p_max_pu[gens]
        cf0 = (pm.mul(w, axis=0).sum() / w.sum())
        cap = n.generators.loc[gens, "p_nom"].where(n.generators.loc[gens, "p_nom"] > 0, n.generators.loc[gens, "p_nom_max"].clip(upper=1e5))
        antes = float((cf0 * cap).sum() / cap.sum()) if cap.sum() > 0 else float(cf0.mean())
        nuevo = pm.copy()
        for g in gens:                      # factor por generador, iterando por el recorte en 1
            k = cf_obj / max(cf0[g], 1e-6)
            for _ in range(8):
                x = (pm[g] * k).clip(upper=1.0); cf = float((x * w).sum() / w.sum())
                if cf <= 0 or abs(cf - cf_obj) < 1e-4:
                    break
                k *= cf_obj / cf
            nuevo[g] = (pm[g] * k).clip(upper=1.0)
        n.generators_t.p_max_pu[gens] = nuevo
        out[tec] = {"cf_atlite": round(antes, 4), "cf_objetivo": round(float(cf_obj), 4), "generadores": len(gens)}
        print("perfil ES", tec, "FC atlite %.3f -> real %d %.3f" % (antes, anio_meteo, cf_obj))
    return out


def main():
    red, ruta_p, anio, salida = sys.argv[1], sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
    salida.mkdir(parents=True, exist_ok=True)
    p = json.load(open(ruta_p, encoding="utf-8"))
    n = pypsa.Network(red)
    informe, ccl = ajustar(n, objetivos(p, anio), anio)
    dir_meteo = salida / "meteo"
    if os.environ.get("CALIBRAR_PERFILES", "1") == "1" and dir_meteo.exists():
        informe.setdefault("ES", {"avisos": []})["perfiles"] = calibrar_perfiles_es(n, dir_meteo, int(p["anio_meteorologico"]))
    n.export_to_netcdf(red)
    if ccl:
        idx = pd.MultiIndex.from_tuples([(c, k) for c, k, _ in ccl], names=["country", "carrier"])
        cols = pd.MultiIndex.from_tuples([(str(anio), "min"), (str(anio), "max")])
        df = pd.DataFrame([[v, v] for *_, v in ccl], index=idx, columns=cols)
        df.to_csv(salida / ("ccl_%d.csv" % anio))
        yaml.safe_dump({"solving": {"constraints": {"CCL": True},
                                    "agg_p_nom_limits": {"file": str((salida / ("ccl_%d.csv" % anio)).resolve()),
                                                         "include_existing": True, "agg_solar": True,
                                                         "agg_offwind": True}}},
                       open(salida / ("ccl_%d.yaml" % anio), "w"))
    json.dump({"anio": anio, "paises": informe, "ccl": [list(x) for x in ccl]},
              open(salida / ("ajustes_%d.json" % anio), "w"), ensure_ascii=False, indent=1)
    print("ajustes", anio, "| CCL:", len(ccl), "| avisos:", sum(len(v["avisos"]) for v in informe.values()))


if __name__ == "__main__":
    main()
