"""Superficie del servidor (AST de Python) frente al contrato OpenAPI publicado.

Dos lectores, una sola representacion normalizada: `Operacion` con su peticion,
su respuesta y sus codigos. Comparar dos representaciones normalizadas es la
unica forma de que un hallazgo signifique algo; comparar textos no mide deriva,
mide formato.
"""

from __future__ import annotations

import ast
import collections
import json
import os
import pathlib
import re
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
    "uuid": "string",
    "Decimal": "number",
    "EmailStr": "string",
    "NameEmail": "string",
    "HttpUrl": "string",
    "AnyHttpUrl": "string",
    "AnyUrl": "string",
    "PostgresDsn": "string",
    "AnyWebsocketUrl": "string",
    "SecretStr": "string",
    "IPvAnyAddress": "string",
    "date": "string",
    "timedelta": "number",
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

BASES_MODELO = {"BaseModel", "BaseSettings", "Schema", "Base", "SQLModel", "GenericModel"}


@dataclass(frozen=True)
class Campo:
    nombre: str
    tipo: str
    requerido: bool
    alias_no_medido: bool = False


@dataclass(frozen=True)
class Operacion:
    metodo: str
    ruta: str
    peticion: tuple
    respuesta: tuple
    codigos: tuple
    fuente: str
    condicional: bool = False


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
    if base in ESCALARES:
        return ESCALARES[base], opcional
    escalares = {nombre.lower(): tipo for nombre, tipo in ESCALARES.items()}
    return escalares.get(base.lower(), base.lower()), opcional


def _nombres_modelos(arboles) -> set:
    """Un modelo es quien hereda de una base conocida, o de otro modelo.

    La clausura importa: `class User(UserBase, table=True)` en SQLModel no nombra
    `SQLModel` en ninguna parte, y sin transitividad el 96 % de los campos de una
    app SQLModel se quedaba fuera de la medicion sin avisar.
    """
    candidatos = {}
    for arbol in arboles:
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ClassDef):
                bases = {ast.unparse(b).split(".")[-1] for b in nodo.bases}
                candidatos.setdefault(nodo.name, set()).update(bases)
    nombres = {nombre for nombre, bases in candidatos.items() if bases & BASES_MODELO}
    for _ in range(8):
        nuevos = {nombre for nombre, bases in candidatos.items() if bases & nombres} - nombres
        if not nuevos:
            break
        nombres |= nuevos
    return nombres


def _requerido_en_llamada(valor) -> bool:
    """`Field(...)` es requerido en pydantic v1 y `Field(None)` no; `Field(min_length=1)` tampoco da default."""
    if not isinstance(valor, ast.Call):
        return isinstance(valor, ast.Constant) and valor.value is Ellipsis
    if valor.args:
        primero = valor.args[0]
        return isinstance(primero, ast.Constant) and primero.value is Ellipsis
    return not any(kw.arg in ("default", "default_factory") for kw in valor.keywords)


def _tiene_valor_por_defecto(valor) -> bool:
    """`title: str = Field(min_length=1)` no tiene default: `Field()` no es un valor, es configuracion.

    Solo `Field(123)`, `Field(default=...)` o `Field(default_factory=...)` dan un default;
    confundirlos marcaba como opcional casi todo campo de una app SQLModel/Pydantic v2.
    """
    if not isinstance(valor, ast.Call):
        return True
    nombre = ast.unparse(valor.func).split(".")[-1]
    if nombre not in ("Field", "Column", "Body", "Form", "File"):
        return True
    return not _requerido_en_llamada(valor)


def _alias_literal(valor) -> str | None:
    """`Field(..., alias="tagList")` publica `tagList`: el nombre esta en el codigo y se puede leer."""
    if not isinstance(valor, ast.Call):
        return None
    for kw in valor.keywords:
        if kw.arg == "alias" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _campos_propios(clase: ast.ClassDef, modelos, con_generador: bool = False) -> list:
    campos = []
    for item in clase.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        tipo, opcional = _tipo_py(ast.unparse(item.annotation), modelos)
        requerido = not (opcional or (item.value is not None and _tiene_valor_por_defecto(item.value)))
        alias = _alias_literal(item.value)
        # Sin alias literal, un `alias_generator` de la clase (o de un padre) renombra el campo
        # con un valor que no esta en el AST: el nombre documentado es NO_MEDIDO, no una deriva.
        campos.append(Campo(alias or item.target.id, tipo, requerido, alias is None and con_generador))
    return campos


