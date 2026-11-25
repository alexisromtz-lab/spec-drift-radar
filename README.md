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

# 5. comparar dos versiones del MISMO contrato (deriva de contrato, no de codigo)
$ spec-drift-radar --contrato-anterior main.json --openapi pr.json

# 6. verificar el instrumento consigo mismo (emite, compara, muta y detecta)
$ spec-drift-radar --self-test
```

`--emitir` seguido de una comparación contra el archivo emitido da **RC 0**: los dos lectores
son el mismo predicado visto desde dos representaciones, y el self-test lo exige en cada
ejecución.

## Deriva entre dos versiones del contrato

El modo anterior compara **código contra contrato**. Este compara **contrato contra contrato**:
lo que cambia entre la versión que hoy consumen los clientes y la que va a publicar el PR. Es la
pregunta que se hace antes de mergear, y no necesita el servidor en marcha.

```console
$ spec-drift-radar --contrato-anterior main.json --openapi pr.json
spec-drift-radar · la deriva entre dos versiones del contrato

MARCO ANTERIOR   dialecto=openapi 3.1.0 · titulo=Pedidos · version=1.0.0 · rutas_spec=1 · esquemas=1
MARCO NUEVO      dialecto=openapi 3.1.0 · titulo=Pedidos · version=1.1.0 · rutas_spec=2 · esquemas=1

HALLAZGOS  rojos=1 · avisos=2 · operaciones_comparadas=1 · esquemas_comparados=1
  [WARN] OPERACION_ANADIDA      POST    /pedidos/{pedido_id}/notas       operacion nueva; no rompe a ningun cliente existente
  [ROJO] CAMPO_REQUERIDO_NUEVO  POST    /pedidos                         peticion 'canal': no existia y ahora es obligatorio
  [WARN] CAMPO_OPCIONAL_NUEVO   POST    /pedidos                         respuesta 'canal': campo nuevo y opcional

