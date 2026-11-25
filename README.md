# spec-drift-radar

Compara la superficie real de un servidor Python con el contrato OpenAPI que publica, sin
levantar el servidor, sin red y sin dependencias de runtime.

Un PR renombra `cliente` a `customer` en el backend, cambia `id` de `integer` a `string` o
olvida documentar un endpoint nuevo: el frontend sigue compilando en verde hasta que revienta
con un cliente real delante. Este radar lee el **AST** del servidor y el **JSON OpenAPI** y
publica la diferencia con su marco.

```console
$ spec-drift-radar --servidor api/ --openapi openapi.json
spec-drift-radar · la superficie del servidor contra el contrato que publica

MARCO DEL SERVIDOR   archivos_py=1 · modelos=3 · rutas_codigo=2 · sin_lector=typescript,yaml
CAMPOS LEIDOS         rutas_con_campos=2 · rutas_sin_campos=0 · prefijos_no_medidos=0 · inclusiones_condicionales=0 · prefijo_base=NO_DECLARADO
MARCO DEL CONTRATO   openapi=3.1.0 · titulo=Pedidos · version=2026-11 · rutas_spec=3

HALLAZGOS  rojos=6 · avisos=1 · pares_comparados=2
  [ROJO] FALTA_EN_CODIGO        POST    /pedidos/{pedido_id}/cancelar    el contrato la publica y el servidor no la declara
  [ROJO] CAMPO_FALTA_EN_SPEC    POST    /pedidos                         cuerpo 'cliente': el servidor lo declara; el contrato no
  [ROJO] REQUERIDO_DIVERGENTE   POST    /pedidos                         cuerpo 'prioridad': servidor=opcional · contrato=requerido
  [ROJO] CAMPO_FALTA_EN_CODIGO  POST    /pedidos                         cuerpo 'customer': el contrato lo declara; el servidor no
  [ROJO] TIPO_DIVERGENTE        POST    /pedidos                         respuesta 'id': servidor=integer · contrato=string
  [WARN] CODIGO_DIVERGENTE      POST    /pedidos                         servidor=[201] · contrato=[200]
  [ROJO] TIPO_DIVERGENTE        GET     /pedidos/{pedido_id}             respuesta 'id': servidor=integer · contrato=string

RC 1 · deriva medida: el contrato y el servidor no dicen lo mismo
```

Esa salida es real: `tests/` reproduce el mismo caso como fixture y lo comprueba.

## Instalación

Sin PyPI todavía. Instalación directa desde GitHub (reproducible, sin nombre corto):

```console
$ pip install git+https://github.com/alexisromtz-lab/spec-drift-radar.git
$ uvx --from git+https://github.com/alexisromtz-lab/spec-drift-radar.git spec-drift-radar --self-test
```

Requiere Python ≥ 3.11. **Cero dependencias de runtime**: sólo `ast`, `json`, `pathlib` y
`argparse`. `pytest` se usa únicamente para la suite.

## Uso

```console
# 1. comparar el código con el contrato publicado
$ spec-drift-radar --servidor api/ --openapi openapi.json

# 2. el mismo resultado en JSON, con el marco completo y los hallazgos
$ spec-drift-radar --servidor api/ --openapi openapi.json --json

# 3. derivar el contrato desde el servidor y escribirlo (arranque de una API sin spec)
$ spec-drift-radar --servidor api/ --emitir openapi.json

# 4. anteponer un prefijo de montaje que el codigo no resuelve (caso raro, ver «Prefijos»)
$ spec-drift-radar --servidor api/ --prefijo /api/v1 --emitir openapi.json

# 5. verificar el instrumento consigo mismo (emite, compara, muta y detecta)
$ spec-drift-radar --self-test
```

`--emitir` seguido de una comparación contra el archivo emitido da **RC 0**: los dos lectores
son el mismo predicado visto desde dos representaciones, y el self-test lo exige en cada
ejecución.

## Códigos de salida

| RC | significado |
|---|---|
| `0` | las dos superficies coinciden y se midieron completas |
| `1` | deriva medida (hallazgo `ROJO`): el contrato y el servidor no dicen lo mismo |
| `2` | `NO_MEDIDO`: universo vacío o lectura incompleta. **Nunca degrada a `0`** |
| `20` | argumentos inválidos (argparse) |

