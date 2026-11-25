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
    "timedelta": "string",
    "time": "string",
    "UploadFile": "string",
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
    cuerpo_manual: bool = False
    cuerpo_por_dependencia: bool = False


@dataclass(frozen=True)
class Superficie:
    etiqueta: str
    operaciones: tuple
    archivos: int
    modelos: int
    sin_medir: tuple
    marco: dict
    # Definiciones nombradas del contrato (`components.schemas` o `definitions`), con sus
    # campos de primer nivel. En un contrato como el de Kubernetes la deriva real vive aqui:
    # las operaciones solo devuelven envoltorios (`PodList`) y el modelo esta un `$ref` mas abajo.
    definiciones: tuple = ()


@dataclass(frozen=True)
class Hallazgo:
    clase: str
    metodo: str
    ruta: str
    detalle: str
    severidad: str
    # Campo al que se refiere el hallazgo, cuando lo hay: dos hallazgos con la misma
    # clase y el mismo campo son el mismo hecho visto desde dos locus, y se publican una vez.
    campo: str = ""


# --- lector 1: AST del servidor ---------------------------------------------

#: Forma de servidor que este lector reconoce. Se publica en el marco porque una
#: superficie vacia no distingue "el servidor no declara rutas" de "aqui no hay
#: FastAPI", y esas dos cosas no se contestan igual.
LECTOR = "fastapi"

#: Marca con la que el contrato emitido publica una ruta que el AST vio dentro de un `if`.
CONDICIONAL = "x-spec-drift-radar-condicional"


GENERICOS_DE_UN_SOLO_ARG = ("Optional[", "list[", "List[", "Sequence[", "set[", "Set[", "tuple[", "Tuple[")


def _anotada(annotation):
    """`Annotated[T, Body(embed=True)]` -> (T, [metadatos]). La metadata es donde FastAPI vive."""
    if isinstance(annotation, ast.Subscript) and ast.unparse(annotation.value).split(".")[-1] == "Annotated":
        rebanada = annotation.slice
        elementos = rebanada.elts if isinstance(rebanada, ast.Tuple) else [rebanada]
        if elementos:
            return elementos[0], [e for e in elementos[1:] if isinstance(e, ast.Call)]
    return annotation, []


def _desnudo(annotation):
    """Quita `Annotated`, `Optional`, `| None` y los genericos de un solo argumento del tipo."""
    actual = annotation
    for _ in range(8):
        actual, _meta = _anotada(actual)
        if isinstance(actual, ast.BinOp) and isinstance(actual.op, ast.BitOr):
            siguiente = actual.right if isinstance(actual.right, ast.Constant) and actual.right.value is None else actual.left
            if ast.unparse(siguiente) in ("None", "NoneType"):
                siguiente = actual.left
            actual = siguiente
            continue
        texto = ast.unparse(actual).replace(" ", "")
        prefijo = next((g for g in GENERICOS_DE_UN_SOLO_ARG if texto.startswith(g) and texto.endswith("]")), None)
        if prefijo is None:
            break
        actual = ast.parse(texto[len(prefijo) : -1], mode="eval").body
    return actual


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
    if base == "Literal" and t.endswith("]"):
        # `Literal["person"]` se publica como `type: string` (con `const`): el tipo medible es el de sus valores.
        tipos = {_tipo_de_literal(valor) for valor in _partir_argumentos(t[len("Literal[") : -1])}
        return (tipos.pop() if len(tipos) == 1 else "any"), opcional
    if base in modelos:
        return "object", opcional
    if base in ESCALARES:
        return ESCALARES[base], opcional
    escalares = {nombre.lower(): tipo for nombre, tipo in ESCALARES.items()}
    return escalares.get(base.lower(), base.lower()), opcional


def _tipo_de_literal(valor: str) -> str:
    """Tipo JSON de un valor de `Literal[...]`: `"person"` publica `string`, `1` publica `integer`."""
    v = valor.strip()
    if len(v) > 1 and v[0] in "\"'" and v[-1] == v[0]:
        return "string"
    if v in ("True", "False"):
        return "boolean"
    try:
        numero = float(v)
        return "integer" if numero.is_integer() and "." not in v else "number"
    except ValueError:
        return "any"