def _con_alias_generador(arboles, nombres) -> set:
    """Modelos cuyo `Config.alias_generator` renombra los campos sin decir como."""
    directos, padres = set(), {}
    for arbol in arboles:
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.ClassDef):
                continue
            padres[nodo.name] = [ast.unparse(b).split(".")[-1] for b in nodo.bases]
            for hijo in nodo.body:
                if not isinstance(hijo, ast.ClassDef):
                    continue
                for linea in hijo.body:
                    objetivo = getattr(linea, "target", None)
                    if isinstance(linea, ast.Assign):
                        objetivo = linea.targets[0] if linea.targets else None
                    if isinstance(objetivo, ast.Name) and objetivo.id == "alias_generator":
                        directos.add(nodo.name)
    heredan = set(directos)
    for _ in range(8):
        nuevos = {nombre for nombre, bases in padres.items()
                  if nombre in nombres and set(bases) & heredan} - heredan
        if not nuevos:
            break
        heredan |= nuevos
    return heredan & nombres


def _modelos(arboles, nombres) -> dict:
    """Los campos de un modelo son los de sus padres mas los propios, que ganan.

    Sin herencia, `class Usuario(UsuarioBase, table=True)` se leia con un solo
    campo de cuatro: el hijo declara `id` y el padre declara el resto.
    """
    clases, padres = {}, {}
    for arbol in arboles:
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ClassDef) and nodo.name in nombres:
                clases[nodo.name] = nodo
                padres[nodo.name] = [ast.unparse(b).split(".")[-1] for b in nodo.bases]

    con_generador = _con_alias_generador(arboles, nombres)
    memo = {}

    def resolver(nombre, visto=()):
        if nombre in memo:
            return memo[nombre]
        clase = clases.get(nombre)
        if clase is None or nombre in visto:
            return ()
        acumulado = {}
        for base in padres.get(nombre, ()):
            for campo in resolver(base, visto + (nombre,)):
                acumulado[campo.nombre] = campo
        for campo in _campos_propios(clase, nombres, nombre in con_generador):
            acumulado[campo.nombre] = campo
        memo[nombre] = tuple(acumulado.values())
        return memo[nombre]

    for nombre in clases:
        resolver(nombre)
    return dict(memo)


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