Un universo vacío **no** se compara: cero rutas de un lado no es «deriva total», es ausencia
de lectura, y publicarlo como RC 1 convertiría un archivo equivocado en un hallazgo. El RC 1
sólo se emite cuando las dos superficies existen.

## Qué mide y qué no

**Lee**

- Servidor: AST de Python. Routers estilo FastAPI (`@router.get/post/...` con ruta literal),
  modelos Pydantic por herencia declarada (`BaseModel`), `Optional[...]`, valores por defecto
  y anotaciones `list[...]` / `dict[...]`.
- Contrato: OpenAPI 3.x en JSON. `paths`, `requestBody`, `responses` 2xx, `components.schemas`
  y `$ref` (con resolución acotada a 8 saltos para no colgarse en una referencia circular).

**No lee (y lo declara en cada salida, campo `sin_lector`)**

- TypeScript y YAML: no hay parser en la caja de herramientas estándar, y un regex no es un
  parser. Se publica como `sin_lector`, nunca como «cero hallazgos».
- Rutas construidas en tiempo de ejecución (`f"{PREFIJO}/x"`): se anotan en `sin_medir` y
  fuerzan RC 2.
- Condiciones de arranque (`if settings.ENV == "development": app.include_router(...)`): el AST
  no evalúa la condición, así que la ruta se publica con `CONDICIONAL_NO_PUBLICADA` y el número
  de inclusiones condicionales viaja en `inclusiones_condicionales`. No es deriva: es un estado
  del servidor que este instrumento no puede decidir.
- Frameworks cuyo enrutado no vive en un decorador con ruta literal.

**Prefijos.** El prefijo estático de un router se compone (`APIRouter(prefix="/usuarios")` +
`@router.get("/{id}")` ⇒ `/usuarios/{id}`). Un `include_router(..., prefix=settings.API_V1_STR)`
se resuelve **sin ejecutar el código**: la herramienta indexa los módulos del árbol, sigue los
`import` entre ellos, propaga constantes (`API_V1_STR = "/api/v1"`, atributos de clase y literales
de módulo) y monta el grafo de `include_router` desde los routers que nadie incluye. Los prefijos
así resueltos se publican en `prefijos_no_medidos=0`; si alguno no se puede resolver, no se
adivina: cuenta en `prefijos_no_medidos` y obliga a declarar `--prefijo`, que **antepone** ese
valor en vez de impedir la corrida.

## Clases de hallazgo

| clase | severidad | qué significa |
|---|---|---|
| `FALTA_EN_SPEC` | ROJO | la ruta existe en el servidor y no en el contrato |
| `FALTA_EN_CODIGO` | ROJO | el contrato publica una ruta que el servidor no declara |
| `CAMPO_FALTA_EN_SPEC` | ROJO | el servidor envía un campo que el contrato no documenta |
| `CAMPO_FALTA_EN_CODIGO` | ROJO | el contrato documenta un campo que el servidor no tiene |
| `TIPO_DIVERGENTE` | ROJO | mismo campo, distinto tipo (`integer` vs `string`) |
| `REQUERIDO_DIVERGENTE` | ROJO | mismo campo, distinto `required` |
| `CODIGO_DIVERGENTE` | WARN | distinto código de éxito (`201` vs `200`); no fuerza RC 1 |
| `CONDICIONAL_NO_PUBLICADA` | WARN | el servidor monta la ruta dentro de un `if`/`try` que el AST no evalúa; no fuerza RC 1 |

## Estado medido

- `v0.3.0` · 37 tests · `--self-test` RC 0 · CI en 3.11 / 3.12 / 3.13.
- **Oraculo diferencial contra el generador de FastAPI** (`fastapi` 0.165.0, `sqlmodel` 0.0.50):
  sobre una app con herencia `SQLModel`, `status.HTTP_201_CREATED`, `Optional[str]` y routers
  con prefijo, el contrato emitido por este radar y el `app.openapi()` real del framework
  coinciden campo por campo ⇒ **RC 0**. Esa corrida encontró cuatro defectos del lector que hoy
  son tests (`TestContraElGeneradorDeFastAPI`): `status_code` por atributo, `anyOf` con `null`,
  los códigos 4xx que el generador añade solo, y hallazgos sin decir de qué lado venían.
