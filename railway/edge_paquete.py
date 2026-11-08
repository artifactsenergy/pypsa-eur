"""Paquete de supuestos de EDGE -> configuración de PyPSA-Eur (v0.62 de EDGE).

Uso: python railway/edge_paquete.py config <paquete.json> <año> <salida.yaml>
     python railway/edge_paquete.py anios <paquete.json>          (imprime los años)

Qué fija el paquete en PyPSA-Eur:
  - años meteorológico y de planificación: snapshots del año meteorológico común; un horizonte por ejecución
    (la previsión 'overnight' de PyPSA-Eur solo admite uno);
  - países: PAISES (opción del panel de EDGE) o, por defecto, España, Portugal, Francia y su entorno;
  - gas y CO2: los congelados por EDGE (costs.overwrites.fuel.gas y emission_prices.co2);
  - potencias y demanda de España y del plan europeo: NO van en la configuración; las aplica
    railway/edge_ajustes.py sobre la red compuesta antes de resolver (ver allí).
Opciones del trabajo (variables de entorno que fija EDGE): CLUSTERS, RESOLUCION, CUTOUT_FUENTE, PAISES.
"""
import json
import os
import sys

import yaml

PAISES_BASE = ["ES", "PT", "FR", "BE", "DE", "CH", "IT", "GB", "LU", "NL"]   # Francia y su entorno inmediato


def cutout(anio_meteo, fuente):
    """(nombre del cutout, entrada de atlite.cutouts o None).
    archivo: nombre del patrón de los cutouts pre-construidos de PyPSA ('europe-AAAA-sarah3-era5'); si el
             archivo de PyPSA no tiene ese año, la regla retrieve_cutout falla y hay que usar 'construir'.
    construir: cutout solo ERA5 (SARAH necesita descarga manual) con la API de Copernicus CDS
             (fichero ~/.cdsapirc o variables CDSAPI_URL y CDSAPI_KEY en el servicio)."""
    if fuente == "construir":
        nombre = "europe-%d-era5" % anio_meteo
        return nombre, {nombre: {"module": "era5", "x": [-12.0, 42.0], "y": [33.0, 72.0], "dx": 0.3, "dy": 0.3,
                                  "time": [str(anio_meteo), str(anio_meteo)], "chunks": {"time": 500},
                                  "prepare_kwargs": {"features": ["influx", "temperature", "wind", "height", "runoff"],
                                                     "monthly_requests": True, "tmpdir": "./cutouts_tmp/"}}}
    return "europe-%d-sarah3-era5" % anio_meteo, None


def configuracion(p, anio):
    a = p["por_anio"][str(anio)]
    met = int(p["anio_meteorologico"])
    # Países del modelo: PAISES (lo fija EDGE desde el panel) o España, Portugal, Francia y su entorno. Los países
    # del plan [europa] que queden fuera se ignoran (con aviso): un conjunto con toda Europa no fuerza un modelo
    # de toda Europa, que no cabe en la memoria del servicio.
    paises = os.environ.get("PAISES", "").split(",") if os.environ.get("PAISES") else list(PAISES_BASE)
    if "ES" not in paises:
        paises = ["ES"] + paises
    fuente = os.environ.get("CUTOUT_FUENTE", "archivo")
    nombre, entrada = cutout(met, "construir" if fuente == "construir" else "archivo")
    cfg = {
        "run": {"name": "edge-" + p.get("ejecucion", "manual"), "disable_progressbar": True,
                "shared_resources": {"policy": False}},
        "countries": [x.strip() for x in paises if x.strip()],
        "snapshots": {"start": "%d-01-01" % met, "end": "%d-01-01" % (met + 1), "inclusive": "left"},
        "planning_horizons": [int(anio)],
        "foresight": "overnight",
        "sector": {"enabled": False},
        "atlite": {"default_cutout": nombre},
        "clustering": {"temporal": {"averaging": os.environ.get("RESOLUCION", "3h")},
                       "cluster_network": {"n_clusters": int(os.environ.get("CLUSTERS", "37"))}},
        "costs": {"overwrites": {"fuel": {"gas": float(a["gas_eur_mwh"])}},
                  "emission_prices": {"enable": True, "co2": float(a["co2_eur_t"])}},
        "solving": {"solver": {"name": "highs", "options": "highs-default"}},
    }
    if entrada:
        cfg["atlite"]["cutouts"] = entrada
        cfg["data"] = {"cutout": {"source": "build"}}
    return cfg


def main():
    orden, ruta = sys.argv[1], sys.argv[2]
    p = json.load(open(ruta, encoding="utf-8"))
    if orden == "anios":
        print(" ".join(map(str, p["anios"])))
    elif orden == "config":
        anio, salida = int(sys.argv[3]), sys.argv[4]
        with open(salida, "w", encoding="utf-8") as f:
            yaml.safe_dump(configuracion(p, anio), f, allow_unicode=True, sort_keys=False)
        print("configuración", salida)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