def _partir_argumentos(texto: str) -> list:
    """Parte argumentos de un generico respetando los corchetes anidados."""
    partes, profundidad, actual = [], 0, ""
    for caracter in texto:
        if caracter in "[(":
            profundidad += 1
        elif caracter in "])":
            profundidad -= 1
        if caracter == "," and profundidad == 0:
            partes.append(actual.strip())
            actual = ""
            continue
        actual += caracter
    if actual.strip():
        partes.append(actual.strip())
    return partes


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
                if any(ast.unparse(d).split(".")[-1].startswith("dataclass") for d in nodo.decorator_list):
                    bases.add("BaseModel")  # `@dataclass` de pydantic publica esquema igual que un modelo
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
    if nombre not in ("Field", "Column", "Body", "Form", "File", "field"):
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
    """El modelo que un tipo publica, dentro de `list[...]`, `Optional[...]` o `Annotated[...]`."""
    nodo = _desnudo(ast.parse(anotacion, mode="eval").body)
    base = ast.unparse(nodo).split(".")[-1]
    return base if base in modelos else None


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


def _omitida(dec: ast.Call) -> bool:
    """`include_in_schema=False` deja la ruta fuera del contrato: no es una ruta, es un handler."""
    for kw in dec.keywords:
        if kw.arg == "include_in_schema" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
            return True
    return False


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


def _metadatos_de(annotation) -> list:
    return _anotada(annotation)[1]


def _llamada_del_parametro(argumento, defecto) -> tuple:
    """(nombre de la llamada que da semantica al parametro, nodo de la llamada).

    `Annotated[Session, Depends(get_db)]` pone la dependencia en la metadata; sin mirarla, cada
    `Depends` del estilo moderno se leia como cuerpo.
    """
    for meta in _metadatos_de(argumento.annotation):
        nombre = _llamada(meta)
        if nombre in NO_VIAJAN_EN_EL_CUERPO or nombre in ("Body", "Form", "File"):
            return nombre, meta
    return _llamada(defecto), defecto


def _opciones_de_cuerpo(llamada) -> tuple:
    alias, embed = None, False
    if isinstance(llamada, ast.Call):
        for kw in llamada.keywords:
            if kw.arg == "alias" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                alias = kw.value.value
            if kw.arg == "embed" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                embed = True
    return alias, embed


FORMULARIOS_POR_DEPENDENCIA = {"OAuth2PasswordRequestForm", "OAuth2PasswordRequestFormStrict"}


def _formulario_por_dependencia(nodo) -> bool:
    """`depend: OAuth2PasswordRequestForm = Depends()` publica 6 campos de formulario que no estan
    en el AST: la clase vive en `fastapi.security`. Adivinar sus campos seria inventar el contrato."""
    for argumento, _defecto in _parametros(nodo):
        for fuente in (argumento.annotation, _anotada(argumento.annotation)[0]):
            if fuente is None:
                continue
            base = ast.unparse(fuente).replace(" ", "").split("[")[0].split(".")[-1]
            if base in FORMULARIOS_POR_DEPENDENCIA:
                return True
    return False


def _cuerpo_de_peticion(nodo, modelos) -> tuple:
    """El cuerpo son los campos del modelo anotado, salvo que el parametro sea una dependencia.

    `articles_repo: ArticlesRepository = Depends(get_repository(...))` no viaja en el cuerpo:
    leerlo como cuerpo convertia cada `Depends` con modelo en 4 campos fantasma por operacion.
    Con **mas de un** parametro de cuerpo, FastAPI embebe cada uno bajo su nombre: sin eso, dos
    modelos se fundian en una sola lista de campos que el contrato nunca publica.
    """
    partes = []
    for argumento, defecto in _parametros(nodo):
        llamada, nodo_llamada = _llamada_del_parametro(argumento, defecto)
        if llamada in NO_VIAJAN_EN_EL_CUERPO:
            continue
        anotada, _meta = _anotada(argumento.annotation)
        nombre_modelo = _modelo_de(ast.unparse(argumento.annotation), modelos)
        # Requerido = la firma no da default (`= None` lo da) y la llamada tampoco (`Body(None)` lo da).
        base = defecto is None or _requerido_en_llamada(defecto)
        formulario = llamada in ("Form", "File") or "UploadFile" in ast.unparse(anotada)
        if llamada in ("Body", "Form", "File") or formulario:
            alias, embed = _opciones_de_cuerpo(nodo_llamada)
            requerido = base and _requerido_en_llamada(nodo_llamada) if isinstance(nodo_llamada, ast.Call) else base
            tipo = "object" if nombre_modelo else _tipo_py(ast.unparse(anotada), modelos)[0]
            partes.append((alias or argumento.arg, nombre_modelo, requerido, embed, formulario, tipo))
            continue
        if nombre_modelo:
            partes.append((argumento.arg, nombre_modelo, base, False, False, "object"))

    if not partes:
        return ()
    if len(partes) == 1 and not partes[0][3] and partes[0][1]:
        return modelos[partes[0][1]]
    if len(partes) == 1 and not partes[0][3] and partes[0][5] != "object" and not partes[0][4]:
        return ()  # un escalar suelto en `Body()` es el cuerpo entero, sin propiedades nombradas
    if all(parte[4] for parte in partes):
        # Formulario: los campos no se embeben bajo el nombre del parametro, se publican sueltos,
        # y un modelo de formulario ni siquiera llega al contrato (limite de FastAPI, no nuestro).
        campos = []
        for _n, modelo, requerido, _e, _f, tipo in partes:
            campos += list(modelos[modelo]) if modelo else [Campo(_n, tipo, requerido)]
        return tuple(campos)
    return tuple(Campo(nombre, tipo, requerido) for nombre, _m, requerido, _e, _fav, tipo in partes)


