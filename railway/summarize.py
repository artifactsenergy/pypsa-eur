"""Resumen de una red PyPSA resuelta, impreso en stdout (para leerlo en los logs de Railway).

Uso: python railway/summarize.py results/<run>/networks/solved_<año>.nc
Cada línea de datos empieza por "RES|" para poder filtrarla en los logs.
"""

import sys

import numpy as np
import pandas as pd
import pypsa

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 200)
pd.set_option("display.max_columns", 30)


def out(title, obj):
    print(f"RES|=== {title} ===", flush=True)
    text = obj.to_string() if hasattr(obj, "to_string") else str(obj)
    for line in text.splitlines():
        print(f"RES|{line}", flush=True)


def country_of(n, buses):
    return n.buses.loc[buses, "country"].values


def main(path):
    n = pypsa.Network(path)
    w = n.snapshot_weightings.generators
    wo = n.snapshot_weightings.objective
    hours = w.sum()

    # --- Generadores -----------------------------------------------------
    g = n.generators.copy()
    g["country"] = country_of(n, g.bus)
    g_energy = (n.generators_t.p.mul(w, axis=0)).sum()  # MWh
    g["gen_TWh"] = g_energy.reindex(g.index).fillna(0) / 1e6
    avail = n.get_switchable_as_dense("Generator", "p_max_pu").mul(g.p_nom_opt, axis=1)
    vres = n.generators_t.p_max_pu.columns
    avail = avail[vres]
    curt = (avail - n.generators_t.p.reindex(columns=vres, fill_value=0)).clip(lower=0)
    g["curt_TWh"] = curt.mul(w, axis=0).sum().reindex(g.index).fillna(0) / 1e6
    g["capex_MEUR"] = g.capital_cost * g.p_nom_opt / 1e6
    g["opex_MEUR"] = (
        n.generators_t.p.mul(wo, axis=0)
        .mul(n.get_switchable_as_dense("Generator", "marginal_cost"))
        .sum()
        .reindex(g.index)
        .fillna(0)
        / 1e6
    )

    # --- Almacenamiento (StorageUnit: hidro de bombeo, embalse, baterías…) ---
    s = n.storage_units.copy()
    s["country"] = country_of(n, s.bus)
    if len(s):
        p = n.storage_units_t.p.reindex(columns=s.index, fill_value=0)
        s["gen_TWh"] = p.clip(lower=0).mul(w, axis=0).sum() / 1e6
        s["charge_TWh"] = (-p.clip(upper=0)).mul(w, axis=0).sum() / 1e6
        s["energy_GWh"] = s.p_nom_opt * s.max_hours / 1e3
        s["capex_MEUR"] = s.capital_cost * s.p_nom_opt / 1e6

    # --- Tabla por tecnología -------------------------------------------
    gt = g.groupby("carrier").agg(
        existing_GW=("p_nom", lambda x: x.sum() / 1e3),
        optimal_GW=("p_nom_opt", lambda x: x.sum() / 1e3),
        gen_TWh=("gen_TWh", "sum"),
        curt_TWh=("curt_TWh", "sum"),
        capex_MEUR=("capex_MEUR", "sum"),
        opex_MEUR=("opex_MEUR", "sum"),
    )
    gt["CF_%"] = 100 * gt.gen_TWh * 1e6 / (gt.optimal_GW * 1e3 * hours)
    out("Generacion por tecnologia (ES+PT)", gt.round(2).sort_values("gen_TWh", ascending=False))

    if len(s):
        st = s.groupby("carrier").agg(
            existing_GW=("p_nom", lambda x: x.sum() / 1e3),
            optimal_GW=("p_nom_opt", lambda x: x.sum() / 1e3),
            energy_GWh=("energy_GWh", "sum"),
            gen_TWh=("gen_TWh", "sum"),
            charge_TWh=("charge_TWh", "sum"),
            capex_MEUR=("capex_MEUR", "sum"),
        )
        out("StorageUnits por tecnologia", st.round(2))

    # --- Stores y Links (baterías/H2 modelados como store + conversores) ---
    if len(n.stores):
        sto = n.stores.copy()
        sto["country"] = country_of(n, sto.bus)
        sto["capex_MEUR"] = sto.capital_cost * sto.e_nom_opt / 1e6
        out(
            "Stores (energia, GWh)",
            sto.groupby(["carrier", "country"]).agg(
                e_nom_opt_GWh=("e_nom_opt", lambda x: x.sum() / 1e3), capex_MEUR=("capex_MEUR", "sum")
            ).round(2),
        )
    if len(n.links):
        lk = n.links.copy()
        lk["c0"] = country_of(n, lk.bus0)
        lk["c1"] = country_of(n, lk.bus1)
        lk["capex_MEUR"] = lk.capital_cost * lk.p_nom_opt / 1e6
        out(
            "Links por tipo (potencia, GW)",
            lk.groupby("carrier").agg(
                existing_GW=("p_nom", lambda x: x.sum() / 1e3),
                optimal_GW=("p_nom_opt", lambda x: x.sum() / 1e3),
                capex_MEUR=("capex_MEUR", "sum"),
            ).round(2),
        )

    # --- Capacidad y generación por país ------------------------------------
    cap_c = g.pivot_table(index="carrier", columns="country", values="p_nom_opt", aggfunc="sum").fillna(0) / 1e3
    gen_c = g.pivot_table(index="carrier", columns="country", values="gen_TWh", aggfunc="sum").fillna(0)
    out("Capacidad optima de generacion por pais (GW)", cap_c.round(2))
    out("Generacion por pais (TWh)", gen_c.round(2))
    if len(s):
        out(
            "StorageUnits por pais (GW)",
            (s.pivot_table(index="carrier", columns="country", values="p_nom_opt", aggfunc="sum").fillna(0) / 1e3).round(2),
        )

    # --- Demanda y precios -------------------------------------------------
    loads = n.loads.copy()
    loads["country"] = country_of(n, loads.bus)
    lp = n.loads_t.p.reindex(columns=loads.index, fill_value=0)
    price = n.buses_t.marginal_price.reindex(columns=loads.bus.values)
    price.columns = loads.index
    rows = []
    for c, idx in loads.groupby("country").groups.items():
        e = lp[idx].mul(w, axis=0)
        cost = (e * price[idx]).sum().sum()
        rows.append(
            {
                "country": c,
                "demand_TWh": e.sum().sum() / 1e6,
                "peak_GW": lp[idx].sum(axis=1).max() / 1e3,
                "price_load_weighted_EUR_MWh": cost / e.sum().sum(),
                "price_time_avg_EUR_MWh": price[idx].mean(axis=1).mul(w).sum() / hours,
            }
        )
    out("Demanda y precio marginal por pais", pd.DataFrame(rows).set_index("country").round(2))

    # Distribución de precios por país (media simple de nodos)
    pc = {}
    for c, idx in loads.groupby("country").groups.items():
        series = price[idx].mean(axis=1)
        pc[c] = series.describe(percentiles=[0.05, 0.25, 0.5, 0.75, 0.95])
        pc[c]["horas_precio<1"] = w[series < 1].sum()
    out("Distribucion del precio marginal (EUR/MWh; horas ponderadas)", pd.DataFrame(pc).round(2))

    # Precio medio por nodo
    bus_price = n.buses_t.marginal_price.mul(w, axis=0).sum() / hours
    bus_tab = pd.DataFrame(
        {"country": n.buses.country, "x": n.buses.x, "y": n.buses.y, "price_EUR_MWh": bus_price}
    ).dropna(subset=["price_EUR_MWh"])
    bus_tab = bus_tab[n.buses.carrier.reindex(bus_tab.index) == "AC"]
    out("Precio medio por nodo AC", bus_tab.round(2))

    # --- Intercambios entre países ------------------------------------------
    rows = []
    lines = n.lines.copy()
    lines["c0"] = country_of(n, lines.bus0)
    lines["c1"] = country_of(n, lines.bus1)
    for name, l in lines[lines.c0 != lines.c1].iterrows():
        f = n.lines_t.p0.get(name, pd.Series(0.0, index=n.snapshots)).mul(w)
        rows.append(
            dict(element=f"line {name}", c0=l.c0, c1=l.c1, existing_GW=l.s_nom / 1e3, optimal_GW=l.s_nom_opt / 1e3,
                 flow_c0_to_c1_TWh=f.clip(lower=0).sum() / 1e6, flow_c1_to_c0_TWh=-f.clip(upper=0).sum() / 1e6)
        )
    if len(n.links):
        for name, l in lk[(lk.c0 != lk.c1) & (lk.carrier == "DC")].iterrows():
            f = n.links_t.p0.get(name, pd.Series(0.0, index=n.snapshots)).mul(w)
            rows.append(
                dict(element=f"link {name}", c0=l.c0, c1=l.c1, existing_GW=l.p_nom / 1e3, optimal_GW=l.p_nom_opt / 1e3,
                     flow_c0_to_c1_TWh=f.clip(lower=0).sum() / 1e6, flow_c1_to_c0_TWh=-f.clip(upper=0).sum() / 1e6)
            )
    if rows:
        out("Interconexiones transfronterizas", pd.DataFrame(rows).set_index("element").round(3))

    # --- Red interna ------------------------------------------------------
    ln = lines.copy()
    ln["length_x_s_nom"] = ln.length * ln.s_nom / 1e6
    ln["length_x_s_nom_opt"] = ln.length * ln.s_nom_opt / 1e6
    out(
        "Red AC (TW·km)",
        pd.Series({"existing_TWkm": ln.length_x_s_nom.sum(), "optimal_TWkm": ln.length_x_s_nom_opt.sum(),
                   "capex_MEUR": (ln.capital_cost * (ln.s_nom_opt - ln.s_nom)).sum() / 1e6}).round(3),
    )

    # --- Emisiones y restricciones globales ------------------------------
    eff = g.efficiency.replace(0, np.nan)
    co2_t = (g.gen_TWh * 1e6 / eff * g.carrier.map(n.carriers.co2_emissions).fillna(0)).sum()
    out("Emisiones CO2 de generacion (Mt)", pd.Series({"co2_Mt": co2_t / 1e6}).round(3))
    if len(n.global_constraints):
        out("Restricciones globales (mu = precio sombra)", n.global_constraints[["type", "constant", "mu"]].round(3))

    # --- Totales ---------------------------------------------------------
    tot = pd.Series(
        {
            "objective_MEUR": n.objective / 1e6,
            "snapshots": len(n.snapshots),
            "weighted_hours": hours,
            "n_buses_AC": int((n.buses.carrier == "AC").sum()),
        }
    )
    out("Totales", tot.round(2))
    print("RES|=== FIN ===", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
