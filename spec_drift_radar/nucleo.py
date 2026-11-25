"""Superficie del servidor (AST de Python) frente al contrato OpenAPI publicado.

Dos lectores, una sola representacion normalizada: `Operacion` con su peticion,
su respuesta y sus codigos. Comparar dos representaciones normalizadas es la
unica forma de que un hallazgo signifique algo; comparar textos no mide deriva,
mide formato.
"""

from __future__ import annotations

import ast
import json
import pathlib
from dataclasses import dataclass

METODOS = ("get", "post", "put", "patch", "delete", "head", "options")

ESCALARES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "bytes": "string",
    "date": "string",
    "datetime": "string",
    "UUID": "string",
    "Decimal": "number",
    "Any": "any",
    "list": "array",
    "List": "array",
    "set": "array",
    "Set": "array",
    "Sequence": "array",
    "tuple": "array",
    "dict": "object",
    "Dict": "object",
    "Mapping": "object",
}

BASES_MODELO = {"BaseModel", "BaseSettings", "Schema", "Base"}


@dataclass(frozen=True)
class Campo:
    nombre: str
    tipo: str
    requerido: bool


@dataclass(frozen=True)
class Operacion:
    metodo: str
    ruta: str
    peticion: tuple
    respuesta: tuple
    codigos: tuple
    fuente: str


@dataclass(frozen=True)
class Superficie:
    etiqueta: str
    operaciones: tuple
    archivos: int
    modelos: int
    sin_medir: tuple
    marco: dict


@dataclass(frozen=True)
class Hallazgo:
    clase: str
    metodo: str
    ruta: str
    detalle: str
    severidad: str


# --- lector 1: AST del servidor ---------------------------------------------


def _tipo_py(anotacion: str, modelos) -> tuple:
    """(tipo normalizado, es_opcional_por_anotacion)."""
    t = anotacion.replace(" ", "")
    opcional = False
    if t.startswith("Optional[") and t.endswith("]"):
        opcional = True
        t = t[len("Optional[") : -1]
    if "|None" in t:
        opcional = True
        t = t.replace("|None", "")
    if t.endswith("None") and "|" in t:
        opcional = True
    for prefijo in ("list[", "List[", "Sequence[", "set[", "Set[", "tuple["):
        if t.startswith(prefijo):
            return "array", opcional
    for prefijo in ("dict[", "Dict[", "Mapping["):
        if t.startswith(prefijo):
            return "object", opcional
    base = t.split("[")[0].split(".")[-1]
    if base in modelos:
        return "object", opcional
    return ESCALARES.get(base, base.lower()), opcional


def _nombres_modelos(arboles) -> set:
    nombres = set()
    for arbol in arboles:
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ClassDef):
                bases = {ast.unparse(b).split(".")[-1] for b in nodo.bases}
                if bases & BASES_MODELO:
                    nombres.add(nodo.name)
    return nombres


def _campos_modelo(clase: ast.ClassDef, modelos) -> tuple:
    campos = []
    for item in clase.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        tipo, opcional = _tipo_py(ast.unparse(item.annotation), modelos)
        requerido = not (opcional or item.value is not None)
        campos.append(Campo(item.target.id, tipo, requerido))
    return tuple(campos)


def _modelos(arboles, nombres) -> dict:
    salida = {}
    for arbol in arboles:
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ClassDef) and nodo.name in nombres:
                salida[nodo.name] = _campos_modelo(nodo, nombres)
    return salida


def _modelo_de(anotacion: str, modelos) -> str | None:
    base = anotacion.replace(" ", "").split("[")[0].split(".")[-1]
    if base in modelos:
        return base
    interior = anotacion.replace(" ", "")
    if interior.startswith("Optional[") and interior.endswith("]"):
        base = interior[len("Optional[") : -1].split("[")[0].split(".")[-1]
        if base in modelos:
            return base
    return None


def _kwargs_decorador(dec: ast.Call, modelos) -> tuple:
    codigos = []
    respuesta = ()
    for kw in dec.keywords:
        if kw.arg == "status_code" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
            codigos.append(kw.value.value)
        if kw.arg == "response_model":
            nombre = _modelo_de(ast.unparse(kw.value), modelos)
            if nombre:
                respuesta = modelos[nombre]
    if not codigos:
        codigos = [200]
    return tuple(codigos), respuesta