def _sin_convertidor(camino: str) -> str:
    """`/files/{file_path:path}` se publica como `/files/{file_path}`: el convertidor no viaja."""
    return re.sub(r"\{([^{}:]+):[^{}]+\}", r"{\1}", camino)


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
        self.montados = set()
        self.retrollamadas = set()
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
                for kw in nodo.keywords:
                    if kw.arg == "callbacks":
                        # `callbacks=router.routes` publica esas rutas bajo el arbol del llamante,
                        # no en `paths`: leerlas como rutas propias inventa una ruta que no existe.
                        valor = kw.value.value if isinstance(kw.value, ast.Attribute) else kw.value
                        if isinstance(valor, ast.Name):
                            self.retrollamadas.add((modulo, valor.id))
                if nodo.func.attr == "mount" and len(nodo.args) >= 2:
                    # `app.mount("/subapi", subapi)` no publica las rutas del hijo en el esquema del padre.
                    montado = self.referencia(nodo.args[1], modulo) if len(nodo.args) >= 2 else None
                    if montado is not None:
                        self.montados.add(montado)
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

    operaciones, vistas, declarados, por_dep = [], {}, 0, 0
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
                claves = {kw.arg for kw in dec.keywords}
                if _omitida(dec):
                    continue
                codigos, respuesta = _kwargs_decorador(dec, modelos)
                if not respuesta:
                    respuesta = _anotacion_de_retorno(nodo, modelos)
                peticion = _cuerpo_de_peticion(nodo, modelos)
                if camino is None:
                    sin_medir.append(f"{ruta}:{nodo.lineno} ruta no literal en {metodo}")
                    continue
                nombre_router = ast.unparse(dec.func.value)
                if (modulo, nombre_router) in indice.montados or (modulo, nombre_router) in indice.retrollamadas:
                    continue
                if isinstance(dec.func.value, ast.Attribute) and dec.func.value.attr == "webhooks":
                    # `@app.webhooks.post("x")` se publica en la seccion `webhooks`, no en `paths`.
                    gancho = _sin_convertidor(f"webhooks/{camino}")
                    if (metodo, gancho) not in vistas:
                        vistas[(metodo, gancho)] = False
                        operaciones.append(Operacion(metodo, gancho, peticion, respuesta, codigos,
                                                     f"{ruta}:{nodo.lineno}", False, False))
                    continue
                propio = indice.prefijos.get((modulo, nombre_router), "")
                for prefijo, condicional in sorted((montaje.get((modulo, nombre_router)) or {"": False}).items()):
                    completa = _sin_convertidor(_une(prefijo_base, _une(_une(prefijo, propio), camino)))
                    if (metodo, completa) in vistas:
                        if not condicional:
                            vistas[(metodo, completa)] = False
                        continue
                    vistas[(metodo, completa)] = condicional
                    manual = "openapi_extra" in claves
                    if manual:
                        peticion = ()
                        declarados += 1
                    por_dependencia = _formulario_por_dependencia(nodo)
                    if por_dependencia:
                        por_dep += 1
                    operaciones.append(Operacion(metodo, completa, peticion, respuesta, codigos,
                                                 f"{ruta}:{nodo.lineno}", condicional, manual, por_dependencia))

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
        "cuerpos_declarados_a_mano": declarados,
        "cuerpos_no_medidos_por_dependencia": por_dep,
        "lector": LECTOR,
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