def _codigo_de(nodo):
    """`201`, `status.HTTP_201_CREATED`, `HTTP_201_CREATED` y `HTTPStatus.CREATED`: los cuatro son codigos."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, int):
        return nodo.value
    # Atributo o nombre suelto: `from starlette.status import HTTP_201_CREATED` deja un Name.
    if isinstance(nodo, (ast.Attribute, ast.Name)):
        encontrado = re.search(r"HTTP_(\d{3})_", ast.unparse(nodo))
        if encontrado:
            return int(encontrado.group(1))
    return None


def _kwargs_decorador(dec: ast.Call, modelos) -> tuple:
    codigos = []
    respuesta = ()
    for kw in dec.keywords:
        if kw.arg == "status_code":
            codigo = _codigo_de(kw.value)
            if codigo is not None:
                codigos.append(codigo)
        if kw.arg == "response_model":
            nombre = _modelo_de(ast.unparse(kw.value), modelos)
            if nombre:
                respuesta = modelos[nombre]
    if not codigos:
        codigos = [200]
    return tuple(codigos), respuesta


def _anotacion_de_retorno(nodo, modelos) -> tuple:
    """`def f(...) -> Token`: sin `response_model`, la respuesta la dice el retorno."""
    if nodo.returns is None:
        return ()
    nombre = _modelo_de(ast.unparse(nodo.returns), modelos)
    return modelos[nombre] if nombre else ()


def _parametros(nodo) -> list:
    """(argumento, valor por defecto): incluye los `*` keyword-only, que no estan en args.args."""
    posicionales = list(nodo.args.posonlyargs) + list(nodo.args.args)
    defectos = list(nodo.args.defaults)
    mapa = {}
    if defectos:
        for argumento, defecto in zip(posicionales[len(posicionales) - len(defectos):], defectos):
            mapa[id(argumento)] = defecto
    pares = [(argumento, mapa.get(id(argumento))) for argumento in posicionales]
    pares += list(zip(nodo.args.kwonlyargs, nodo.args.kw_defaults))
    if pares and pares[0][0].arg in ("self", "cls"):
        pares = pares[1:]
    return [(argumento, defecto) for argumento, defecto in pares if argumento.annotation is not None]


NO_VIAJAN_EN_EL_CUERPO = {
    "Depends", "Security", "Query", "Path", "Header", "Cookie", "Request", "Response",
    "WebSocket", "BackgroundTasks",
}


def _llamada(valor) -> str | None:
    return ast.unparse(valor.func).split(".")[-1] if isinstance(valor, ast.Call) else None


def _cuerpo_de_peticion(nodo, modelos) -> tuple:
    """El cuerpo son los campos del modelo anotado, salvo que el parametro sea una dependencia.

    `articles_repo: ArticlesRepository = Depends(get_repository(...))` no viaja en el cuerpo:
    leerlo como cuerpo convertia cada `Depends` con modelo en 4 campos fantasma por operacion.
    """
    campos, embebido = (), None
    for argumento, defecto in _parametros(nodo):
        llamada = _llamada(defecto)
        if llamada in NO_VIAJAN_EN_EL_CUERPO:
            continue
        nombre_modelo = _modelo_de(ast.unparse(argumento.annotation), modelos)
        if llamada in ("Body", "Form", "File"):
            alias, embed = None, False
            for kw in defecto.keywords:
                if kw.arg == "alias" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    alias = kw.value.value
                if kw.arg == "embed" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    embed = True
            if embed:
                embebido = Campo(alias or argumento.arg, "object", _requerido_en_llamada(defecto))
                continue
            if nombre_modelo:
                campos += modelos[nombre_modelo]
            continue
        if nombre_modelo:
            campos += modelos[nombre_modelo]
    return (embebido,) if embebido else campos


def _prefijos_de_router(arbol) -> dict:
    """Prefijo propio de cada router del modulo: `router = APIRouter(prefix="/users")`."""
    prefijos = {}

    def anotar(objetivo, valor):
        if not isinstance(objetivo, ast.Name) or not isinstance(valor, ast.Call):
            return
        if ast.unparse(valor.func).split(".")[-1] not in ("APIRouter", "FastAPI"):
            return
        prefijo = ""
        for kw in valor.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                prefijo = kw.value.value
        prefijos[objetivo.id] = prefijo

    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign):
            for objetivo in nodo.targets:
                anotar(objetivo, nodo.value)
        elif isinstance(nodo, ast.AnnAssign):
            anotar(nodo.target, nodo.value)
    return prefijos


def _une(prefijo: str, camino: str) -> str:
    """Une sin perder la barra final: FastAPI publica `/items/` y `/items` como rutas distintas."""
    if not prefijo:
        return camino
    cola = "/" if camino.endswith("/") else ""
    partes = [p for p in (prefijo.strip("/"), camino.strip("/")) if p]
    return "/" + "/".join(partes) + cola


def _modulo_de(ruta: pathlib.Path, raiz: pathlib.Path) -> str:
    try:
        relativa = ruta.relative_to(raiz)
    except ValueError:
        relativa = pathlib.Path(ruta.name)
    partes = list(relativa.with_suffix("").parts)
    if partes and partes[-1] == "__init__":
        partes.pop()
    return ".".join(partes)


def _cadenas_de(arbol) -> tuple:
    """Literales de cadena: `API_V1_STR = "/api/v1"` y atributos de clase `Clase.X = "..."`."""
    propias, de_clase = {}, {}
    for nodo in arbol.body:
        if isinstance(nodo, (ast.Assign, ast.AnnAssign)):
            objetivos = nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
            if isinstance(nodo.value, ast.Constant) and isinstance(nodo.value.value, str):
                for objetivo in objetivos:
                    if isinstance(objetivo, ast.Name):
                        propias[objetivo.id] = nodo.value.value
        elif isinstance(nodo, ast.ClassDef):
            for hijo in nodo.body:
                if not isinstance(hijo, (ast.Assign, ast.AnnAssign)):
                    continue
                if not (isinstance(hijo.value, ast.Constant) and isinstance(hijo.value.value, str)):
                    continue
                objetivos = hijo.targets if isinstance(hijo, ast.Assign) else [hijo.target]
                for objetivo in objetivos:
                    if isinstance(objetivo, ast.Name):
                        de_clase.setdefault(objetivo.id, set()).add(hijo.value.value)
    return propias, de_clase


def _importados_de(arbol) -> dict:
    """`from a.b import router as r` ⇒ r -> (modulo base, atributo); `import a.b as x` ⇒ x -> (a.b, None)."""
    importados = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            for alias in nodo.names:
                importados[alias.asname or alias.name] = (nodo.module or "", alias.name, nodo.level)
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if alias.asname:
                    importados[alias.asname] = (alias.name, None, 0)
    return importados


def _buscar_modulo(nombre: str, modulos) -> str | None:
    """Los imports suelen ser absolutos (`app.api.routes.users`) y el indice relativo (`api.routes.users`)."""
    if not nombre:
        return None
    partes = nombre.split(".")
    for desde in range(len(partes)):
        candidato = ".".join(partes[desde:])
        if candidato in modulos:
            return candidato
    return None


class _Indice:
    """Todo lo que hay que saber de un proyecto para resolver un prefijo de montaje."""

    def __init__(self, arboles, raiz):
        self.modulos = {_modulo_de(ruta, raiz): arbol for ruta, arbol in arboles}
        self.cadenas = {m: _cadenas_de(a) for m, a in self.modulos.items()}
        self.imports = {m: _importados_de(a) for m, a in self.modulos.items()}
        self.prefijos = {(m, n): p for m, a in self.modulos.items() for n, p in _prefijos_de_router(a).items()}
        self.constantes = {}
        self.atributos = collections.defaultdict(set)
        for modulo, (propias, de_clase) in self.cadenas.items():
            for nombre, valor in propias.items():
                self.constantes[(modulo, nombre)] = valor
            for nombre, valores in de_clase.items():
                self.atributos[nombre] |= valores
                if len(valores) == 1:
                    self.constantes.setdefault((modulo, nombre), next(iter(valores)))

    def valor(self, expr, modulo) -> str | None:
        propias = self.cadenas[modulo][0]
        importados = self.imports[modulo]
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        if isinstance(expr, ast.JoinedStr):
            partes = []
            for valor in expr.values:
                if isinstance(valor, ast.Constant) and isinstance(valor.value, str):
                    partes.append(valor.value)
                elif isinstance(valor, ast.FormattedValue):
                    resuelto = self.valor(valor.value, modulo)
                    if resuelto is None:
                        return None
                    partes.append(resuelto)
                else:
                    return None
            return "".join(partes)
        if isinstance(expr, ast.Name):
            if expr.id in propias:
                return propias[expr.id]
            if expr.id in importados:
                base, atributo, _ = importados[expr.id]
                destino = _buscar_modulo(base, self.modulos) if base else None
                if destino and atributo and (destino, atributo) in self.constantes:
                    return self.constantes[(destino, atributo)]
            return None
        if isinstance(expr, ast.Attribute):
            base = expr.value
            if isinstance(base, ast.Name) and base.id in importados:
                modulo_base, nombre, _ = importados[base.id]
                candidatos = [f"{modulo_base}.{nombre}" if nombre else modulo_base, modulo_base]
                for candidato in candidatos:
                    destino = _buscar_modulo(candidato, self.modulos) if candidato else None
                    if destino and (destino, expr.attr) in self.constantes:
                        return self.constantes[(destino, expr.attr)]
            valores = self.atributos.get(expr.attr)
            if valores and len(valores) == 1:
                return next(iter(valores))
        return None

    def referencia(self, expr, modulo):
        """A que (modulo, router) apunta una expresion usada como destino de include_router."""
        importados = self.imports[modulo]
        if isinstance(expr, ast.Name):
            if expr.id in importados:
                base, atributo, _ = importados[expr.id]
                if atributo is None:
                    destino = _buscar_modulo(base, self.modulos)
                    return (destino, None) if destino else None
                completo = _buscar_modulo(f"{base}.{atributo}", self.modulos) if base else None
                if completo:
                    return (completo, None)
                destino = _buscar_modulo(base, self.modulos) if base else None
                return (destino, atributo) if destino else None
            return (modulo, expr.id)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            base = expr.value.id
            if base in importados:
                modulo_base, nombre, _ = importados[base]
                candidatos = [f"{modulo_base}.{nombre}" if nombre else modulo_base, modulo_base]
                for candidato in candidatos:
                    destino = _buscar_modulo(candidato, self.modulos) if candidato else None
                    if destino:
                        return (destino, expr.attr)
        return None

    def aristas(self, ajenos: list) -> tuple:
        """Grafo de montaje: (host_modulo, host_router) -> (destino, prefijo). y cuantas son condicionales."""
        aristas, condicionales = [], 0
        for modulo, arbol in self.modulos.items():
            condicionales_nodos = set()
            for nodo in ast.walk(arbol):
                if isinstance(nodo, (ast.If, ast.Try, ast.While, ast.For, ast.AsyncFor)):
                    for hijo in ast.walk(nodo):
                        condicionales_nodos.add(id(hijo))
            for nodo in ast.walk(arbol):
                if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)):
                    continue
                if nodo.func.attr != "include_router" or not nodo.args:
                    continue
                host = self.referencia(nodo.func.value, modulo)
                destino = self.referencia(nodo.args[0], modulo)
                if destino is None:
                    ajenos.append(f"{modulo}: include_router con destino no resoluble ({ast.unparse(nodo.args[0])})")
                    continue
                prefijo, declarado = "", True
                for kw in nodo.keywords:
                    if kw.arg != "prefix":
                        continue
                    resuelto = self.valor(kw.value, modulo)
                    if resuelto is None:
                        declarado = False
                        ajenos.append(f"{modulo}:{nodo.lineno} include_router con prefijo no resoluble"
                                      f" ({ast.unparse(kw.value)}): las rutas montadas ahi no llevan ese prefijo")
                    else:
                        prefijo = resuelto
                condicional = id(nodo) in condicionales_nodos
                if condicional:
                    condicionales += 1
                aristas.append((host, destino, prefijo, declarado, condicional))
        return aristas, condicionales

    def montajes(self, aristas) -> dict:
        """Prefijos con los que se alcanza cada router: {(modulo, router): {prefijo: condicional}}.

        Un `include_router` dentro de un `if` o un `try` no se puede decidir sin ejecutar:
        se marca condicional y todas sus rutas heredan la marca.
        """
        destinos = {destino for _, destino, _, _, _ in aristas}
        # Una raiz es todo nodo que nadie monta: un router suelto pero tambien el objeto
        # `FastAPI()` del arranque, que no es un router y por tanto no esta en `prefijos`.
        nodos = set(self.prefijos) | {host for host, _, _, _, _ in aristas if host is not None} | destinos
        semillas = [(nodo, "", False) for nodo in sorted(nodos) if nodo not in destinos]
        por_host = collections.defaultdict(list)
        for host, destino, prefijo, _, condicional in aristas:
            if host is None:
                semillas.append((destino, prefijo, condicional))
            else:
                por_host[host].append((destino, prefijo, condicional))
        montaje = collections.defaultdict(dict)
        cola = collections.deque(semillas)
        pasos = 0
        while cola and pasos < 10000:
            pasos += 1
            clave, prefijo, condicional = cola.popleft()
            if prefijo in montaje[clave] and (montaje[clave][prefijo] or not condicional):
                continue
            montaje[clave][prefijo] = condicional
            for destino, propio, condicional_hijo in por_host.get(clave, ()):
                cola.append((destino, _une(prefijo, propio), condicional or condicional_hijo))
        return montaje


def superficie_servidor(rutas: list, prefijo_base: str = "", raiz=None) -> Superficie:
    arboles, sin_medir, ajenos = [], [], []
    for ruta in sorted(pathlib.Path(p) for p in rutas):
        fuente = ruta.read_text(encoding="utf-8", errors="replace")
        try:
            arboles.append((ruta, ast.parse(fuente, filename=str(ruta))))
        except SyntaxError as exc:
            sin_medir.append(f"{ruta}: no parsea ({exc.msg} linea {exc.lineno})")

    nombres = _nombres_modelos([a for _, a in arboles])
    modelos = _modelos([a for _, a in arboles], nombres)

    if raiz is None:
        raiz = pathlib.Path(os.path.commonpath([str(r.parent) for r, _ in arboles])) if arboles else pathlib.Path(".")
    indice = _Indice(arboles, pathlib.Path(raiz))
    aristas, condicionales = indice.aristas(ajenos)
    montaje = indice.montajes(aristas)
    if not prefijo_base:
        sin_medir.extend(ajenos)

    operaciones, vistas = [], {}
    for ruta, arbol in arboles:
        modulo = _modulo_de(ruta, pathlib.Path(raiz))
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
                if not respuesta:
                    respuesta = _anotacion_de_retorno(nodo, modelos)
                peticion = _cuerpo_de_peticion(nodo, modelos)
                if camino is None:
                    sin_medir.append(f"{ruta}:{nodo.lineno} ruta no literal en {metodo}")
                    continue
                nombre_router = ast.unparse(dec.func.value)
                propio = indice.prefijos.get((modulo, nombre_router), "")
                for prefijo, condicional in sorted((montaje.get((modulo, nombre_router)) or {"": False}).items()):
                    completa = _une(prefijo_base, _une(_une(prefijo, propio), camino))
                    if (metodo, completa) in vistas:
                        if not condicional:
                            vistas[(metodo, completa)] = False
                        continue
                    vistas[(metodo, completa)] = condicional
                    operaciones.append(Operacion(metodo, completa, peticion, respuesta, codigos,
                                                 f"{ruta}:{nodo.lineno}", condicional))

    con_campos = sum(1 for op in operaciones if op.peticion or op.respuesta)
    marco = {
        "archivos_py": len(arboles),
        "modelos": len(modelos),
        "rutas_codigo": len(operaciones),
        "rutas_con_campos": con_campos,
        "rutas_sin_campos": len(operaciones) - con_campos,
        "prefijos_no_medidos": len(ajenos),
        "prefijo_base": prefijo_base or "NO_DECLARADO",
        "inclusiones_condicionales": condicionales,
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


def _tipo_de(definicion: dict, componentes: dict, profundidad: int = 0) -> str:
    """Tipo normalizado de un schema.

    OpenAPI 3.1 expresa `Optional[str]` como `anyOf: [{type: string}, {type: null}]`:
    el tipo real es la rama que no es nula, y publicarlo como `any` convertiria el
    contrato del propio generador de FastAPI en una deriva falsa.
    """
    definicion = _resolver(definicion, componentes)
    tipo = definicion.get("type")
    if isinstance(tipo, str):
        return tipo
    if profundidad < 3:
        for clave in ("anyOf", "oneOf", "allOf"):
            ramas = definicion.get(clave)
            if isinstance(ramas, list):
                for rama in ramas:
                    resuelta = _resolver(rama, componentes)
                    if resuelta.get("type") == "null":
                        continue
                    candidato = _tipo_de(resuelta, componentes, profundidad + 1)
                    if candidato != "any":
                        return candidato
    if "properties" in definicion:
        return "object"
    if "items" in definicion:
        return "array"
    return "any"


def _campos_schema(schema: dict, componentes: dict) -> tuple:
    schema = _resolver(schema, componentes)
    if schema.get("type") == "array" or "items" in schema:
        schema = _resolver(schema.get("items") or {}, componentes)
    propiedades = schema.get("properties") or {}
    requeridos = set(schema.get("required") or ())
    campos = [
        Campo(nombre, _tipo_de(definicion, componentes), nombre in requeridos)
        for nombre, definicion in propiedades.items()
    ]
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


def _divergencias(campos_codigo: tuple, campos_spec: tuple, metodo: str, ruta: str, lado: str) -> list:
    salida = []
    codigo = {c.nombre: c for c in campos_codigo}
    spec = {c.nombre: c for c in campos_spec}
    # Un `alias_generator` renombra campos con un valor que no esta en el AST: un nombre que
    # solo aparece en un lado no es deriva, es un nombre que este lector no puede calcular.
    con_generador = [c for c in campos_codigo if c.alias_no_medido]
    for nombre in sorted(codigo):
        if nombre not in spec:
            if con_generador:
                salida.append(Hallazgo("ALIAS_NO_MEDIDO", metodo, ruta,
                                       f"{lado} '{nombre}': el modelo declara `alias_generator`,"
                                       " el nombre publicado no se puede calcular desde el AST", "WARN"))
            else:
                salida.append(Hallazgo("CAMPO_FALTA_EN_SPEC", metodo, ruta,
                                       f"{lado} '{nombre}': el servidor lo declara; el contrato no", "ROJO"))
            continue
        if codigo[nombre].tipo != spec[nombre].tipo:
            salida.append(Hallazgo("TIPO_DIVERGENTE", metodo, ruta,
                                   f"{lado} '{nombre}': servidor={codigo[nombre].tipo} · contrato={spec[nombre].tipo}", "ROJO"))
        if lado != "respuesta" and codigo[nombre].requerido != spec[nombre].requerido:
            salida.append(Hallazgo("REQUERIDO_DIVERGENTE", metodo, ruta,
                                   f"{lado} '{nombre}': servidor={'requerido' if codigo[nombre].requerido else 'opcional'}"
                                   f" · contrato={'requerido' if spec[nombre].requerido else 'opcional'}", "ROJO"))
    for nombre in sorted(spec):
        if nombre not in codigo:
            if con_generador:
                salida.append(Hallazgo("ALIAS_NO_MEDIDO", metodo, ruta,
                                       f"{lado} '{nombre}': el contrato lo publica y el modelo lo renombra"
                                       " con un `alias_generator` que no se puede calcular", "WARN"))
            else:
                salida.append(Hallazgo("CAMPO_FALTA_EN_CODIGO", metodo, ruta,
                                       f"{lado} '{nombre}': el contrato lo declara; el servidor no", "ROJO"))
    return salida


def comparar(codigo: Superficie, spec: Superficie) -> list:
    hallazgos = []
    indice_codigo, indice_spec = _indice(codigo), _indice(spec)
    for clave in sorted(indice_codigo, key=lambda k: (k[1], k[0])):
        if clave not in indice_spec:
            op = indice_codigo[clave]
            if op.condicional:
                hallazgos.append(Hallazgo("CONDICIONAL_NO_PUBLICADA", op.metodo, op.ruta,
                                          f"el servidor la monta bajo una condicion que no se evaluo; ver {op.fuente}", "WARN"))
            else:
                hallazgos.append(Hallazgo("FALTA_EN_SPEC", op.metodo, op.ruta,
                                          f"el servidor la declara en {op.fuente}", "ROJO"))
    for clave in sorted(indice_spec, key=lambda k: (k[1], k[0])):
        if clave not in indice_codigo:
            hallazgos.append(Hallazgo("FALTA_EN_CODIGO", clave[0], clave[1],
                                      "el contrato la publica y el servidor no la declara", "ROJO"))
    for clave in sorted(set(indice_codigo) & set(indice_spec), key=lambda k: (k[1], k[0])):
        a, b = indice_codigo[clave], indice_spec[clave]
        hallazgos.extend(_divergencias(a.peticion, b.peticion, a.metodo, a.ruta, "cuerpo"))
        hallazgos.extend(_divergencias(a.respuesta, b.respuesta, a.metodo, a.ruta, "respuesta"))
        exitos_codigo = {c for c in a.codigos if 200 <= c < 300}
        exitos_spec = {c for c in b.codigos if 200 <= c < 300}
        if exitos_codigo != exitos_spec:
            hallazgos.append(Hallazgo("CODIGO_DIVERGENTE", a.metodo, a.ruta,
                                      f"servidor={sorted(exitos_codigo)} · contrato={sorted(exitos_spec)}", "WARN"))
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
