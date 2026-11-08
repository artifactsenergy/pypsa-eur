"""Comunicación con EDGE por el bucket S3 de Railway (contrato en escenarios/modelos_externos.py de EDH).

Se ejecuta con el python del sistema (apt: python3-boto3), fuera del entorno de pixi.
Uso:
  python3 railway/edge_bucket.py paquete <destino.json>
  python3 railway/edge_bucket.py estado ESTADO FASE [MENSAJE]
  python3 railway/edge_bucket.py resumen <dir_edge>        junta por_anio_*.json en resumen.json
  python3 railway/edge_bucket.py subir <dir_edge>          sube resumen.json, series/ y logs/ a RUN_PREFIX
Variables: PAQUETE_KEY, RUN_PREFIX, BUCKET, ENDPOINT, ACCESS_KEY_ID, SECRET_ACCESS_KEY, REGION.
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

PAQ = Path(os.environ.get("EDGE_DIR", "/app/edge")) / "paquete.json"


def s3():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["ENDPOINT"], region_name=os.environ.get("REGION", "auto"),
                        aws_access_key_id=os.environ["ACCESS_KEY_ID"], aws_secret_access_key=os.environ["SECRET_ACCESS_KEY"])


def put_json(clave, obj):
    s3().put_object(Bucket=os.environ["BUCKET"], Key=clave, ContentType="application/json",
                    Body=json.dumps(obj, ensure_ascii=False, default=float).encode())


def main():
    orden = sys.argv[1]
    if orden == "paquete":
        destino = Path(sys.argv[2]); destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(s3().get_object(Bucket=os.environ["BUCKET"], Key=os.environ["PAQUETE_KEY"])["Body"].read())
        print("paquete", destino)
    elif orden == "estado":
        p = json.loads(PAQ.read_text()) if PAQ.exists() else {}
        put_json(os.environ["RUN_PREFIX"] + "estado.json",
                 {"estado": sys.argv[2], "fase": sys.argv[3], "mensaje": (sys.argv[4] if len(sys.argv) > 4 else "")[-2000:],
                  "huella": p.get("huella"),
                  "actualizado": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    elif orden == "resumen":
        d = Path(sys.argv[2]); p = json.loads(PAQ.read_text()) if PAQ.exists() else {}
        por_anio, avisos, comprobacion = {}, [], {}
        for f in sorted(d.glob("por_anio_*.json")):
            a = f.stem.split("_")[-1]; x = json.loads(f.read_text())
            por_anio[a] = x["indicadores"]; comprobacion[a] = x["comprobacion_supuestos"]
            avisos += ["%s · %s" % (a, v) for v in x.get("avisos", [])]
        avisos += [l.strip() for l in (d / "avisos.txt").read_text().splitlines()] if (d / "avisos.txt").exists() else []
        json.dump({"huella": p.get("huella"), "modelo": "pypsa", "por_anio": por_anio, "comprobacion_supuestos": comprobacion,
                   "avisos": avisos,
                   "notas": ["Precios = coste marginal de PyPSA-Eur (sin márgenes de escasez ni estrategias de oferta).",
                             "Resolución temporal reducida: las horas de cada bloque repiten el mismo precio."]},
                  open(d / "resumen.json", "w"), ensure_ascii=False, indent=1)
        print("resumen", list(por_anio))
    elif orden == "subir":
        d = Path(sys.argv[2]); cli = s3(); n = 0
        for f in [x for x in d.rglob("*") if x.is_file() and x.name != "paquete.json"]:
            cli.upload_file(str(f), os.environ["BUCKET"], os.environ["RUN_PREFIX"] + f.relative_to(d).as_posix()); n += 1
        print(n, "ficheros subidos")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