def _desenvuelto(schema: dict, componentes: dict, profundidad: int = 0) -> dict:
    """El schema del que cuelgan los campos, sin `$ref`, `anyOf` de union ni envoltorio de array.

    `Item | None` se publica como `anyOf: [Item, null]`: la rama que no es nula es el modelo.
    """
    schema = _resolver(schema, componentes)
    if not isinstance(schema, dict) or profundidad > 4:
        return {}
    for clave in ("anyOf", "oneOf"):
        ramas = [r for r in (schema.get(clave) or ()) if (_resolver(r, componentes) or {}).get("type") != "null"]
        if ramas:
            # Pydantic v2 envuelve `Profile | None` en `anyOf: [{not: ...}, {anyOf: [Profile]}]`:
            # la rama con campos puede estar a dos `anyOf` de distancia, no en el primer nivel.
            resueltas = [_desenvuelto(rama, componentes, profundidad + 1) for rama in ramas]
            con_propiedades = [rama for rama in resueltas if rama.get("properties")]
            if con_propiedades:
                return con_propiedades[0]
            return resueltas[0] if resueltas else {}
    if schema.get("type") == "array" or "items" in schema:
        return _desenvuelto(schema.get("items") or {}, componentes, profundidad + 1)
    return schema


def _propiedades_declaradas(schema: dict, componentes: dict, profundidad: int = 0) -> tuple:
    """Propiedades y requeridos de un schema, **fusionando** `allOf` con las propias.

    FastAPI publica un cuerpo con varios `Body()`/`Form()` como un schema con `allOf` de los modelos
    embebidos **y** sus `properties` escalares en el mismo nivel. Quedarse con una sola rama borraba
    los escalares del contrato y convertia un contrato completo en una deriva falsa.
    """
    schema = _desenvuelto(schema, componentes, profundidad)
    propiedades, requeridos = {}, set()
    if not isinstance(schema, dict) or profundidad > 4:
        return propiedades, requeridos
    for rama in schema.get("allOf") or ():
        heredadas, obligatorias = _propiedades_declaradas(rama, componentes, profundidad + 1)
        propiedades.update(heredadas)
        requeridos |= obligatorias
    propiedades.update(schema.get("properties") or {})
    requeridos |= set(schema.get("required") or ())
    return propiedades, requeridos


def _campos_schema(schema: dict, componentes: dict) -> tuple:
    propiedades, requeridos = _propiedades_declaradas(schema, componentes)
    return tuple(
        Campo(nombre, _tipo_de(definicion, componentes), nombre in requeridos)
        for nombre, definicion in propiedades.items()
    )


DIALECTOS = ("openapi", "swagger")


def _dialecto(documento: dict) -> tuple:
    """Dialecto declarado por el propio documento, o `NO_MEDIDO`.

    Swagger 2.0 y OpenAPI 3.x no comparten ni el sitio de los esquemas (`definitions` frente a
    `components.schemas`) ni el del cuerpo (`parameters[in=body]` frente a `requestBody`). Leer
    un 2.0 con el lector de 3.x no falla: devuelve 865 operaciones con cero campos, que es un
    verde barato sobre un contrato que nunca se miro.
    """
    if isinstance(documento.get("openapi"), str):
        return "openapi", documento["openapi"]
    if str(documento.get("swagger")) == "2.0":
        return "swagger", "2.0"
    return "desconocido", "NO_MEDIDO"


def _esquemas_declarados(documento: dict, dialecto: str) -> dict:
    if dialecto == "swagger":
        return documento.get("definitions") or {}
    return ((documento.get("components") or {}).get("schemas")) or {}


def _cuerpo(op: dict, dialecto: str = "openapi") -> dict:
    """El cuerpo declarado, sea JSON o de formulario: los nombres se comparan igual."""
    if dialecto == "swagger":
        for parametro in op.get("parameters") or ():
            if isinstance(parametro, dict) and parametro.get("in") == "body":
                return parametro.get("schema") or {}
        return {}
    contenido = (op.get("requestBody") or {}).get("content") or {}
    for tipo, valor in contenido.items():
        if "json" in tipo:
            return valor.get("schema") or {}
    for valor in contenido.values():
        if isinstance(valor, dict) and valor.get("schema"):
            return valor["schema"]
    return {}


