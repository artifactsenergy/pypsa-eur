# PyPSA-Eur en Railway lanzado desde EDGE (v0.62 de EDGE Escenarios)

El servicio `pypsa-eur` arranca con `railway/job.sh` (imagen `Dockerfile.railway`). Si EDGE ha fijado `PAQUETE_KEY`,
el trabajo usa los supuestos comunes del escenario de EDGE; si no, ejecuta el snakemake de `SNAKE_*` como antes.

Por cada año del paquete:

1. `edge_paquete.py`: configuración con el año meteorológico común (snapshots y cutout), un único horizonte, gas
   (`costs.overwrites.fuel.gas`) y CO2 (`emission_prices`), países (España, Portugal, Francia y su entorno más los
   del plan `[europa]`), nudos (`CLUSTERS`) y resolución (`RESOLUCION`).
2. snakemake hasta `compose_network`.
3. `edge_ajustes.py`: sobre la red compuesta fija la demanda y las potencias de España y del plan europeo (escalado
   del parque existente, restricción CCL de PyPSA-Eur para lo ampliable, baterías con potencia y horas fijas).
   Lo que no se puede aplicar queda en `ajustes_<año>.json` y en los avisos.
4. snakemake resuelve; `edge_export.py` publica `precio_horario_<año>.csv` (hora de Madrid), `flujos_es_<año>.csv`,
   `mix_<año>.csv` y la **comprobación de supuestos** (modelo frente a paquete).

Resultados en el bucket, `runs/pypsa/<id>/`. Los cutouts van al disco temporal del contenedor y se descargan en cada ejecución; con `CUTOUT_EN_VOLUMEN=1` se guardan en el volumen (hace falta ampliarlo: un cutout de Europa de un año no cabe en 5 GB).

## Puesta en marcha

- Quitar el *Custom Start Command* del servicio (hoy ejecuta `railway/summarize.py`): manda el `CMD` de la imagen.
- Variables: `BUCKET`, `ENDPOINT`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`, `REGION` (las del bucket de openTEPES).
- `CUTOUT_FUENTE=construir` necesita la API de Copernicus CDS (`CDSAPI_URL` y `CDSAPI_KEY` o `~/.cdsapirc`);
  con `archivo` se descarga el cutout pre-construido `europe-<año>-sarah3-era5`, que puede no existir para todos los años.
- Memoria: con 8 GB, empezar con pocos nudos (20-37) y 3h; Europa con más nudos puede no caber.

    python railway/test_edge.py
