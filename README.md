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
MARCO DEL CONTRATO   openapi=3.1.0 · titulo=Pedidos · version=2026-11 · rutas_spec=3

HALLAZGOS  rojos=6 · avisos=1 · pares_comparados=2
  [ROJO] FALTA_EN_CODIGO        POST    /pedidos/{pedido_id}/cancelar    el contrato la publica y el servidor no la declara
  [ROJO] CAMPO_FALTA_EN_SPEC    POST    /pedidos                         el servidor declara 'cliente'; el contrato no
  [ROJO] REQUERIDO_DIVERGENTE   POST    /pedidos                         'prioridad': servidor=opcional · contrato=requerido
  [ROJO] CAMPO_FALTA_EN_CODIGO  POST    /pedidos                         el contrato declara 'customer'; el servidor no
  [ROJO] TIPO_DIVERGENTE        POST    /pedidos                         'id': servidor=integer · contrato=string
  [WARN] CODIGO_DIVERGENTE      POST    /pedidos                         servidor=[201] · spec=[200]
  [ROJO] TIPO_DIVERGENTE        GET     /pedidos/{pedido_id}             'id': servidor=integer · contrato=string

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

# 4. verificar el instrumento consigo mismo (emite, compara, muta y detecta)
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
- Frameworks cuyo enrutado no vive en un decorador con ruta literal.

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

## Estado medido

- `v0.1.0` · 20 tests · `--self-test` RC 0 · CI en 3.11 / 3.12 / 3.13.
- Verificado sobre fixtures sintéticos que reproducen las siete clases de deriva de arriba;
  **todavía no se ha medido sobre un proyecto externo real**. Cuando se mida, la cifra y el
  repositorio van aquí; hasta entonces, este párrafo es la declaración honesta del hueco.

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
