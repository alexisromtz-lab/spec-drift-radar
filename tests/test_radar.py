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