- **Comparación contra el contrato real de un proyecto externo**
  ([`fastapi/full-stack-fastapi-template`](https://github.com/fastapi/full-stack-fastapi-template)
  @ `77e550a`, 16-Nov-2026): se importó la app real y se volcó su `app.openapi()` — el oráculo de
  verdad: **14 rutas / 22 operaciones**. El radar, sobre los mismos 25 archivos, publicó 21 modelos
  y 23 rutas de código, resolvió el prefijo `/api/v1` **sin `--prefijo`** (`prefijos_no_medidos=0`)
  y comparó **22 pares** con **0 rojos y 1 aviso** declarado (`CONDICIONAL_NO_PUBLICADA` en
  `/api/v1/private/users/`, que el template sólo monta si `FASTAPI_ENV == "development"`) ⇒ **RC 0**.
  La corrida inicial daba **48 rojos** y todos eran defectos de este lector; hoy son tests:
  la barra final de una ruta, los parámetros *keyword-only* (`def f(*, session, user_in)`), un
  `Field(min_length=1)` que no es un valor por defecto, `EmailStr` como `string`, `required` en la
  respuesta (donde no es contrato de cliente), el modelo de respuesta declarado sólo en la
  anotación de retorno, y las inclusiones condicionales.
- **Dos defectos del upstream**, medidos y no corregidos aquí: `backend/app/api/deps.py:36` no
  compila (`except InvalidTokenError, ValidationError:` es sintaxis de Python 2) y
  `backend/app/models.py:55` anota `list[Item]` antes de definir `Item` sin
  `from __future__ import annotations` ⇒ `NameError` al importar. Contra el clon intacto este
  radar publica **RC 2** (`no parsea`) en lugar de saltarse el archivo en silencio.

Reproducir la corrida externa (incluye el parche mínimo para poder importar la app):

```console
$ git clone --depth 1 https://github.com/fastapi/full-stack-fastapi-template.git /tmp/tpl
$ git -C /tmp/tpl rev-parse HEAD   # 77e550a73bb49dd2967622c5ed11bf7a49603c93
$ cp -r /tmp/tpl/backend /tmp/tpl_oraculo
$ # 1) parche de sintaxis en /tmp/tpl_oraculo/app/api/deps.py:36
$ # 2) from __future__ import annotations en /tmp/tpl_oraculo/app/models.py
$ # 3) mkdir /tmp/tpl_oraculo/app/frontend   (lo exige FastAPI.frontend())
$ python3.12 -m venv /tmp/venv_tpl2 && /tmp/venv_tpl2/bin/pip install \
    fastapi[standard] sqlmodel pydantic-settings sentry-sdk pyjwt pwdlib[argon2,bcrypt] \
    psycopg[binary] emails jinja2
$ (cd /tmp/tpl_oraculo && SECRET_KEY=x PROJECT_NAME=x FIRST_SUPERUSER=a@b.c \
    FIRST_SUPERUSER_PASSWORD=x DATABASE_URL=postgresql://x/x \
    /tmp/venv_tpl2/bin/python -c 'import json;from app.main import app;print(json.dumps(app.openapi()))' \
    > /tmp/tpl_verdadero.json)
$ spec-drift-radar --servidor /tmp/tpl_oraculo/app --openapi /tmp/tpl_verdadero.json
# RC 0 · archivos_py=25 · modelos=21 · pares_comparados=22 · rojos=0 · avisos=1
```

## Familia

Las cuatro herramientas comparten el mismo vocabulario: *marco declarado*, *NO_MEDIDO* y
*conciliación por pareja con base propia*.

- [composicion-core](https://github.com/alexisromtz-lab/composicion-core) — núcleo compartido,
  contrato de medición con marco declarado (`ResultadoMedido`).
- [py-test-shadow-guard](https://github.com/alexisromtz-lab/py-test-shadow-guard) — detecta
  tests que se sobrescriben en silencio tras un merge limpio.
- [merge-lattice-governor](https://github.com/alexisromtz-lab/merge-lattice-governor) — plan
  exacto del orden de aterrizaje de ramas bajo solvencia por prefijo.
- **spec-drift-radar** — deriva entre el servidor y su contrato publicado.

## Licencia

MIT. Ver [LICENSE](LICENSE).