RC 1 · ruptura medida: un cliente de la version anterior deja de funcionar
```

La asimetría es el veredicto, no un detalle: **perder** algo, o que algo pase a ser
**obligatorio**, rompe clientes (`ROJO`); **añadir** algo opcional o una operación nueva no
rompe nada (`WARN`, no fuerza RC 1). Un cambio de tipo cuenta como ruptura aunque pudiera ser un
ensanche (`integer` → `number`), porque este lector no conoce la relación de subtipos y decir
«compatible» sin poder medirlo sería un verde barato.

Se mide en **dos ejes**, y cada campo se publica **una sola vez**:

| eje | `metodo` del hallazgo | qué cubre |
|---|---|---|
| operaciones | `get`, `post`, … | rutas, cuerpo, respuesta y códigos de éxito de cada operación |
| modelos nombrados | `esquema` | los campos de primer nivel de `components.schemas` / `definitions` |

Sin el segundo eje el radar sólo vería el envoltorio de cada operación. En un contrato generado
por anotaciones el envoltorio casi nunca cambia: **cambia el modelo que referencia**. Si un campo
perdido se ve en la operación y en el modelo, se publica en la operación (el locus del cliente);
el eje de modelos sólo habla cuando ninguna operación incrusta ese modelo. Sobre un modelo el
contrato no dice si se usa como petición o como respuesta, así que ahí se **asume petición**, que
es el uso que rompe: es una suposición declarada, no una lectura.

| clase | severidad | qué significa |
|---|---|---|
| `OPERACION_ELIMINADA` | ROJO | la versión anterior publicaba la ruta y esta no |
| `OPERACION_ANADIDA` | WARN | operación nueva; no rompe a ningún cliente existente |
| `OPERACION_CONDICIONAL_ELIMINADA` | WARN | la anterior la marcaba `x-spec-drift-radar-condicional`: no consta que se publicara |
| `CAMPO_ELIMINADO` | ROJO | el campo existía y ya no se publica |
| `TIPO_CAMBIADO` | ROJO | mismo campo, distinto tipo |
| `REQUERIDO_ENDURECIDO` | ROJO | era opcional y ahora es obligatorio |
| `REQUERIDO_RELAJADO` | WARN | era obligatorio y ahora es opcional |
| `CAMPO_REQUERIDO_NUEVO` | ROJO | no existía y ahora es obligatorio |
| `CAMPO_OPCIONAL_NUEVO` | WARN | campo nuevo y opcional |
| `CODIGO_ELIMINADO` | ROJO | el éxito `2xx` ya no se publica |
| `CODIGO_ANADIDO` | WARN | éxito `2xx` nuevo |
| `ESQUEMA_ELIMINADO` / `ESQUEMA_ANADIDO` | WARN | modelo nombrado que aparece o desaparece del contrato |

**Dialectos.** Se leen **OpenAPI 3.x** y **Swagger 2.0**, que no comparten ni el sitio de los
esquemas (`components.schemas` / `definitions`) ni el del cuerpo (`requestBody` /
`parameters[in=body]`). Leer un 2.0 con el lector de 3.x no fallaba: devolvía 865 operaciones con
cero campos y un `RC 0` sobre un contrato que nunca se miró. Un documento que no se declara en
ninguno de los dos dialectos publica `sin_medir` y **RC 2**: las rutas se comparan, los cuerpos no.

Un `emitir` que se compara con el contrato ya publicado mide la frontera entre dos lectores, no una
deriva: el contrato emitido marca `x-spec-drift-radar-condicional: true` en las rutas que el AST
vio dentro de un `if`, y el radar lo lee de vuelta para no contar su retirada como ruptura.

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

- Contrato: **OpenAPI 3.x** y **Swagger 2.0**, en JSON. El dialecto declarado por el documento se
  publica en el marco (`dialecto=`); uno que no se declare en ninguno de los dos no se lee a
  medias: las rutas se comparan, los cuerpos no, y el RC es 2.
- Servidor: AST de Python. Routers estilo FastAPI (`@router.get/post/...` con ruta literal),
  modelos Pydantic por herencia declarada (`BaseModel`) **y** `@dataclass` de pydantic,
  `Optional[...]`, `X | None`, `Annotated[T, Body(embed=True)]`, valores por defecto
  (`Field(...)` requerido, `Field(None)` opcional, `Field(min_length=1)` sin default, `field(...)`),
  `alias=` literal, y anotaciones `list[...]` / `dict[...]` / `Literal[...]`.
- Varios parámetros de cuerpo a la vez: FastAPI los **embebe** cada uno bajo su nombre o su
  `alias`; el radar lee ese nombre, no una lista fundida de campos que el contrato nunca publica.
- Formularios y ficheros (`Form()`, `File()`, `UploadFile`, `list[UploadFile]`): un escalar de
  formulario **sí** publica una propiedad con su nombre; un modelo de formulario se publica
  mediante `allOf`, y el radar fusiona `allOf` con las `properties` del mismo nivel.
- `app.mount(...)` no publica las rutas del hijo en el esquema del padre (se excluyen);
  `callbacks=router.routes` publica esas rutas bajo el árbol del llamante, no en `paths`;
  `@app.webhooks.post(...)` vive en la sección `webhooks` y se compara en su propio espacio de
  nombres; `include_in_schema=False` no se publica.
- Los parámetros que son **dependencias** (`= Depends(...)`, `= Query(...)`, `= Header(...)`) no
  se leen como cuerpo: sin esa distinción, cada `Depends` con modelo anotado se publicaba como
  cuatro o cinco campos fantasma por operación.
- Contrato: OpenAPI 3.x en JSON. `paths`, `requestBody`, `responses` 2xx, `components.schemas`
  y `$ref` (con resolución acotada a 8 saltos para no colgarse en una referencia circular).

**No lee (y lo declara en cada salida, campo `sin_lector`)**

- TypeScript y YAML: no hay parser en la caja de herramientas estándar, y un regex no es un
  parser. Se publica como `sin_lector`, nunca como «cero hallazgos».
- Rutas construidas en tiempo de ejecución (`f"{PREFIJO}/x"`): se anotan en `sin_medir` y
  fuerzan RC 2.
- `alias_generator` en `Config`: renombra campos con un valor que no está en el AST (una función).
  Los nombres afectados se publican como `ALIAS_NO_MEDIDO`, no como deriva. Un `alias="…"` literal
  sí se compara.
- Condiciones de arranque (`if settings.ENV == "development": app.include_router(...)`): el AST
  no evalúa la condición, así que la ruta se publica con `CONDICIONAL_NO_PUBLICADA` y el número
  de inclusiones condicionales viaja en `inclusiones_condicionales`. No es deriva: es un estado
  del servidor que este instrumento no puede decidir.
- El cuerpo que viaja en un formulario de seguridad (`OAuth2PasswordRequestForm` y su variante
  `Strict`): sus seis campos (`grant_type`, `username`, `password`, `scope`, `client_id`,
  `client_secret`) los genera `fastapi.security`, no el AST. Se publica
  `CUERPO_NO_MEDIDO_POR_DEPENDENCIA` y ese cuerpo **no se compara** — declarado, no adivinado.
- Frameworks cuyo enrutado no vive en un decorador con ruta literal (Flask, Django, Starlette puro): el marco declara `lector=fastapi`, y si el contrato publica operaciones que ese lector no encuentra, sale **un** aviso `SERVIDOR_NO_LEIDO` — no una `FALTA_EN_CODIGO` por ruta, que sería atribuir al servidor una ceguera del instrumento — y el veredicto es **RC 2**, nunca RC 0.

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
| `ALIAS_NO_MEDIDO` | WARN | el modelo declara `alias_generator`: el nombre publicado no se calcula desde el AST; no fuerza RC 1 |
| `CUERPO_NO_MEDIDO_POR_DEPENDENCIA` | WARN | el cuerpo viaja en un formulario de `fastapi.security`; sus campos no se calculan desde el AST y no se comparan |
| `SERVIDOR_NO_LEIDO` | WARN | el lector de rutas (`lector=fastapi`) no encontró ninguna y el contrato publica varias: no hay nada que comparar ruta a ruta |

## Estado medido

- `v0.6.0` · 80 tests · `--self-test` RC 0 · CI en 3.11 / 3.12 / 3.13.
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
- **Segundo oráculo, otra familia de proyecto**
  ([`nsidnev/fastapi-realworld-example-app`](https://github.com/nsidnev/fastapi-realworld-example-app)
  @ `029eb77`, 16-Nov-2026): 72 archivos, 32 modelos, **12 rutas / 19 operaciones** idénticas al
  `app.openapi()` real, montaje resuelto a **cuatro niveles** (`main → api → articles.api → *`),
  `prefijos_no_medidos=0`. La primera corrida dio **156 rojos**; hoy son **0 rojos y 4 avisos**
  (`ALIAS_NO_MEDIDO`) ⇒ **RC 0**. Ese proyecto no usa nada del template oficial, y por eso encontró
  cinco defectos que el primer oráculo no podía ver: `Depends(...)` leído como cuerpo,
  `Body(..., embed=True, alias="article")` sin embeker, `Field(...)` (pydantic v1) leído como
  opcional, `alias="tagList"` ignorado, `alias_generator` convertido en deriva y
  `status_code=HTTP_201_CREATED` (nombre suelto) leído como 200.
- **Tercer oráculo, el corpus del propio framework**
  ([`fastapi/fastapi`](https://github.com/fastapi/fastapi) @ `05e6df7`, 17-Nov-2026): los
  **324 ejemplos ejecutables** de `docs_src`. Cada app se importa y su `app.openapi()` real es la
  verdad, así que aquí no cabe la excusa de «otra forma de leer»: si el radar discrepa, el radar
  está mal. Primera corrida: **40 apps con rojos**. Noventa y dos defectos de lector después (doce
  causas distintas, todas con test): **0 apps con rojos**, **321 RC 0**, **3 RC 2** declarados
  (`NO_MEDIDO`: apps sin una sola ruta publicada — sólo `StaticFiles`, sólo middleware, o la única
  ruta con `include_in_schema=False`), **38 apps no construidas** por dependencias ausentes
  (`httpx2`, `uvicorn`, `orjson`, directorio `static`), contadas y no escondidas.
  Lo que este corpus encontró y los dos anteriores no podían ver: metadatos de `Annotated[...]`
  ignorados (se perdían `Body(embed=True)` y `Depends`), `list[Model]` y `Model | None` sin
  desenvolver, más de un cuerpo **sin embeber** bajo su nombre (dos modelos fundidos en una lista
  que el contrato no publica), campo embebido tipado `object` en vez de su escalar real,
  `required` mal calculado, envoltorio `anyOf` de pydantic v2 escondiendo el modelo a dos saltos,
  convertidor `{p:path}`, `include_in_schema=False` publicado, `app.mount()` leído como router,
  formularios y `multipart` fuera del lector de cuerpo, `@dataclass` desconocido como modelo,
  `Literal["x"]` tipado como su texto, `allOf` sin fusionar con las `properties` del mismo nivel,
  sección `webhooks`, routers de `callbacks=` y el formulario de `fastapi.security`.
- **Una hipótesis propia que la medición mató** (misma corrida, 17-Nov-2026): durante un rato este
  radar publicó que FastAPI «no publica los campos de un modelo de formulario». Falso: los publica
  mediante `allOf` junto a los escalares del mismo nivel, y el que leía mal era este lector. Diez
  apps daban rojo por ese defecto; corregido el lector, dan verde. Se anota aquí porque un
  instrumento que presume un defecto del sujeto cuando el defecto es suyo no sirve de nada.
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

$ git clone --depth 1 https://github.com/nsidnev/fastapi-realworld-example-app.git /tmp/rw
$ git -C /tmp/rw rev-parse HEAD   # 029eb7781c60d5f563ee8990a0cbfb79b244538c
$ python3.12 -m venv /tmp/venv_rw && /tmp/venv_rw/bin/pip install \
    "fastapi==0.79.1" "pydantic[email]==1.10.18" "databases==0.6.2" "aiosql==6.4" "setuptools<70" \
    "pyjwt==2.4.0" passlib bcrypt asyncpg pypika loguru "python-slugify==6.1.2" Unidecode ujson
$ (cd /tmp/rw && DATABASE_URL=postgresql://u:p@localhost/db SECRET_KEY=x /tmp/venv_rw/bin/python -c \
    'import json;from app.main import app;print(json.dumps(app.openapi()))' > /tmp/rw_verdadero.json)
$ spec-drift-radar --servidor /tmp/rw/app --openapi /tmp/rw_verdadero.json
# RC 0 · archivos_py=72 · modelos=32 · rutas_codigo=19 · rutas_spec=19 · rojos=0 · avisos=4
```