def _respuesta(op: dict, dialecto: str = "openapi") -> dict:
    respuestas = op.get("responses") or {}
    exitosas = sorted(
        (int(c), v) for c, v in respuestas.items() if str(c).isdigit() and 200 <= int(c) < 300
    )
    if not exitosas:
        return {}
    if dialecto == "swagger":
        return (exitosas[0][1] or {}).get("schema") or {}
    contenido = (exitosas[0][1] or {}).get("content") or {}
    for tipo, valor in contenido.items():
        if "json" in tipo:
            return valor.get("schema") or {}
    return {}


def superficie_openapi(documento: dict) -> Superficie:
    if not isinstance(documento, dict):
        return Superficie("openapi", (), 0, 0, ("el documento no es un objeto JSON",), {})
    dialecto, version_dialecto = _dialecto(documento)
    componentes = _esquemas_declarados(documento, dialecto)
    # `webhooks` es una seccion hermana de `paths` (OpenAPI 3.1): mismo lector, otro espacio de nombres.
    secciones = [("", documento.get("paths") or {}), ("webhooks/", documento.get("webhooks") or {})]
    operaciones = []
    for prefijo, caminos in secciones:
        for camino, item in (caminos.items() if isinstance(caminos, dict) else ()):
            if not isinstance(item, dict):
                continue
            for metodo, op in item.items():
                if metodo.lower() not in METODOS or not isinstance(op, dict):
                    continue
                codigos = tuple(sorted(int(c) for c in (op.get("responses") or {}) if str(c).isdigit()))
                operaciones.append(
                    Operacion(
                        metodo.lower(),
                        _sin_convertidor(f"{prefijo}{camino}"),
                        _campos_schema(_cuerpo(op, dialecto), componentes),
                        _campos_schema(_respuesta(op, dialecto), componentes),
                        codigos,
                        "openapi",
                        condicional=bool(op.get(CONDICIONAL)),
                    )
                )
    info = documento.get("info") or {}
    marco = {
        "openapi": documento.get("openapi") or documento.get("swagger") or "NO_MEDIDO",
        "dialecto": f"{dialecto} {version_dialecto}" if dialecto != "desconocido" else "NO_MEDIDO",
        "titulo": info.get("title") or "NO_MEDIDO",
        "version_declarada": info.get("version") or "NO_MEDIDO",
        "rutas_spec": len(operaciones),
        "esquemas": len(componentes),
    }
    if not operaciones:
        sin_medir = ("el documento no declara ninguna operacion",)
    elif dialecto not in DIALECTOS:
        # Un dialecto que este lector no conoce deja el documento sin cuerpos: las rutas
        # se comparan, los mensajes no, y eso no es un cero.
        sin_medir = ("el documento no se declara OpenAPI 3.x ni Swagger 2.0:"
                     " las rutas se comparan y los cuerpos no se leen",)
    else:
        sin_medir = ()
    definiciones = tuple(
        (nombre, _campos_schema(esquema, componentes))
        for nombre, esquema in sorted(componentes.items())
        if isinstance(esquema, dict)
    )
    marco["profundidad_campos"] = 1
    return Superficie("openapi", tuple(operaciones), 1, len(componentes), tuple(sin_medir), marco,
                      definiciones)


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
    if not codigo.operaciones and spec.operaciones:
        # Cero rutas leidas con contrato poblado no es "el servidor no las declara":
        # es que este lector no sabe leer ese servidor. Publicar una FALTA_EN_CODIGO
        # por ruta seria inventar una deriva del sujeto con un defecto del instrumento.
        return [Hallazgo("SERVIDOR_NO_LEIDO", "*", "*",
                         f"el lector de rutas solo entiende {LECTOR} y no encontro ninguna;"
                         f" el contrato publica {len(spec.operaciones)} operaciones: las rutas no se comparan",
                         "WARN")]
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
        if a.cuerpo_por_dependencia or b.cuerpo_por_dependencia:
            hallazgos.append(Hallazgo("CUERPO_NO_MEDIDO_POR_DEPENDENCIA", a.metodo, a.ruta,
                                      "el cuerpo viaja en un formulario de seguridad que este lector no calcula"
                                      " desde el AST; los campos del contrato no se comparan", "WARN"))
        elif not (a.cuerpo_manual or b.cuerpo_manual):
            hallazgos.extend(_divergencias(a.peticion, b.peticion, a.metodo, a.ruta, "cuerpo"))
        hallazgos.extend(_divergencias(a.respuesta, b.respuesta, a.metodo, a.ruta, "respuesta"))
        exitos_codigo = {c for c in a.codigos if 200 <= c < 300}
        exitos_spec = {c for c in b.codigos if 200 <= c < 300}
        if exitos_codigo != exitos_spec:
            hallazgos.append(Hallazgo("CODIGO_DIVERGENTE", a.metodo, a.ruta,
                                      f"servidor={sorted(exitos_codigo)} · contrato={sorted(exitos_spec)}", "WARN"))
    return hallazgos


