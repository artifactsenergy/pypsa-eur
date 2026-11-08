#!/usr/bin/env bash
# Trabajo de Railway lanzado desde EDGE (v0.62). Sin PAQUETE_KEY ejecuta el comando antiguo (SNAKE_*).
# EDGE fija RUN_ID, PAQUETE_KEY, RUN_PREFIX, CLUSTERS, RESOLUCION y CUTOUT_FUENTE y vuelve a desplegar.
# Por cada año del paquete:
#   1. configuración desde el paquete (año meteorológico, gas, CO2, países, nudos, resolución, cutout);
#   2. snakemake hasta compose_network;
#   3. edge_ajustes.py fija sobre la red compuesta las potencias y la demanda de España y del plan [europa];
#   4. snakemake resuelve (con la restricción CCL si hace falta) y edge_export.py saca series y comprobaciones.
# Al final junta el resumen, lo sube todo a RUN_PREFIX y deja estado.json = terminado (o error).
set -uo pipefail
cd /app
if [[ -z "${PAQUETE_KEY:-}" ]]; then
  # EDGE_SOLO=1: el servicio solo corre trabajos lanzados desde EDGE (un despliegue sin paquete no calcula nada)
  if [[ "${EDGE_SOLO:-0}" == "1" ]]; then echo "Sin PAQUETE_KEY: esperando un lanzamiento desde EDGE."; exit 0; fi
  exec bash -lc "pixi run -e default snakemake --cores ${SNAKE_CORES:-all} ${SNAKE_TARGET} --configfile ${SNAKE_CONFIG} --rerun-incomplete ${SNAKE_EXTRA:-}"
fi
export EDGE_DIR=/app/edge
E=$EDGE_DIR; mkdir -p $E/logs; LOG=$E/logs/run.log
B(){ python3 railway/edge_bucket.py "$@"; }
estado(){ B estado "$@" || true; }
falla(){ estado error "$1" "$(tail -c 1500 $LOG 2>/dev/null)"; B resumen $E >/dev/null 2>&1; B subir $E || true; exit 1; }
PX(){ pixi run -e default "$@"; }

# Clave de Copernicus CDS (solo para CUTOUT_FUENTE=construir): CDSAPI_URL y CDSAPI_KEY -> ~/.cdsapirc
if [[ -n "${CDSAPI_KEY:-}" ]]; then
  printf 'url: %s\nkey: %s\n' "${CDSAPI_URL:-https://cds.climate.copernicus.eu/api}" "$CDSAPI_KEY" > ~/.cdsapirc
fi
# Cutouts y datos descargados en el volumen (/app/results) para no bajarlos en cada ejecución.
mkdir -p /app/results/_cache/cutout data
[[ -e data/cutout ]] || ln -s /app/results/_cache/cutout data/cutout

estado calculando "preparando paquete"
B paquete $E/paquete.json >>$LOG 2>&1 || falla "paquete"
ANIOS=$(python3 -c "import json;print(' '.join(map(str,json.load(open('$E/paquete.json'))['anios'])))")
RUN=$(python3 -c "import json;print('edge-'+json.load(open('$E/paquete.json')).get('ejecucion','manual'))")
echo "== $RUN_ID | años: $ANIOS | nudos ${CLUSTERS:-37} | ${RESOLUCION:-3h} | cutout ${CUTOUT_FUENTE:-archivo} | $(nproc) vCPU, $(awk '/MemTotal/{printf "%.1f", $2/1048576}' /proc/meminfo) GB" | tee -a $LOG
HECHOS=()
for Y in $ANIOS; do
  CFG=$E/config_$Y.yaml; SOL=results/$RUN/networks/solved_$Y.nc
  PX python railway/edge_paquete.py config $E/paquete.json $Y $CFG >>$LOG 2>&1 || { echo "$Y: configuración" >> $E/avisos.txt; continue; }
  estado calculando "año $Y: red y datos"
  if ! PX snakemake --cores ${SNAKE_CORES:-all} $SOL --configfile $CFG --until compose_network --rerun-incomplete >>$LOG 2>&1; then
    echo "$Y: falló la preparación de la red (ver logs/run.log)" >> $E/avisos.txt; continue
  fi
  RED=$(ls -t resources/$RUN/networks/composed_$Y.nc resources/networks/composed_$Y.nc 2>/dev/null | head -1)
  [[ -n "$RED" ]] || { echo "$Y: no se encontró composed_$Y.nc" >> $E/avisos.txt; continue; }
  estado calculando "año $Y: supuestos de EDGE"
  PX python railway/edge_ajustes.py $RED $E/paquete.json $Y $E >>$LOG 2>&1 || { echo "$Y: ajustes" >> $E/avisos.txt; continue; }
  EXTRA=""; [[ -f $E/ccl_$Y.yaml ]] && EXTRA="$E/ccl_$Y.yaml"
  estado calculando "año $Y: resolviendo"
  if ! PX snakemake --cores ${SNAKE_CORES:-all} $SOL --configfile $CFG $EXTRA --rerun-triggers mtime >>$LOG 2>&1; then
    echo "$Y: falló la optimización (ver logs/run.log)" >> $E/avisos.txt; continue
  fi
  PX python railway/edge_export.py $SOL $E/paquete.json $Y $E $E/ajustes_$Y.json >>$LOG 2>&1 \
    && HECHOS+=($Y) || echo "$Y: exportación" >> $E/avisos.txt
done
cp -r results/$RUN/logs $E/logs/snakemake 2>/dev/null || true
B resumen $E >>$LOG 2>&1
(( ${#HECHOS[@]} )) || falla "ningún año resuelto"
B subir $E >>$LOG 2>&1 || falla "subida"
estado terminado "fin" "años resueltos: ${HECHOS[*]} de $ANIOS"