def superficie_servidor(rutas: list) -> Superficie:
    arboles, sin_medir = [], []
    for ruta in sorted(pathlib.Path(p) for p in rutas):
        fuente = ruta.read_text(encoding="utf-8", errors="replace")
        try:
            arboles.append((ruta, ast.parse(fuente, filename=str(ruta))))
        except SyntaxError as exc:
            sin_medir.append(f"{ruta}: no parsea ({exc.msg} linea {exc.lineno})")

    nombres = _nombres_modelos([a for _, a in arboles])
    modelos = _modelos([a for _, a in arboles], nombres)

    operaciones = []
    for ruta, arbol in arboles:
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in nodo.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                metodo = dec.func.attr.lower()
                if metodo not in METODOS:
                    continue
                camino = None
                if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                    camino = dec.args[0].value
                codigos, respuesta = _kwargs_decorador(dec, modelos)
                peticion = ()
                argumentos = nodo.args.args
                if argumentos and argumentos[0].arg in ("self", "cls"):
                    argumentos = argumentos[1:]
                for arg in argumentos:
                    if arg.annotation is None:
                        continue
                    nombre_modelo = _modelo_de(ast.unparse(arg.annotation), modelos)
                    if nombre_modelo:
                        peticion = modelos[nombre_modelo]
                if camino is None:
                    sin_medir.append(f"{ruta}:{nodo.lineno} ruta no literal en {metodo}")
                    continue
                operaciones.append(Operacion(metodo, camino, peticion, respuesta, codigos, f"{ruta}:{nodo.lineno}"))

    marco = {
        "archivos_py": len(arboles),
        "modelos": len(modelos),
        "rutas_codigo": len(operaciones),
        "sin_lector": ["typescript", "yaml"],
    }
    return Superficie("servidor", tuple(operaciones), len(arboles), len(modelos), tuple(sin_medir), marco)


# --- lector 2: documento OpenAPI --------------------------------------------


def _resolver(schema: dict, componentes: dict) -> dict:
    visto = 0
    while isinstance(schema, dict) and "$ref" in schema and visto < 8:
        nombre = str(schema["$ref"]).split("/")[-1]
        schema = componentes.get(nombre, {})
        visto += 1
    return schema if isinstance(schema, dict) else {}


def _campos_schema(schema: dict, componentes: dict) -> tuple:
    schema = _resolver(schema, componentes)
    if schema.get("type") == "array" or "items" in schema:
        schema = _resolver(schema.get("items") or {}, componentes)
    propiedades = schema.get("properties") or {}
    requeridos = set(schema.get("required") or ())
    campos = []
    for nombre, definicion in propiedades.items():
        definicion = _resolver(definicion, componentes)
        tipo = definicion.get("type")
        if tipo is None:
            if "properties" in definicion:
                tipo = "object"
            elif "items" in definicion:
                tipo = "array"
            elif "$ref" in definicion:
                tipo = "object"
            else:
                tipo = "any"
        if tipo == "object" and "properties" not in definicion and "$ref" not in definicion:
            tipo = "object"
        campos.append(Campo(nombre, tipo, nombre in requeridos))
    return tuple(campos)


def _cuerpo(op: dict) -> dict:
    contenido = (op.get("requestBody") or {}).get("content") or {}
    for tipo, valor in contenido.items():
        if "json" in tipo:
            return valor.get("schema") or {}
    return {}


def _respuesta(op: dict) -> dict:
    respuestas = op.get("responses") or {}
    exitosas = sorted(
        (int(c), v) for c, v in respuestas.items() if str(c).isdigit() and 200 <= int(c) < 300
    )
    if not exitosas:
        return {}
    contenido = (exitosas[0][1] or {}).get("content") or {}
    for tipo, valor in contenido.items():
        if "json" in tipo:
            return valor.get("schema") or {}
    return {}


def superficie_openapi(documento: dict) -> Superficie:
    if not isinstance(documento, dict):
        return Superficie("openapi", (), 0, 0, ("el documento no es un objeto JSON",), {})
    componentes = ((documento.get("components") or {}).get("schemas")) or {}
    paths = documento.get("paths") or {}
    operaciones = []
    for camino, item in paths.items():
        if not isinstance(item, dict):
            continue
        for metodo, op in item.items():
            if metodo.lower() not in METODOS or not isinstance(op, dict):
                continue
            codigos = tuple(sorted(int(c) for c in (op.get("responses") or {}) if str(c).isdigit()))
            operaciones.append(
                Operacion(
                    metodo.lower(),
                    camino,
                    _campos_schema(_cuerpo(op), componentes),
                    _campos_schema(_respuesta(op), componentes),
                    codigos,
                    "openapi",
                )
            )
    info = documento.get("info") or {}
    marco = {
        "openapi": documento.get("openapi") or documento.get("swagger") or "NO_MEDIDO",
        "titulo": info.get("title") or "NO_MEDIDO",
        "version_declarada": info.get("version") or "NO_MEDIDO",
        "rutas_spec": len(operaciones),
        "esquemas": len(componentes),
    }
    sin_medir = () if operaciones else ("el documento no declara ninguna operacion",)
    return Superficie("openapi", tuple(operaciones), 1, len(componentes), tuple(sin_medir), marco)


# --- comparacion ------------------------------------------------------------


def _indice(superficie: Superficie) -> dict:
    return {(op.metodo, op.ruta): op for op in superficie.operaciones}


