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
estado(){ echo "[$(date -u +%H:%M:%S)] estado: $1 · $2${3:+ · $3}"; B estado "$@" || true; }
# Latido: cada LATIDO_S segundos (300 por defecto) escribe en el log de Railway la memoria, el progreso de
# snakemake y la última línea del log del solver, para ver si avanza sin esperar al final.
T0=$(date +%s)
latido(){
  while sleep ${LATIDO_S:-300}; do
    MEM=$(awk '{printf "%.1f", $1/1073741824}' /sys/fs/cgroup/memory.current 2>/dev/null || echo "?")
    PASO=$(grep -E "steps \([0-9]+%\) done" $LOG 2>/dev/null | tail -n1 | sed 's/^ *//')
    SLOG=$(find . -path "*solve_network*" -name "*.log" -newer $E/paquete.json 2>/dev/null | xargs -r ls -t 2>/dev/null | head -n1)
    SOLV=""; [[ -n "$SLOG" ]] && SOLV=$(grep -v "^\s*$" "$SLOG" | tail -n1 | cut -c1-160)
    echo "[$(date -u +%H:%M:%S)] latido +$(( ($(date +%s)-T0)/60 )) min | mem ${MEM} GB | ${PASO:-snakemake: sin progreso aún}${SOLV:+ | solver: $SOLV}"
  done
}
falla(){ echo "== FALLO: $1"; cat $E/avisos.txt 2>/dev/null; tail -n 120 $LOG 2>/dev/null; estado error "$1" "$(tail -c 1500 $LOG 2>/dev/null)"; B resumen $E >/dev/null 2>&1; B subir $E || true; exit 1; }
# Entorno pixi activado con el script generado en la imagen (no `pixi run`, que reinstala highspy 1.13 del lock).
if [[ -f /opt/pixi_activar.sh ]]; then PX(){ ( source /opt/pixi_activar.sh && "$@" ); }; else PX(){ pixi run -e default "$@"; }; fi

# Clave de Copernicus CDS (solo para CUTOUT_FUENTE=construir): CDSAPI_URL y CDSAPI_KEY -> ~/.cdsapirc
if [[ -n "${CDSAPI_KEY:-}" ]]; then
  printf 'url: %s\nkey: %s\n' "${CDSAPI_URL:-https://cds.climate.copernicus.eu/api}" "$CDSAPI_KEY" > ~/.cdsapirc
fi
# Cutouts: por defecto en el disco temporal del contenedor (se descargan en cada ejecución; el volumen de
# resultados es pequeño). CUTOUT_EN_VOLUMEN=1 los guarda en el volumen (/app/results/_cache/cutout), lo que
# hace falta para no reconstruir en cada ejecución los años que no están en el archivo de PyPSA.
mkdir -p data
if [[ "${CUTOUT_EN_VOLUMEN:-0}" == "1" ]]; then
  mkdir -p /app/results/_cache/cutout
  [[ -e data/cutout ]] || ln -s /app/results/_cache/cutout data/cutout
else
  rm -rf /app/results/_cache/cutout          # restos de intentos anteriores: liberan el volumen
fi
echo "disco: $(df -h /app | tail -1 | awk '{print $4}') libres en /app; volumen: $(df -h /app/results | tail -1 | awk '{print $4}') libres"

