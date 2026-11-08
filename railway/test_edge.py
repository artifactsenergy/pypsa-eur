"""Pruebas de la integración con EDGE sobre una red sintética pequeña (sin el workflow de PyPSA-Eur).

    python -m pytest railway/test_edge.py -q      (o: python railway/test_edge.py)
"""
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd
import pypsa

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_ajustes  # noqa: E402
import edge_export  # noqa: E402
import edge_paquete  # noqa: E402


def paquete():
    return {"ejecucion": "r1", "anios": [2030], "anio_meteorologico": 2025, "europa_paises": ["FR"], "huella": "h",
            "por_anio": {"2030": {"gas_eur_mwh": 30.0, "co2_eur_t": 80.0,
                                  "espana": {"potencia_mw": {"solar": 900, "onwind": 300, "CCGT": 150, "nuclear": 50,
                                                             "battery": 100, "hydro": 120, "PHS": 0, "solar_term": 10,
                                                             "cogeneracion": 0, "electrolizadores": 0},
                                             "almacenamiento": {"baterias_horas": 2.0}, "demanda_twh": 0.04},
                                  "europa": {"FR": {"nuclear": 200, "demanda_twh": 0.03}, "PL": {"solar": 5}}}}}


def red():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-01-01", periods=48, freq="3h"))
    n.snapshot_weightings.loc[:, :] = 3.0
    for b, c in (("ES0", "ES"), ("ES1", "ES"), ("FR0", "FR")):
        n.add("Bus", b, carrier="AC", country=c)
        n.add("Bus", b + " battery", carrier="battery", country=c)
    n.add("Line", "L1", bus0="ES0", bus1="ES1", s_nom=1e4, x=0.1, r=0.01)
    n.add("Line", "L2", bus0="ES1", bus1="FR0", s_nom=300, x=0.1, r=0.01)
    h = np.arange(48) % 8
    sol = np.clip(np.sin((h - 2) / 8 * 2 * np.pi), 0, 1)
    for b in ("ES0", "ES1", "FR0"):
        n.add("Load", b + " load", bus=b, p_set=100 + 20 * (h > 5))
        n.add("Generator", b + " solar", bus=b, carrier="solar", p_nom_extendable=True, p_nom_max=1e4,
              p_max_pu=sol, capital_cost=50, marginal_cost=0)
        n.add("Generator", b + " CCGT", bus=b, carrier="CCGT", p_nom=200, marginal_cost=90)
        n.add("Generator", b + " CCGT ext", bus=b, carrier="CCGT", p_nom_extendable=True, capital_cost=100, marginal_cost=90)
        n.add("Generator", b + " nuclear", bus=b, carrier="nuclear", p_nom=150, marginal_cost=10)
        n.add("StorageUnit", b + " hydro", bus=b, carrier="hydro", p_nom=60, max_hours=100, inflow=10)
        n.add("Store", b + " battery", bus=b + " battery", carrier="battery", e_nom_extendable=True, capital_cost=10,
              e_cyclic=True)
        n.add("Link", b + " battery charger", bus0=b, bus1=b + " battery", carrier="battery charger",
              p_nom_extendable=True, efficiency=0.95, capital_cost=10)
        n.add("Link", b + " battery discharger", bus0=b + " battery", bus1=b, carrier="battery discharger",
              p_nom_extendable=True, efficiency=0.95, capital_cost=10)
    n.add("Generator", "ES0 VOLL", bus="ES0", carrier="load", p_nom=1e5, marginal_cost=3000)
    n.add("Generator", "FR0 VOLL", bus="FR0", carrier="load", p_nom=1e5, marginal_cost=3000)
    return n


