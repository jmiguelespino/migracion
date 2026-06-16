# Fase 5: Administración

**Migración:** Visual FoxPro → Python + FastAPI + React

(Demo) Migración del módulo Administración

## Explicación

(Modo demo) Código de ejemplo para la fase "Administración", generado localmente sin llamar a Claude ni gastar tokens. Los modelos reflejan las tablas reales del ZIP.

## Archivos generados (carpeta `src/`)

- `models.py` — Modelos (demo, desde tablas reales)
- `api.py` — Endpoints (demo)

Incluye tests automatizados en `tests/test_fase.py`.

## Instrucciones de instalación

pip install fastapi uvicorn pydantic
uvicorn api:app --reload

## Interfaz

(Demo) Interfaz web simulada para esta fase.

---
_Generado por LegacyMigrator._