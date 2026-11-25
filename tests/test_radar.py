import json
import pathlib

import pytest

from spec_drift_radar import __version__
from spec_drift_radar.cli import main
from spec_drift_radar.nucleo import (
    comparar,
    emitir_openapi,
    leer_json,
    rc_de,
    superficie_openapi,
    superficie_servidor,
)

CODIGO = '''
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


@pytest.fixture
def proyecto(tmp_path):
    """Un servidor de dos rutas y el contrato emitido desde el propio servidor."""
    (tmp_path / "app.py").write_text(CODIGO, encoding="utf-8")
    servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
    documento = emitir_openapi(servidor)
    return tmp_path, servidor, documento


def _medir(directorio, documento):
    servidor = superficie_servidor(sorted(pathlib.Path(directorio).rglob("*.py")))
    spec = superficie_openapi(documento)
    hallazgos = comparar(servidor, spec)
    return servidor, spec, hallazgos, rc_de(hallazgos, servidor, spec)


class TestSuperficieDelServidor:
    def test_lee_modelos_rutas_y_codigos(self, proyecto):
        _, servidor, _ = proyecto
        assert servidor.marco["rutas_codigo"] == 2
        assert servidor.marco["modelos"] == 2
        post = [o for o in servidor.operaciones if o.metodo == "post"][0]
        assert post.codigos == (201,)
        assert {c.nombre for c in post.peticion} == {"nombre", "precio", "nota"}

    def test_optional_y_default_son_opcionales(self, proyecto):
        _, servidor, _ = proyecto
        post = [o for o in servidor.operaciones if o.metodo == "post"][0]
        requeridos = {c.nombre for c in post.peticion if c.requerido}
        assert requeridos == {"nombre", "precio"}
        assert {c.tipo for c in post.peticion} == {"string", "number"}

    def test_archivo_que_no_parsea_no_se_mide(self, tmp_path):
        (tmp_path / "roto.py").write_text("def f(:\n", encoding="utf-8")
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert servidor.marco["archivos_py"] == 0
        assert any("no parsea" in nota for nota in servidor.sin_medir)

    def test_ruta_no_literal_queda_sin_medir(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nrouter = APIRouter()\n'
            'PREFIJO = "/v1"\n\n@router.get(f"{PREFIJO}/x")\ndef x():\n    return {}\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert servidor.marco["rutas_codigo"] == 0
        assert any("no literal" in nota for nota in servidor.sin_medir)


class TestContratoEmitido:
    def test_el_contrato_emitido_no_deriva_del_servidor(self, proyecto):
        directorio, _, documento = proyecto
        _, spec, hallazgos, rc = _medir(directorio, documento)
        assert hallazgos == []
        assert rc == 0
        assert spec.marco["rutas_spec"] == 2


class TestDeriva:
    def test_ruta_del_servidor_ausente_del_contrato(self, proyecto):
        directorio, _, documento = proyecto
        del documento["paths"]["/items/{item_id}"]
        _, _, hallazgos, rc = _medir(directorio, documento)
        assert rc == 1
        assert [h.clase for h in hallazgos] == ["FALTA_EN_SPEC"]
        assert hallazgos[0].ruta == "/items/{item_id}"

    def test_ruta_del_contrato_ausente_del_servidor(self, proyecto):
        directorio, _, documento = proyecto
        documento["paths"]["/fantasma"] = {"get": {"responses": {"200": {"description": ""}}}}
        _, _, hallazgos, rc = _medir(directorio, documento)
        assert rc == 1
        assert [h.clase for h in hallazgos] == ["FALTA_EN_CODIGO"]

    def test_campo_renombrado_se_ve_en_las_dos_direcciones(self, proyecto):
        directorio, _, documento = proyecto
        esquema = documento["components"]["schemas"]["post_items_entrada"]
        esquema["properties"]["name"] = esquema["properties"].pop("nombre")
        esquema["required"] = ["name", "precio"]
        _, _, hallazgos, rc = _medir(directorio, documento)
        assert rc == 1
        clases = {h.clase for h in hallazgos}
        assert clases == {"CAMPO_FALTA_EN_SPEC", "CAMPO_FALTA_EN_CODIGO"}
        assert any("'nombre'" in h.detalle for h in hallazgos)
        assert any("'name'" in h.detalle for h in hallazgos)

    def test_tipo_divergente(self, proyecto):
        directorio, _, documento = proyecto
        documento["components"]["schemas"]["get_items_item_id_salida"]["properties"]["id"] = {"type": "string"}
        _, _, hallazgos, rc = _medir(directorio, documento)
        assert rc == 1
        assert [h.clase for h in hallazgos] == ["TIPO_DIVERGENTE"]
        assert "integer" in hallazgos[0].detalle and "string" in hallazgos[0].detalle

    def test_requerido_divergente(self, proyecto):
        directorio, _, documento = proyecto
        esquema = documento["components"]["schemas"]["post_items_entrada"]
        esquema["required"] = ["nombre", "precio", "nota"]
        _, _, hallazgos, rc = _medir(directorio, documento)
        assert rc == 1
        assert [h.clase for h in hallazgos] == ["REQUERIDO_DIVERGENTE"]
        assert "nota" in hallazgos[0].detalle

    def test_codigo_divergente_es_aviso_y_no_rc_1(self, proyecto):
        directorio, _, documento = proyecto
        post = documento["paths"]["/items"]["post"]
        post["responses"]["200"] = post["responses"].pop("201")
        _, _, hallazgos, rc = _medir(directorio, documento)
        assert [h.clase for h in hallazgos] == ["CODIGO_DIVERGENTE"]
        assert hallazgos[0].severidad == "WARN"
        assert rc == 0


class TestPrefijosYModelos:
    def test_prefijo_de_router_se_compone_en_la_ruta(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom pydantic import BaseModel\n\n'
            'router = APIRouter(prefix="/usuarios", tags=["usuarios"])\n\n'
            'class Salida(BaseModel):\n    id: int\n\n'
            '@router.get("/{id}", response_model=Salida)\ndef leer(id: int):\n    return {"id": id}\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert [op.ruta for op in servidor.operaciones] == ["/usuarios/{id}"]

    def test_prefijo_base_declarado_se_antepone(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import FastAPI\napp = FastAPI()\n\n'
            '@app.get("/salud")\ndef salud():\n    return {}\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")), prefijo_base="/api/v1")
        assert [op.ruta for op in servidor.operaciones] == ["/api/v1/salud"]
        assert servidor.marco["prefijo_base"] == "/api/v1"

    def test_include_router_con_prefijo_no_resoluble_es_no_medido(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import FastAPI\napp = FastAPI()\n'
            'app.include_router(otro, prefix=AJUSTES.API)\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert servidor.marco["prefijos_no_medidos"] == 1
        assert any("prefijo no resoluble" in nota for nota in servidor.sin_medir)

    def test_prefijo_base_silencia_el_hueco_declarado(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import FastAPI\napp = FastAPI()\n'
            'app.include_router(otro, prefix=AJUSTES.API)\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")), prefijo_base="/api/v1")
        assert servidor.sin_medir == ()
        assert servidor.marco["prefijos_no_medidos"] == 1

    def test_modelo_sqlmodel_por_herencia_indirecta(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom sqlmodel import SQLModel, Field\n\nrouter = APIRouter()\n\n'
            'class UsuarioBase(SQLModel):\n    email: str\n    nombre: str | None = None\n\n'
            'class Usuario(UsuarioBase, table=True):\n    id: int | None = Field(default=None, primary_key=True)\n\n'
            '@router.post("/usuarios")\ndef crear(usuario: Usuario):\n    return {}\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert servidor.marco["modelos"] == 2
        op = servidor.operaciones[0]
        assert {c.nombre for c in op.peticion} == {"id", "email", "nombre"}
        assert {c.nombre for c in op.peticion if c.requerido} == {"email"}

    def test_marco_publica_rutas_sin_campos(self, proyecto):
        _, servidor, _ = proyecto
        assert servidor.marco["rutas_con_campos"] == 2
        assert servidor.marco["rutas_sin_campos"] == 0


class TestContraElGeneradorDeFastAPI:
    """El oraculo no es mi emisor: es `app.openapi()` del propio FastAPI.

    Los cuatro casos de abajo son los que fallaron en la primera corrida contra el
    generador real; quedan como contrato para que no vuelvan.
    """

    def test_status_por_atributo_se_resuelve(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter, status\n\nrouter = APIRouter(prefix="/x")\n\n'
            '@router.post("", status_code=status.HTTP_201_CREATED)\ndef crear():\n    return {}\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert servidor.operaciones[0].codigos == (201,)

    def test_anyof_con_null_es_el_tipo_no_nulo(self, proyecto):
        directorio, _, documento = proyecto
        documento["components"]["schemas"]["get_items_item_id_salida"]["properties"]["id"] = {
            "anyOf": [{"type": "integer"}, {"type": "null"}]
        }
        _, _, hallazgos, rc = _medir(directorio, documento)
        assert hallazgos == []
        assert rc == 0

    def test_codigos_no_2xx_del_generador_no_son_deriva(self, proyecto):
        directorio, _, documento = proyecto
        documento["paths"]["/items"]["post"]["responses"]["422"] = {"description": "validation"}
        _, _, hallazgos, rc = _medir(directorio, documento)
        assert hallazgos == []
        assert rc == 0

    def test_el_hallazgo_dice_de_que_lado_esta(self, proyecto):
        directorio, _, documento = proyecto
        documento["components"]["schemas"]["post_items_entrada"]["properties"]["nombre"] = {"type": "integer"}
        _, _, hallazgos, _ = _medir(directorio, documento)
        assert [h.clase for h in hallazgos] == ["TIPO_DIVERGENTE"]
        assert hallazgos[0].detalle.startswith("cuerpo 'nombre'")


class TestNoMedido:
    def test_sin_archivos_python_es_rc_2(self, tmp_path):
        _, _, _, rc = _medir(tmp_path, emitir_openapi(superficie_servidor([])))
        assert rc == 2

    def test_contrato_sin_paths_es_rc_2(self, proyecto):
        """Cero rutas del lado del contrato no es deriva total: es ausencia de lectura."""
        directorio, _, _ = proyecto
        servidor, spec, hallazgos, rc = _medir(directorio, {"openapi": "3.1.0", "info": {}, "paths": {}})
        assert len(hallazgos) == 2
        assert rc == 2
        assert spec.sin_medir

    def test_json_invalido_es_rc_2(self, tmp_path):
        destino = tmp_path / "openapi.json"
        destino.write_text("{no es json", encoding="utf-8")
        documento, error = leer_json(destino)
        assert documento is None
        assert error is not None
        assert main(["--servidor", str(tmp_path), "--openapi", str(destino)]) == 2


class TestCli:
    def test_json_publica_marco_y_rc(self, proyecto, tmp_path, capsys):
        directorio, _, documento = proyecto
        destino = tmp_path / "openapi.json"
        destino.write_text(json.dumps(documento), encoding="utf-8")
        rc = main(["--servidor", str(directorio), "--openapi", str(destino), "--json"])
        salida = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert salida["rc"] == 0
        assert salida["marco"]["servidor"]["rutas_codigo"] == 2
        assert salida["marco"]["contrato"]["rutas_spec"] == 2
        assert salida["marco"]["servidor"]["sin_lector"] == ["typescript", "yaml"]

    def test_texto_nombra_los_hallazgos(self, proyecto, tmp_path, capsys):
        directorio, _, documento = proyecto
        del documento["paths"]["/items"]
        destino = tmp_path / "openapi.json"
        destino.write_text(json.dumps(documento), encoding="utf-8")
        rc = main(["--servidor", str(directorio), "--openapi", str(destino)])
        salida = capsys.readouterr().out
        assert rc == 1
        assert "FALTA_EN_SPEC" in salida
        assert "RC 1" in salida

    def test_emitir_escribe_el_contrato(self, proyecto, tmp_path, capsys):
        directorio, _, _ = proyecto
        destino = tmp_path / "emitido.json"
        rc = main(["--servidor", str(directorio), "--emitir", str(destino)])
        assert rc == 0
        emitido = json.loads(destino.read_text(encoding="utf-8"))
        assert set(emitido["paths"]) == {"/items", "/items/{item_id}"}
        assert "contrato emitido" in capsys.readouterr().out

    def test_version_repite_el_paquete(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == f"spec-drift-radar {__version__}"

    def test_self_test_rc_0(self, capsys):
        assert main(["--self-test"]) == 0
        assert "self-test RC 0" in capsys.readouterr().out

    def test_sin_argumentos_es_error_de_uso(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 2


class TestContraUnProyectoReal:
    """La segunda corrida contra un proyecto externo (`full-stack-fastapi-template`).

    Cada caso de abajo es un defecto que el oraculo real encontro y que aqui queda
    como contrato: sin el, la herramienta publicaba 48 hallazgos falsos sobre un
    proyecto sano.
    """

    def test_la_barra_final_no_se_pierde(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/items")\n\n'
            '@router.get("/")\ndef listar():\n    return []\n\n'
            '@router.get("/{id}")\ndef uno(id: int):\n    return {}\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert sorted(op.ruta for op in servidor.operaciones) == ["/items/", "/items/{id}"]

    def test_el_cuerpo_puede_venir_en_un_parametro_keyword_only(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom pydantic import BaseModel\n\nrouter = APIRouter()\n\n'
            'class Usuario(BaseModel):\n    email: str\n\n'
            '@router.patch("/usuarios/{id}")\ndef editar(*, id: int, usuario: Usuario):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [c.nombre for c in op.peticion] == ["email"]

    def test_field_sin_default_es_requerido(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom sqlmodel import SQLModel, Field\n\nrouter = APIRouter()\n\n'
            'class Item(SQLModel):\n'
            '    titulo: str = Field(min_length=1, max_length=255)\n'
            '    nota: str | None = Field(default=None, max_length=255)\n'
            '    orden: int = Field(default_factory=int)\n\n'
            '@router.post("/items")\ndef crear(item: Item):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        requeridos = {c.nombre for c in op.peticion if c.requerido}
        assert requeridos == {"titulo"}

    def test_la_respuesta_la_declara_la_anotacion_de_retorno(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom pydantic import BaseModel\n\nrouter = APIRouter()\n\n'
            'class Token(BaseModel):\n    acceso: str\n    tipo: str\n\n'
            '@router.post("/login")\ndef login() -> Token:\n    return Token(acceso="a", tipo="bearer")\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [c.nombre for c in op.respuesta] == ["acceso", "tipo"]

    def test_emailstr_es_una_cadena(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom pydantic import BaseModel, EmailStr\n\nrouter = APIRouter()\n\n'
            'class Usuario(BaseModel):\n    email: EmailStr\n\n'
            '@router.post("/usuarios")\ndef crear(usuario: Usuario):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [(c.nombre, c.tipo) for c in op.peticion] == [("email", "string")]

    def test_el_prefijo_se_resuelve_cruzando_modulos(self, tmp_path):
        (tmp_path / "config.py").write_text(
            'from pydantic_settings import BaseSettings\n\nclass Settings(BaseSettings):\n'
            '    API: str = "/api/v1"\n\nsettings = Settings()\n',
            encoding="utf-8",
        )
        (tmp_path / "rutas.py").write_text(
            'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/usuarios")\n\n'
            '@router.get("/")\ndef listar():\n    return []\n',
            encoding="utf-8",
        )
        (tmp_path / "raiz.py").write_text(
            'from fastapi import FastAPI\n\n'
            'from config import settings\nfrom rutas import router\n\n'
            'app = FastAPI()\napp.include_router(router, prefix=settings.API)\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert [op.ruta for op in servidor.operaciones] == ["/api/v1/usuarios/"]
        assert servidor.marco["prefijos_no_medidos"] == 0

    def test_un_include_router_condicional_no_es_un_rojo(self, tmp_path):
        (tmp_path / "condicional.py").write_text(
            'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/privado")\n\n'
            '@router.post("/usuarios")\ndef crear():\n    return {}\n',
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\n\nfrom condicional import router as privado\n\n'
            'API = "development"\nrouter = APIRouter()\n\n'
            '@router.get("/salud")\ndef salud():\n    return {}\n\n'
            'if API == "development":\n    router.include_router(privado)\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert servidor.marco["inclusiones_condicionales"] == 1
        documento = emitir_openapi(servidor)
        documento["paths"].pop("/privado/usuarios")
        _servidor, _spec, hallazgos, rc = _medir(tmp_path, documento)
        assert [h.clase for h in hallazgos] == ["CONDICIONAL_NO_PUBLICADA"]
        assert hallazgos[0].severidad == "WARN"
        assert rc == 0


class TestContraUnSegundoProyecto:
    """La corrida contra `nsidnev/fastapi-realworld-example-app` (pydantic v1, `Depends` por todas partes).

    Otro proyecto, otra familia, otros defectos: los cinco de abajo no los encontro el
    primer oraculo porque el template oficial no usa inyeccion de dependencias ni
    `alias_generator`.
    """

    def test_una_dependencia_no_es_un_cuerpo(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter, Depends\nfrom pydantic import BaseModel\n\n'
            'router = APIRouter()\n\n'
            'class Usuario(BaseModel):\n    email: str\n    bio: str\n\n'
            'def get_usuario() -> Usuario:\n    return Usuario(email="a@b.c", bio="")\n\n'
            '@router.get("/perfil")\ndef perfil(usuario: Usuario = Depends(get_usuario)):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert op.peticion == ()

    def test_body_embebido_publica_el_alias_de_la_llamada(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter, Body\nfrom pydantic import BaseModel\n\n'
            'router = APIRouter()\n\n'
            'class ArticuloEntra(BaseModel):\n    titulo: str\n\n'
            '@router.post("/articulos")\n'
            'def crear(articulo: ArticuloEntra = Body(..., embed=True, alias="article")):\n'
            '    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [(c.nombre, c.tipo, c.requerido) for c in op.peticion] == [("article", "object", True)]

    def test_field_con_puntos_suspensivos_es_requerido(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom pydantic import BaseModel, Field\n\n'
            'router = APIRouter()\n\n'
            'class Usuario(BaseModel):\n'
            '    email: str = Field(...)\n'
            '    bio: str = Field("")\n\n'
            '@router.post("/usuarios")\ndef crear(usuario: Usuario):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert {c.nombre for c in op.peticion if c.requerido} == {"email"}

    def test_un_alias_literal_es_el_nombre_documentado(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom pydantic import BaseModel, Field\n\n'
            'router = APIRouter()\n\n'
            'class Articulo(BaseModel):\n    tags: list[str] = Field([], alias="tagList")\n\n'
            '@router.get("/articulos", response_model=Articulo)\ndef listar():\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [c.nombre for c in op.respuesta] == ["tagList"]

    def test_un_generador_de_alias_se_declara_no_se_adivina(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom pydantic import BaseModel\n\n'
            'def a_camel(texto: str) -> str:\n    return texto\n\n'
            'class Modelo(BaseModel):\n'
            '    class Config:\n        alias_generator = a_camel\n\n'
            'class Salida(Modelo):\n    total_del_dia: int\n\n'
            'router = APIRouter()\n\n'
            '@router.get("/resumen", response_model=Salida)\ndef resumen():\n    return {}\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        documento = emitir_openapi(servidor)
        documento["paths"]["/resumen"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"] = {"$ref": "#/components/schemas/Salida"}
        documento["components"]["schemas"]["Salida"] = {
            "type": "object", "properties": {"totalDelDia": {"type": "integer"}}, "required": ["totalDelDia"]
        }
        _servidor, _spec, hallazgos, rc = _medir(tmp_path, documento)
        assert {h.clase for h in hallazgos} == {"ALIAS_NO_MEDIDO"}
        assert all(h.severidad == "WARN" for h in hallazgos)
        assert rc == 0

    def test_el_codigo_de_estado_puede_ser_un_nombre_importado(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom starlette.status import HTTP_201_CREATED\n\n'
            'router = APIRouter()\n\n'
            '@router.post("/usuarios", status_code=HTTP_201_CREATED)\ndef crear():\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert op.codigos == (201,)


class TestContraElCorpusDeFastAPI:
    """La corrida contra los 324 ejemplos de `fastapi/fastapi@05e6df7` (`docs_src`).

    Es el oraculo mas duro: cada app se **ejecuta** y su `app.openapi()` es la verdad, asi que
    ningun defecto de nuestro lector puede justificarse como "lectura distinta". Los ocho de
    abajo son los que este corpus encontro y que los dos oraculos anteriores no tocaban.
    """

    def test_anotated_con_metadatos_no_borra_el_parametro(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from typing import Annotated\nfrom fastapi import APIRouter, Body\n'
            'from pydantic import BaseModel\n\nrouter = APIRouter()\n\n'
            'class Item(BaseModel):\n    name: str\n\n'
            '@router.put("/items/{id}")\ndef actualizar(id: int, item: Annotated[Item, Body(embed=True)]):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [(c.nombre, c.tipo) for c in op.peticion] == [("item", "object")]

    def test_dos_cuerpos_se_embeben_cada_uno_bajo_su_nombre(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter, Body\nfrom pydantic import BaseModel\n\n'
            'router = APIRouter()\n\n'
            'class Item(BaseModel):\n    name: str\n\n'
            'class User(BaseModel):\n    username: str\n\n'
            '@router.post("/x")\ndef crear(item: Item, user: User, importance: int = Body()):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [(c.nombre, c.tipo, c.requerido) for c in op.peticion] == [
            ("item", "object", True), ("user", "object", True), ("importance", "integer", True),
        ]

    def test_lista_de_modelos_en_la_respuesta(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\nfrom pydantic import BaseModel\n\nrouter = APIRouter()\n\n'
            'class Item(BaseModel):\n    name: str\n\n'
            '@router.get("/items/", response_model=list[Item])\ndef leer():\n    return []\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [(c.nombre, c.tipo) for c in op.respuesta] == [("name", "string")]

    def test_el_convertidor_de_ruta_no_es_parte_de_la_ruta(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\n\nrouter = APIRouter()\n\n'
            '@router.get("/files/{file_path:path}")\ndef leer(file_path: str):\n    return {}\n',
            encoding="utf-8",
        )
        assert superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0].ruta == "/files/{file_path}"

    def test_include_in_schema_false_no_se_publica(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter\n\nrouter = APIRouter()\n\n'
            '@router.get("/oculta", include_in_schema=False)\ndef oculta():\n    return {}\n\n'
            '@router.get("/visible")\ndef visible():\n    return {}\n',
            encoding="utf-8",
        )
        rutas = [op.ruta for op in superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones]
        assert rutas == ["/visible"]

    def test_mount_no_publica_las_rutas_del_hijo(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter, FastAPI\n\napp = FastAPI()\nsubapi = APIRouter()\n\n'
            '@app.get("/app")\ndef raiz():\n    return {}\n\n'
            '@subapi.get("/sub")\ndef sub():\n    return {}\n\n'
            'app.mount("/subapi", subapi)\n',
            encoding="utf-8",
        )
        rutas = [op.ruta for op in superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones]
        assert rutas == ["/app"]

    def test_un_formulario_escalar_si_publica_su_campo(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from typing import Annotated\nfrom fastapi import APIRouter, File, Form\n'
            'from fastapi import UploadFile\n\nrouter = APIRouter()\n\n'
            '@router.post("/login/")\ndef login(username: Annotated[str, Form()],'
            ' foto: Annotated[UploadFile, File()]):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [(c.nombre, c.tipo) for c in op.peticion] == [("username", "string"), ("foto", "string")]

    def test_all_of_no_borra_los_escalares_del_mismo_nivel(self, tmp_path):
        """FastAPI publica `<varios Form()>` como `allOf: [modelos]` **mas** sus `properties`."""
        documento = {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1"},
            "paths": {"/p/": {"post": {"requestBody": {"content": {"application/x-www-form-urlencoded": {
                "schema": {"$ref": "#/components/schemas/Body"}}}}, "responses": {"200": {}}}}},
            "components": {"schemas": {
                "Body": {"allOf": [{"$ref": "#/components/schemas/Perfil"}],
                         "properties": {"token": {"type": "string"}}, "required": ["token"], "type": "object"},
                "Perfil": {"properties": {"name": {"type": "string"}}, "required": ["name"], "type": "object"},
            }},
        }
        op = superficie_openapi(documento).operaciones[0]
        assert {c.nombre for c in op.peticion} == {"name", "token"}

    def test_un_literal_se_publica_con_el_tipo_de_sus_valores(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from typing import Literal\nfrom fastapi import APIRouter\n'
            'from pydantic import BaseModel\n\nrouter = APIRouter()\n\n'
            'class Persona(BaseModel):\n    kind: Literal["person"]\n\n'
            '@router.post("/x")\ndef crear(p: Persona):\n    return {}\n',
            encoding="utf-8",
        )
        op = superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones[0]
        assert [(c.nombre, c.tipo) for c in op.peticion] == [("kind", "string")]

    def test_los_webhooks_no_son_rutas(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import FastAPI\n\napp = FastAPI()\n\n'
            '@app.webhooks.post("new-subscription")\ndef aviso():\n    return {}\n\n'
            '@app.get("/users/")\ndef leer():\n    return []\n',
            encoding="utf-8",
        )
        rutas = sorted(op.ruta for op in superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones)
        assert rutas == ["/users/", "webhooks/new-subscription"]

    def test_un_router_de_callbacks_no_es_un_router_montado(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from fastapi import APIRouter, FastAPI\n\napp = FastAPI()\ncb = APIRouter()\n\n'
            '@cb.post("{$callback_url}/aviso")\ndef aviso():\n    return {}\n\n'
            '@app.post("/pagar/", callbacks=cb.routes)\ndef pagar():\n    return {}\n',
            encoding="utf-8",
        )
        rutas = [op.ruta for op in superficie_servidor(sorted(tmp_path.rglob("*.py"))).operaciones]
        assert rutas == ["/pagar/"]

    def test_el_formulario_de_seguridad_declara_lo_que_no_mide(self, tmp_path):
        (tmp_path / "app.py").write_text(
            'from typing import Annotated\nfrom fastapi import APIRouter, Depends\n'
            'from fastapi.security import OAuth2PasswordRequestForm\n\nrouter = APIRouter()\n\n'
            '@router.post("/token")\ndef login(form: Annotated[OAuth2PasswordRequestForm, Depends()]):\n    return {}\n',
            encoding="utf-8",
        )
        servidor = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        assert servidor.operaciones[0].cuerpo_por_dependencia is True
        assert servidor.marco["cuerpos_no_medidos_por_dependencia"] == 1


class TestServidorQueElLectorNoEntiende:
    """Cuarto oraculo: flask-smorest @ /tmp/app_flask, spec generada por el framework.

    Medido el 25-Nov-2026 sobre la app de ejemplo de flask-smorest: el contrato
    publica 3 operaciones y este lector lee 0 rutas. La respuesta correcta no es
    "el servidor no las declara" (falso: las declara en otra forma) ni RC 0
    (seria un falso verde en la direccion peligrosa), sino NO_MEDIDO.
    """

    FLASK = '''