def test_config():
    os.environ["CLUSTERS"] = "20"
    c = edge_paquete.configuracion(paquete(), 2030)
    assert c["snapshots"]["start"] == "2025-01-01" and c["planning_horizons"] == [2030]
    assert c["costs"]["overwrites"]["fuel"]["gas"] == 30.0 and c["costs"]["emission_prices"]["co2"] == 80.0
    assert "FR" in c["countries"] and c["clustering"]["cluster_network"]["n_clusters"] == 20
    os.environ["PAISES"] = "PT,FR"
    assert edge_paquete.configuracion(paquete(), 2030)["countries"] == ["ES", "PT", "FR"]
    os.environ.pop("PAISES")
    assert c["atlite"]["default_cutout"] == "europe-2025-sarah3-era5"
    os.environ["CUTOUT_FUENTE"] = "construir"
    c = edge_paquete.configuracion(paquete(), 2030)
    assert c["atlite"]["default_cutout"] in c["atlite"]["cutouts"] and c["data"]["cutout"]["source"] == "build"
    os.environ.pop("CUTOUT_FUENTE")


def test_ajustes_y_export():
    n = red()
    inf, ccl = edge_ajustes.ajustar(n, edge_ajustes.objetivos(paquete(), 2030), 2030)
    w = n.snapshot_weightings.generators
    es = n.loads.index[n.loads.bus.str.startswith("ES")]
    assert abs(n.loads_t.p_set[es].mul(w, axis=0).sum().sum() / 1e6 - 0.04) < 1e-9
    assert abs(n.generators.filter(like="ES", axis=0).query("carrier=='CCGT' and not p_nom_extendable").p_nom.sum() - 150) < 1e-6
    assert n.generators.loc[["ES0 CCGT ext", "ES1 CCGT ext"], "p_nom_max"].eq(0).all()
    assert abs(n.generators.loc[["ES0 nuclear", "ES1 nuclear"], "p_nom"].sum() - 50) < 1e-6
    assert abs(n.generators.loc["FR0 nuclear", "p_nom"] - 200) < 1e-6 or any("nuclear" in a for a in inf["FR"]["avisos"])
    assert ("ES", "solar-all", 900.0) in ccl
    d = n.links.loc[["ES0 battery discharger", "ES1 battery discharger"]]
    assert abs(d.p_nom_min.sum() - 100) < 1e-6 and (d.p_nom_min == d.p_nom_max).all()
    assert abs(n.stores.loc[["ES0 battery", "ES1 battery"], "e_nom_min"].sum() - 200) < 1e-6
    assert any("solar_term" in a for a in inf["ES"]["avisos"])
    # CCL a mano (como en solve_network): suma de solar ampliable de España = 900
    n.optimize(solver_name="highs", extra_functionality=lambda n, s: n.model.add_constraints(
        n.model["Generator-p_nom"].loc[["ES0 solar", "ES1 solar"]].sum() == 900, name="ccl"))
    tmp = tempfile.mkdtemp()
    ruta = os.path.join(tmp, "solved.nc"); n.export_to_netcdf(ruta)
    json.dump(paquete(), open(os.path.join(tmp, "p.json"), "w"))
    sys.argv = ["x", ruta, os.path.join(tmp, "p.json"), "2030", tmp]
    edge_export.main()
    pr = pd.read_csv(os.path.join(tmp, "series", "precio_horario_2030.csv"), index_col=0)
    assert len(pr) == 144 and {"ES", "FR"} <= set(pr.columns) and pr.index[0] == "2025-01-01T01"   # 00 UTC = 01 Madrid
    fl = pd.read_csv(os.path.join(tmp, "series", "flujos_es_2030.csv"), index_col=0)
    assert list(fl.columns) == ["FR"]
    out = json.load(open(os.path.join(tmp, "por_anio_2030.json")))
    comp = {(f["pais"], f["supuesto"]): f for f in out["comprobacion_supuestos"]}
    assert comp[("ES", "solar_mw")]["ok"] and comp[("ES", "CCGT_mw")]["ok"] and comp[("ES", "demanda_twh")]["ok"]
    assert comp[("ES", "battery_mw")]["modelo"] == 100


if __name__ == "__main__":
    test_config(); test_ajustes_y_export(); print("ok")