estado calculando "preparando paquete"
B paquete $E/paquete.json >>$LOG 2>&1 || falla "paquete"
ANIOS=$(python3 -c "import json;print(' '.join(map(str,json.load(open('$E/paquete.json'))['anios'])))")
RUN=$(python3 -c "import json;print('edge-'+json.load(open('$E/paquete.json')).get('ejecucion','manual'))")
echo "== $RUN_ID | años: $ANIOS | nudos ${CLUSTERS:-37} | ${RESOLUCION:-3h} | cutout ${CUTOUT_FUENTE:-archivo} | $(nproc) vCPU, $(awk '/MemTotal/{printf "%.1f", $2/1048576}' /proc/meminfo) GB" | tee -a $LOG
latido & LATIDO_PID=$!; trap 'kill $LATIDO_PID 2>/dev/null' EXIT
echo "== solver: highs ${SOLVER_OPCIONES:-highs-hipo} ($(PX python -c 'from importlib.metadata import version as v;print("highspy",v("highspy"))' 2>/dev/null || echo 'highspy ?'))"
HECHOS=()
for Y in $ANIOS; do
  CFG=$E/config_$Y.yaml; SOL=results/$RUN/networks/solved_$Y.nc
  PX python railway/edge_paquete.py config $E/paquete.json $Y $CFG >>$LOG 2>&1 || { echo "$Y: configuración" >> $E/avisos.txt; continue; }
  estado calculando "año $Y: red y datos"
  if ! PX snakemake --cores ${SNAKE_CORES:-all} $SOL --configfile $CFG --until compose_network --rerun-incomplete >>$LOG 2>&1; then
    echo "$Y: falló la preparación de la red (ver logs/run.log)" | tee -a $E/avisos.txt; grep -iE "error|exception|missing|not found" $LOG | tail -n 40; continue
  fi
  RED=$(ls -t resources/$RUN/networks/composed_$Y.nc resources/networks/composed_$Y.nc 2>/dev/null | head -1)
  [[ -n "$RED" ]] || { echo "$Y: no se encontró composed_$Y.nc" >> $E/avisos.txt; continue; }
  estado calculando "año $Y: supuestos de EDGE"
  PX python railway/edge_ajustes.py $RED $E/paquete.json $Y $E >>$LOG 2>&1 || { echo "$Y: ajustes" >> $E/avisos.txt; continue; }
  EXTRA=""; [[ -f $E/ccl_$Y.yaml ]] && EXTRA="$E/ccl_$Y.yaml"
  estado calculando "año $Y: resolviendo"
  if ! PX snakemake --cores ${SNAKE_CORES:-all} $SOL --configfile $CFG $EXTRA --rerun-triggers mtime >>$LOG 2>&1; then
    # Si falla con HiPO, se reintenta una vez con el punto interior clásico (IPX, highs-default).
    if [[ "${SOLVER_OPCIONES:-highs-hipo}" != "highs-default" ]]; then
      echo "$Y: falló con ${SOLVER_OPCIONES:-highs-hipo}; reintento con highs-default" | tee -a $E/avisos.txt
      estado calculando "año $Y: resolviendo (reintento IPX)"
      SOLVER_OPCIONES=highs-default PX python railway/edge_paquete.py config $E/paquete.json $Y $CFG >>$LOG 2>&1
      PX snakemake --cores ${SNAKE_CORES:-all} $SOL --configfile $CFG $EXTRA --rerun-triggers mtime >>$LOG 2>&1 \
        || { echo "$Y: falló la optimización (ver logs/run.log)" | tee -a $E/avisos.txt; grep -iE "error|exception|infeasible" $LOG | tail -n 20; continue; }
    else
      echo "$Y: falló la optimización (ver logs/run.log)" | tee -a $E/avisos.txt; grep -iE "error|exception|infeasible" $LOG | tail -n 20; continue
    fi
  fi
  PX python railway/edge_export.py $SOL $E/paquete.json $Y $E $E/ajustes_$Y.json >>$LOG 2>&1 \
    && HECHOS+=($Y) || echo "$Y: exportación" >> $E/avisos.txt
done
cp -r results/$RUN/logs $E/logs/snakemake 2>/dev/null || true
B resumen $E >>$LOG 2>&1
(( ${#HECHOS[@]} )) || falla "ningún año resuelto"
B subir $E >>$LOG 2>&1 || falla "subida"
estado terminado "fin" "años resueltos: ${HECHOS[*]} de $ANIOS"