from flask.views import MethodView
from flask_smorest import Blueprint

blp = Blueprint("items", "items", url_prefix="/items")

@blp.route("/")
class Items(MethodView):
    def get(self):
        return []

    def post(self):
        return {}
'''

    SPEC = {
        "openapi": "3.0.3",
        "info": {"title": "Ejemplo smorest", "version": "v1"},
        "paths": {
            "/items/": {"get": {"responses": {"200": {"description": "ok"}}},
                        "post": {"responses": {"201": {"description": "ok"}}}},
            "/items/{item_id}": {"get": {"responses": {"200": {"description": "ok"}}}},
        },
    }

    def _medir(self, tmp_path, fuente, spec_dict):
        (tmp_path / "app.py").write_text(fuente)
        codigo = superficie_servidor(sorted(tmp_path.rglob("*.py")))
        spec = superficie_openapi(spec_dict)
        return codigo, spec

    def test_declara_que_lector_uso(self, tmp_path):
        codigo, _ = self._medir(tmp_path, self.FLASK, self.SPEC)
        assert codigo.marco["lector"] == "fastapi"

    def test_un_aviso_que_nombra_al_lector_y_no_una_deriva_por_ruta(self, tmp_path):
        codigo, spec = self._medir(tmp_path, self.FLASK, self.SPEC)
        hallazgos = comparar(codigo, spec)
        assert [h.clase for h in hallazgos] == ["SERVIDOR_NO_LEIDO"]
        assert hallazgos[0].severidad == "WARN"
        assert "fastapi" in hallazgos[0].detalle
        assert "3 operaciones" in hallazgos[0].detalle
        assert rc_de(hallazgos, codigo, spec) == 2

    def test_servidor_y_contrato_vacios_siguen_en_no_medido_sin_aviso(self, tmp_path):
        codigo, spec = self._medir(tmp_path, "x = 1\n", {"openapi": "3.0.3", "info": {}, "paths": {}})
        assert comparar(codigo, spec) == []
        assert rc_de([], codigo, spec) == 2
