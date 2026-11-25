"""CLI de spec-drift-radar.

RC 0 sin deriva · RC 1 deriva medida · RC 2 NO_MEDIDO (nunca degrada a 0) ·
RC 20 argumentos.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import __version__
from .nucleo import (
    comparar,
    emitir_openapi,
    leer_json,
    rc_de,
    superficie_openapi,
    superficie_servidor,
)

CABECERA = "spec-drift-radar · la superficie del servidor contra el contrato que publica"


def _archivos_py(directorio: pathlib.Path) -> list:
    return sorted(p for p in directorio.rglob("*.py") if "__pycache__" not in p.parts)


def _texto(codigo, spec, hallazgos, rc) -> str:
    lineas = [CABECERA, ""]
    lineas.append(f"MARCO DEL SERVIDOR   archivos_py={codigo.marco['archivos_py']} · modelos={codigo.marco['modelos']}"
                  f" · rutas_codigo={codigo.marco['rutas_codigo']} · sin_lector={','.join(codigo.marco['sin_lector'])}")
    lineas.append(f"CAMPOS LEIDOS         rutas_con_campos={codigo.marco['rutas_con_campos']}"
                  f" · rutas_sin_campos={codigo.marco['rutas_sin_campos']}"
                  f" · prefijos_no_medidos={codigo.marco['prefijos_no_medidos']}"
                  f" · inclusiones_condicionales={codigo.marco['inclusiones_condicionales']}"
                  f" · prefijo_base={codigo.marco['prefijo_base']}")
    lineas.append(f"MARCO DEL CONTRATO   openapi={spec.marco.get('openapi', 'NO_MEDIDO')}"
                  f" · titulo={spec.marco.get('titulo', 'NO_MEDIDO')}"
                  f" · version={spec.marco.get('version_declarada', 'NO_MEDIDO')}"
                  f" · rutas_spec={spec.marco.get('rutas_spec', 0)}")
    huecos = list(codigo.sin_medir) + list(spec.sin_medir)
    if huecos:
        lineas.append("")
        lineas.append(f"SIN MEDIR ({len(huecos)})")
        lineas.extend(f"  - {h}" for h in huecos)
    if hallazgos:
        rojos = [h for h in hallazgos if h.severidad == "ROJO"]
        avisos = [h for h in hallazgos if h.severidad != "ROJO"]
        lineas.append("")
        lineas.append(f"HALLAZGOS  rojos={len(rojos)} · avisos={len(avisos)}"
                      f" · pares_comparados={len(set((o.metodo, o.ruta) for o in codigo.operaciones) & set((o.metodo, o.ruta) for o in spec.operaciones))}")
        for h in hallazgos:
            lineas.append(f"  [{h.severidad:<4}] {h.clase:<22} {h.metodo.upper():<7} {h.ruta:<32} {h.detalle}")
    elif rc == 0:
        lineas.append("")
        lineas.append(f"SIN DERIVA  pares_comparados={len(codigo.operaciones)} · se midieron las dos superficies completas")
    lineas.append("")
    lineas.append(f"RC {rc}" + (" · deriva medida: el contrato y el servidor no dicen lo mismo" if rc == 1 else ""))
    if rc == 2:
        lineas.append("RC 2 · NO_MEDIDO: un cero aquí no es un cero, es una ausencia de lectura")
    return "\n".join(lineas)


def _self_test() -> int:
    import tempfile

    codigo = '''
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

router = APIRouter()


class Item(BaseModel):
    nombre: str
    precio: float
    nota: Optional[str] = None


class Salida(BaseModel):
    id: int
    nombre: str


@router.post("/items", response_model=Salida, status_code=201)
def crear(item: Item):
    return {"id": 1, "nombre": item.nombre}


@router.get("/items/{item_id}", response_model=Salida)
def leer(item_id: int):
    return {"id": item_id, "nombre": "x"}
'''
    with tempfile.TemporaryDirectory() as tmp:
        raiz = pathlib.Path(tmp)
        (raiz / "app.py").write_text(codigo, encoding="utf-8")
        servidor = superficie_servidor(_archivos_py(raiz))
        if servidor.marco["rutas_codigo"] != 2:
            print(f"FALLO: se esperaban 2 rutas, se leyeron {servidor.marco['rutas_codigo']}")
            return 1
        emitido = raiz / "openapi.json"
        emitido.write_text(json.dumps(emitir_openapi(servidor)), encoding="utf-8")
        documento, error = leer_json(emitido)
        if error:
            print(f"FALLO: {error}")
            return 1
        spec = superficie_openapi(documento)
        hallazgos = comparar(servidor, spec)
        rc = rc_de(hallazgos, servidor, spec)
        if rc != 0:
            print(f"FALLO: el contrato emitido desde el propio servidor no da RC 0 (RC {rc}, {len(hallazgos)} hallazgos)")
            for h in hallazgos:
                print("   ", h)
            return 1
        documento["paths"]["/items"]["post"]["responses"]["201"]["content"]["application/json"]["schema"][
            "$ref"] = "#/components/schemas/otro"
        documento["components"]["schemas"]["otro"] = {
            "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]
        }
        spec_mutada = superficie_openapi(documento)
        hallazgos = comparar(servidor, spec_mutada)
        rc = rc_de(hallazgos, servidor, spec_mutada)
        if rc != 1 or not any(h.clase == "TIPO_DIVERGENTE" for h in hallazgos):
            print(f"FALLO: la mutacion de un tipo no produjo RC 1 con TIPO_DIVERGENTE (RC {rc})")
            return 1
    print(f"self-test RC 0 · {__version__} · 2 rutas leidas, contrato emitido sin deriva, mutacion detectada")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="spec-drift-radar",
        description="Compara la superficie real de un servidor Python (AST) con el contrato OpenAPI que publica.",
    )
    parser.add_argument("--servidor", help="directorio con el codigo del servidor (se leen los .py recursivamente)")
    parser.add_argument("--openapi", help="documento OpenAPI en JSON")
    parser.add_argument("--emitir", help="escribe el contrato derivado del servidor en esta ruta y no compara")
    parser.add_argument("--prefijo", default="",
                        help="prefijo de montaje que el codigo no puede resolver (p. ej. /api/v1); se antepone a todas las rutas")
    parser.add_argument("--json", action="store_true", help="salida JSON con el marco y los hallazgos")
    parser.add_argument("--version", action="version", version=f"spec-drift-radar {__version__}")
    parser.add_argument("--self-test", action="store_true", help="verifica el instrumento consigo mismo")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.servidor:
        parser.error("falta --servidor (o usa --self-test)")
    directorio = pathlib.Path(args.servidor)
    if not directorio.is_dir():
        parser.error(f"--servidor no es un directorio: {directorio}")
    archivos = _archivos_py(directorio)
    servidor = superficie_servidor(archivos, prefijo_base=args.prefijo)

    if args.emitir:
        if not servidor.operaciones:
            print("RC 2 · NO_MEDIDO: no se leyo ninguna ruta; no se emite un contrato vacio")
            return 2
        destino = pathlib.Path(args.emitir)
        destino.write_text(json.dumps(emitir_openapi(servidor), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"contrato emitido · rutas={servidor.marco['rutas_codigo']} · archivos={servidor.marco['archivos_py']}"
              f" · destino={destino}")
        return 2 if servidor.sin_medir else 0

    if not args.openapi:
        parser.error("falta --openapi")
    documento, error = leer_json(args.openapi)
    if error:
        print(f"RC 2 · NO_MEDIDO: {error}")
        return 2
    spec = superficie_openapi(documento)
    hallazgos = comparar(servidor, spec)
    medidas = len(set((o.metodo, o.ruta) for o in servidor.operaciones) & set((o.metodo, o.ruta) for o in spec.operaciones))
    rc = rc_de(hallazgos, servidor, spec)

    if args.json:
        print(json.dumps({
            "rc": rc,
            "marco": {"servidor": servidor.marco, "contrato": spec.marco},
            "sin_medir": list(servidor.sin_medir) + list(spec.sin_medir),
            "pares_comparados": medidas,
            "hallazgos": [
                {"clase": h.clase, "severidad": h.severidad, "metodo": h.metodo, "ruta": h.ruta, "detalle": h.detalle}
                for h in hallazgos
            ],
        }, indent=2, ensure_ascii=False))
    else:
        print(_texto(servidor, spec, hallazgos, rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