def _deriva_de_campos(viejos: tuple, nuevos: tuple, metodo: str, ruta: str, lado: str) -> list:
    """Deriva de un lado del mensaje entre dos versiones del mismo contrato.

    El veredicto es asimetrico a proposito: lo que rompe a un cliente que hoy
    funciona es perder algo, o que algo pase a ser obligatorio. Anadir campo
    opcional no rompe nada y no puede pesar lo mismo.

    `lado="esquema"` mide un modelo nombrado, del que el contrato no dice si se usa
    como peticion o como respuesta: ahi se asume el uso como peticion, que es el que
    rompe. Se declara porque es una suposicion, no una lectura.
    """
    salida = []
    viejos_por_nombre = {c.nombre: c for c in viejos}
    nuevos_por_nombre = {c.nombre: c for c in nuevos}
    for nombre in sorted(viejos_por_nombre):
        viejo = viejos_por_nombre[nombre]
        if nombre not in nuevos_por_nombre:
            salida.append(Hallazgo("CAMPO_ELIMINADO", metodo, ruta,
                                   f"{lado} '{nombre}': la version anterior lo publicaba y esta no", "ROJO", campo=nombre))
            continue
        nuevo = nuevos_por_nombre[nombre]
        if viejo.tipo != nuevo.tipo:
            salida.append(Hallazgo("TIPO_CAMBIADO", metodo, ruta,
                                   f"{lado} '{nombre}': anterior={viejo.tipo} · nuevo={nuevo.tipo}", "ROJO", campo=nombre))
        if lado != "respuesta" and not viejo.requerido and nuevo.requerido:
            salida.append(Hallazgo("REQUERIDO_ENDURECIDO", metodo, ruta,
                                   f"{lado} '{nombre}': era opcional y ahora es obligatorio", "ROJO", campo=nombre))
        if lado != "respuesta" and viejo.requerido and not nuevo.requerido:
            salida.append(Hallazgo("REQUERIDO_RELAJADO", metodo, ruta,
                                   f"{lado} '{nombre}': era obligatorio y ahora es opcional", "WARN", campo=nombre))
    for nombre in sorted(nuevos_por_nombre):
        if nombre in viejos_por_nombre:
            continue
        if lado != "respuesta" and nuevos_por_nombre[nombre].requerido:
            salida.append(Hallazgo("CAMPO_REQUERIDO_NUEVO", metodo, ruta,
                                   f"{lado} '{nombre}': no existia y ahora es obligatorio", "ROJO", campo=nombre))
        else:
            salida.append(Hallazgo("CAMPO_OPCIONAL_NUEVO", metodo, ruta,
                                   f"{lado} '{nombre}': campo nuevo y opcional", "WARN", campo=nombre))
    return salida