def _divergencias(campos_codigo: tuple, campos_spec: tuple, metodo: str, ruta: str) -> list:
    salida = []
    codigo = {c.nombre: c for c in campos_codigo}
    spec = {c.nombre: c for c in campos_spec}
    for nombre in sorted(codigo):
        if nombre not in spec:
            salida.append(Hallazgo("CAMPO_FALTA_EN_SPEC", metodo, ruta,
                                   f"el servidor declara '{nombre}'; el contrato no", "ROJO"))
            continue
        if codigo[nombre].tipo != spec[nombre].tipo:
            salida.append(Hallazgo("TIPO_DIVERGENTE", metodo, ruta,
                                   f"'{nombre}': servidor={codigo[nombre].tipo} · contrato={spec[nombre].tipo}", "ROJO"))
        if codigo[nombre].requerido != spec[nombre].requerido:
            salida.append(Hallazgo("REQUERIDO_DIVERGENTE", metodo, ruta,
                                   f"'{nombre}': servidor={'requerido' if codigo[nombre].requerido else 'opcional'}"
                                   f" · contrato={'requerido' if spec[nombre].requerido else 'opcional'}", "ROJO"))
    for nombre in sorted(spec):
        if nombre not in codigo:
            salida.append(Hallazgo("CAMPO_FALTA_EN_CODIGO", metodo, ruta,
                                   f"el contrato declara '{nombre}'; el servidor no", "ROJO"))
    return salida


def comparar(codigo: Superficie, spec: Superficie) -> list:
    hallazgos = []
    indice_codigo, indice_spec = _indice(codigo), _indice(spec)
    for clave in sorted(indice_codigo, key=lambda k: (k[1], k[0])):
        if clave not in indice_spec:
            op = indice_codigo[clave]
            hallazgos.append(Hallazgo("FALTA_EN_SPEC", op.metodo, op.ruta,
                                      f"el servidor la declara en {op.fuente}", "ROJO"))
    for clave in sorted(indice_spec, key=lambda k: (k[1], k[0])):
        if clave not in indice_codigo:
            hallazgos.append(Hallazgo("FALTA_EN_CODIGO", clave[0], clave[1],
                                      "el contrato la publica y el servidor no la declara", "ROJO"))
    for clave in sorted(set(indice_codigo) & set(indice_spec), key=lambda k: (k[1], k[0])):
        a, b = indice_codigo[clave], indice_spec[clave]
        hallazgos.extend(_divergencias(a.peticion, b.peticion, a.metodo, a.ruta))
        hallazgos.extend(_divergencias(a.respuesta, b.respuesta, a.metodo, a.ruta))
        if set(a.codigos) != set(b.codigos):
            hallazgos.append(Hallazgo("CODIGO_DIVERGENTE", a.metodo, a.ruta,
                                      f"servidor={sorted(a.codigos)} · spec={sorted(b.codigos)}", "WARN"))
    return hallazgos


def rc_de(hallazgos, codigo: Superficie, spec: Superficie) -> int:
    """1 deriva medida · 2 NO_MEDIDO (universo vacio o lectura incompleta) · 0 limpio.

    Un universo vacio no se compara: cero rutas de un lado no es "deriva total", es
    ausencia de lectura, y publicarlo como RC 1 convertiria un archivo equivocado en
    un hallazgo. El RC 1 solo se emite cuando las dos superficies existen.
    """
    vacio = not codigo.operaciones or not spec.operaciones
    if not vacio and any(h.severidad == "ROJO" for h in hallazgos):
        return 1
    if vacio or codigo.sin_medir or spec.sin_medir:
        return 2
    if not set(_indice(codigo)) & set(_indice(spec)):
        return 2
    return 0


# --- emision: la superficie del servidor como contrato ----------------------


def emitir_openapi(codigo: Superficie, titulo: str = "emitido por spec-drift-radar") -> dict:
    paths, esquemas = {}, {}
    for op in codigo.operaciones:
        nombre = f"{op.metodo}_{op.ruta.strip('/').replace('/', '_') or 'raiz'}".replace("{", "").replace("}", "")
        entrada = {}
        if op.peticion:
            esquemas[nombre + "_entrada"] = {
                "type": "object",
                "properties": {c.nombre: {"type": c.tipo} for c in op.peticion},
                "required": [c.nombre for c in op.peticion if c.requerido],
            }
            entrada["requestBody"] = {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{nombre}_entrada"}}}}
        if op.respuesta:
            esquemas[nombre + "_salida"] = {
                "type": "object",
                "properties": {c.nombre: {"type": c.tipo} for c in op.respuesta},
                "required": [c.nombre for c in op.respuesta if c.requerido],
            }
        codigo_ok = sorted(c for c in op.codigos if 200 <= c < 300) or [200]
        respuesta = {"description": ""}
        if op.respuesta:
            respuesta["content"] = {"application/json": {"schema": {"$ref": f"#/components/schemas/{nombre}_salida"}}}
        entrada["responses"] = {str(codigo_ok[0]): respuesta}
        paths.setdefault(op.ruta, {})[op.metodo] = entrada
    return {
        "openapi": "3.1.0",
        "info": {"title": titulo, "version": "0.0.0-emitido"},
        "paths": paths,
        "components": {"schemas": esquemas},
    }


def leer_json(ruta) -> tuple:
    try:
        return json.loads(pathlib.Path(ruta).read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{ruta}: {exc}"
