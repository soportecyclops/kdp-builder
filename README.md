# KDP Builder — Pipeline de libros infantiles

Sistema local (Ollama + FastAPI) para generar cuentos infantiles de forma asistida:
sorteo de ingredientes por "bolsas" → borrador segmentado (modelo local) →
edición cloud → chequeo de consistencia de mundo → lector infantil simulado + pulido → EPUB.

## Componentes
- `kdp.py` — CLI y lógica del pipeline (agentes: writer, editor, critic, world, reader, polisher)
- `webui.py` — backend FastAPI
- `webui.html` — frontend (SPA sin build, un solo archivo)
- `prompts/` — system prompts de cada agente
- `config_bags.yaml` / `config_bags_es.yaml` — bolsas de ingredientes narrativos (EN/ES)

## Uso
```bash
python3 -m venv venv && source venv/bin/activate
pip install httpx pyyaml jinja2 rich typer fastapi uvicorn
python -m uvicorn webui:app --host 0.0.0.0 --port 8080
```

Requiere una instancia de Ollama accesible (configurar `OLLAMA` en `kdp.py`).

## Servicio systemd
Ver `deploy/kdp-webui.service`.