def comparar_contratos(anterior: Superficie, nuevo: Superficie) -> list:
    """Deriva de `anterior` a `nuevo`: las dos superficies son documentos, no codigo.

    Un cambio de tipo cuenta como ruptura aunque pudiera ser un ensanche: este
    lector no conoce la relacion de subtipos entre `number` e `integer`, y decir
    "compatible" sin poder medirlo seria un verde barato.
    """
    hallazgos = []
    viejas, nuevas = _indice(anterior), _indice(nuevo)
    for clave in sorted(viejas, key=lambda k: (k[1], k[0])):
        if clave in nuevas:
            continue
        vieja = viejas[clave]
        if vieja.condicional:
            hallazgos.append(Hallazgo("OPERACION_CONDICIONAL_ELIMINADA", clave[0], clave[1],
                                      f"la version anterior la marcaba `{CONDICIONAL}`: no consta que"
                                      " se publicara, asi que su retirada no se mide como ruptura", "WARN"))
        else:
            hallazgos.append(Hallazgo("OPERACION_ELIMINADA", clave[0], clave[1],
                                      "la version anterior la publicaba y esta no", "ROJO"))
    for clave in sorted(nuevas, key=lambda k: (k[1], k[0])):
        if clave not in viejas:
            hallazgos.append(Hallazgo("OPERACION_ANADIDA", clave[0], clave[1],
                                      "operacion nueva; no rompe a ningun cliente existente", "WARN"))
    for clave in sorted(set(viejas) & set(nuevas), key=lambda k: (k[1], k[0])):
        vieja, nueva = viejas[clave], nuevas[clave]
        hallazgos.extend(_deriva_de_campos(vieja.peticion, nueva.peticion, clave[0], clave[1], "peticion"))
        hallazgos.extend(_deriva_de_campos(vieja.respuesta, nueva.respuesta, clave[0], clave[1], "respuesta"))
        exitos_viejos = {c for c in vieja.codigos if 200 <= c < 300}
        exitos_nuevos = {c for c in nueva.codigos if 200 <= c < 300}
        for codigo in sorted(exitos_viejos - exitos_nuevos):
            hallazgos.append(Hallazgo("CODIGO_ELIMINADO", clave[0], clave[1],
                                      f"el exito {codigo} ya no se publica; un cliente que lo espera se rompe", "ROJO"))
        for codigo in sorted(exitos_nuevos - exitos_viejos):
            hallazgos.append(Hallazgo("CODIGO_ANADIDO", clave[0], clave[1],
                                      f"exito {codigo} nuevo; el cliente anterior no lo contempla", "WARN"))
    hallazgos.extend(_deriva_de_definiciones(anterior.definiciones, nuevo.definiciones))
    # Un campo perdido se publica una sola vez: en la operacion, si ahi se ve, y en el modelo
    # solo cuando ninguna operacion lo incrusta. Contarlo dos veces inflaria el informe con un
    # hecho y dos locus, y el que lee creeria que hay dos rupturas.
    vistos = {(h.clase, h.campo) for h in hallazgos if h.metodo != "esquema" and h.campo}
    return [h for h in hallazgos
            if h.metodo != "esquema" or not h.campo or (h.clase, h.campo) not in vistos]


def _deriva_de_definiciones(viejas: tuple, nuevas: tuple) -> list:
    """Deriva de los modelos nombrados: `metodo='esquema'`, `ruta=<nombre del modelo>`.

    Sin este eje el comparador solo veria el envoltorio de cada operacion. En un contrato
    generado por anotaciones el envoltorio casi nunca cambia: cambia el modelo que referencia.
    """
    salida = []
    indice_viejo, indice_nuevo = dict(viejas), dict(nuevas)
    for nombre in sorted(indice_viejo):
        if nombre not in indice_nuevo:
            salida.append(Hallazgo("ESQUEMA_ELIMINADO", "esquema", nombre,
                                   "el modelo ya no se publica; si algo lo referenciaba, sus campos"
                                   " apareceran como eliminados en la operacion que lo usa", "WARN"))
            continue
        salida.extend(_deriva_de_campos(indice_viejo[nombre], indice_nuevo[nombre], "esquema", nombre, "campo"))
    for nombre in sorted(indice_nuevo):
        if nombre not in indice_viejo:
            salida.append(Hallazgo("ESQUEMA_ANADIDO", "esquema", nombre,
                                   "modelo nuevo; no rompe a ningun cliente existente", "WARN"))
    return salida


def rc_de_contratos(hallazgos, anterior: Superficie, nuevo: Superficie) -> int:
    """1 ruptura medida · 2 NO_MEDIDO · 0 compatible.

    Dos documentos vacios no son dos documentos compatibles: son dos documentos
    sin universo, y ahi un cero tampoco es un cero.
    """
    if not anterior.operaciones or not nuevo.operaciones:
        return 2
    if any(h.severidad == "ROJO" for h in hallazgos):
        return 1
    if anterior.sin_medir or nuevo.sin_medir:
        return 2
    return 0


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
        if op.condicional:
            # La ruta existe en el AST dentro de un `if` que no se evaluo: emitirla sin
            # marca seria publicar como cierto algo que el servidor puede no montar.
            entrada["x-spec-drift-radar-condicional"] = True
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