Reproducir el tercer oráculo (324 apps, una verdad ejecutada por app):

```console
$ git clone --depth 1 https://github.com/fastapi/fastapi.git /tmp/fa
$ git -C /tmp/fa rev-parse HEAD   # 05e6df7e906c3decba377c37548019973f40bf15
$ python3.12 -m venv /tmp/venv_fa2 && /tmp/venv_fa2/bin/pip install \
    fastapi pydantic python-multipart sqlmodel jinja2 itsdangerous pydantic-settings ujson
$ # por cada docs_src/<dir>/*.py que contenga "= FastAPI(":
$ #   copiarlo a /tmp/fa_apps/<dir>__<stem>/ y volcar app.openapi() a openapi.json
$ #   (38 apps no se pueden importar: httpx2, uvicorn, orjson, static/ ausente)
$ for d in /tmp/fa_apps/*/; do spec-drift-radar --servidor "$d" --openapi "$d/openapi.json" --json; done
# 324 medidas · RC 0 = 321 · RC 1 = 0 · RC 2 = 3 (sin rutas publicadas)
```

- **Cuarto oráculo, un framework que este lector no reconoce**
  ([`flask-smorest`](https://github.com/marshmallow-code/flask-smorest), 25-Nov-2026): la app de
  ejemplo del propio proyecto, con su contrato generado por el framework (3 operaciones, cuerpo y
  `query` reales). Un instrumento deshonesto contesta aquí una de dos cosas: «0 hallazgos, sin
  deriva» (falso verde en la dirección peligrosa) o tres `FALTA_EN_CODIGO` (le atribuye al sujeto
  lo que es ceguera del lector). Este contesta **un aviso `SERVIDOR_NO_LEIDO`, `rojos=0` y RC 2**,
  y el marco publica `lector=fastapi` para que se vea de un golpe por qué no hay nada que
  comparar. Es el resultado que se quería: el camino `NO_MEDIDO` no se degrada a 0 cuando el
  servidor existe y el lector no lo entiende.

```console
$ python3.12 -m venv /tmp/venv_flask && /tmp/venv_flask/bin/pip install flask flask-smorest
$ # app de ejemplo de flask-smorest (blueprint /items con GET, POST y GET /<int:item_id>)
$ /tmp/venv_flask/bin/python -c "from app import app; import json; \
    json.dump(app.test_client().get('/openapi.json').get_json(), open('/tmp/flask_verdadero.json','w'))"
$ spec-drift-radar --servidor /tmp/app_flask --openapi /tmp/flask_verdadero.json
# MARCO DEL SERVIDOR ... rutas_codigo=0 · lector=fastapi
# HALLAZGOS  rojos=0 · avisos=1   [WARN] SERVIDOR_NO_LEIDO
# RC 2 · NO_MEDIDO: un cero aquí no es un cero, es una ausencia de lectura
```

- **Quinto oráculo, el contrato más grande que se publica en JSON: Kubernetes.** Dos releases
  reales, `v1.28.0` → `v1.29.0` (`api/openapi-spec/swagger.json`, swagger 2.0, 3,0 MB cada una):
  865 y 878 operaciones, 570 y 577 modelos, **544 modelos comparados**, RC 1 con 35 rupturas.
  La medición se puede comprobar contra las notas de la release: 25 operaciones desaparecen de
  `flowcontrol.apiserver.k8s.io/v1beta2` y 25 aparecen en `v1beta3`; `clustercidrs` (9 rutas) sale
  y `servicecidrs` (12) entra; el campo `uid` desaparece de `networking.v1alpha1.ParentReference`;
  y los diez campos nuevos son los que la 1.29 estrenó (`PodAffinityTerm.matchLabelKeys` y
  `mismatchLabelKeys`, `LifecycleHandler.sleep`, `VolumeProjection.clusterTrustBundle`,
  `PersistentVolumeClaimSpec.volumeAttributesClassName` con su `modifyVolumeStatus`,
  `LoadBalancerIngress.ipMode`, `ValidationRule.optionalOldSelf`). Antes de este modo el mismo par
  daba **865 operaciones con 0 campos leídos y ningún aviso**: el lector de OpenAPI 3.x no fallaba
  con un documento 2.0, devolvía un cero. Ese cero era el defecto; ahora hay dialecto declarado.

```console
$ for t in v1.28.0 v1.29.0; do curl -sSL -o "k8s_$t.json" \
    "https://raw.githubusercontent.com/kubernetes/kubernetes/$t/api/openapi-spec/swagger.json"; done
$ spec-drift-radar --contrato-anterior k8s_v1.28.0.json --openapi k8s_v1.29.0.json
# MARCO ANTERIOR   dialecto=swagger 2.0 · titulo=Kubernetes · rutas_spec=865 · esquemas=570
# MARCO NUEVO      dialecto=swagger 2.0 · titulo=Kubernetes · rutas_spec=878 · esquemas=577
# HALLAZGOS  rojos=35 · avisos=117 · operaciones_comparadas=831 · esquemas_comparados=544
# RC 1 · ruptura medida: un cliente de la version anterior deja de funcionar
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
