#!/usr/bin/env python3
"""Web UI del pipeline KDP (ES). Servicio: kdp-webui, puerto 8080"""
import json, shutil, subprocess, time
import concurrent.futures
from pathlib import Path
import httpx, yaml
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
import kdp

app = FastAPI()
CANCEL_SLUGS = set()
BOOKS, BAGS, PROMPTS, OLLAMA = kdp.BOOKS, kdp.BAGS, kdp.PROMPTS, kdp.OLLAMA

def ollama_stream(model, system, user, temperature=0.7):
    with httpx.stream("POST", f"{OLLAMA}/api/chat", timeout=1800, json={
        "model": model, "stream": True, "options": {"temperature": temperature},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}) as r:
        for line in r.iter_lines():
            if not line:
                continue
            j = json.loads(line)
            if j.get("error"):
                raise RuntimeError(j["error"])
            tok = j.get("message", {}).get("content", "")
            if tok:
                yield tok

@app.get("/api/logs")
def get_logs(n: int = 100, level: str = "", slug: str = ""):
    c = kdp.db()
    q = "SELECT ts,level,slug,story,agent,model,elapsed,words_in,words_out,detail FROM events"
    conds, args = [], []
    if level:
        conds.append("level=?"); args.append(level)
    if slug:
        conds.append("slug=?"); args.append(slug)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; args.append(n)
    rows = c.execute(q, args).fetchall()
    c.close()
    cols = ["ts","level","slug","story","agent","model","elapsed","words_in","words_out","detail"]
    return [dict(zip(cols, r)) for r in rows]

@app.get("/api/models")
def models():
    return httpx.get(f"{OLLAMA}/api/tags", timeout=30).json()

@app.get("/api/bags")
def get_bags():
    return yaml.safe_load(BAGS.read_text())

@app.post("/api/bags")
def save_bags(data: dict = Body(...)):
    BAGS.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return {"ok": True}

@app.get("/api/books")
def books():
    out = []
    for p in sorted(BOOKS.glob("*/book.yaml")):
        cfg = yaml.safe_load(p.read_text())
        d = p.parent
        cfg["written"] = len(list((d / "stories").glob("st*.md")))
        cfg["edited"] = len(list((d / "edited").glob("st*.md")))
        cfg["has_outline"] = (d / "02_outline.json").exists()
        cfg["has_epub"] = (d / f"{cfg['slug']}.epub").exists()
        out.append(cfg)
    return out

@app.post("/api/books")
def create_book(data: dict = Body(...)):
    slug = kdp.slugify(data["title"])
    if (BOOKS / slug).exists():
        raise HTTPException(400, "Ya existe un libro con ese titulo")
    stories = int(data.get("stories", 30))
    words = int(data.get("words_per_story", 450))
    if not (1 <= stories <= 200):
        raise HTTPException(400, "stories debe estar entre 1 y 200")
    if not (100 <= words <= 2000):
        raise HTTPException(400, "words_per_story debe estar entre 100 y 2000")
    kdp.new(title=data["title"], language=data.get("language", "en"),
            stories=stories, words_per_story=words,
            model_local=data.get("model_local", "qwen2.5:3b"),
            model_cloud=data.get("model_cloud", "minimax-m2.5:cloud"))
    return {"slug": slug}

@app.delete("/api/books/{slug}")
def delete_book(slug: str):
    d = BOOKS / slug
    if not d.exists():
        raise HTTPException(404, "No existe")
    shutil.rmtree(d)
    c = kdp.db()
    c.execute("DELETE FROM books WHERE slug=?", (slug,))
    c.execute("DELETE FROM runs WHERE slug=?", (slug,))
    c.commit(); c.close()
    return {"ok": True}

@app.post("/api/books/{slug}/outline")
def gen_outline(slug: str):
    kdp.outline(slug)
    return {"ok": True}

@app.get("/api/books/{slug}/outline")
def get_outline(slug: str):
    d, _ = kdp.load_book(slug)
    f = d / "02_outline.json"
    if not f.exists():
        raise HTTPException(404, "Sin specs")
    return json.loads(f.read_text())

@app.put("/api/books/{slug}/outline")
def save_outline(slug: str, data: dict = Body(...)):
    d, _ = kdp.load_book(slug)
    (d / "02_outline.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True}

@app.get("/api/books/{slug}/progress")
def progress(slug: str):
    """Estado de todos los cuentos en una sola llamada."""
    d, _ = kdp.load_book(slug)
    outline = json.loads((d / "02_outline.json").read_text()) if (d / "02_outline.json").exists() else {"stories": []}
    specs = {s["number"]: s for s in outline["stories"]}
    drafts, drafts_bad = [], []
    for pp in (d / "stories").glob("st*.md"):
        n = int(pp.stem[2:]); txt = pp.read_text()
        drafts.append(n)
        if kdp.qc_story(txt, specs.get(n, {})):
            drafts_bad.append(n)
    edited = {}
    for pp in (d / "edited").glob("st*.md"):
        n = int(pp.name[2:5])
        edited.setdefault(n, []).append(pp.name.split(".", 1)[1].rsplit(".md", 1)[0])
    finals = sorted(int(pp.stem[2:]) for pp in (d / "final").glob("st*.md")) if (d / "final").exists() else []
    return {"drafts": sorted(drafts), "drafts_bad": sorted(drafts_bad), "edited": edited, "finals": finals}

@app.get("/api/books/{slug}/story/{n}")
def get_story(slug: str, n: int):
    d, _ = kdp.load_book(slug)
    draft = d / "stories" / f"st{n:03d}.md"
    fin = d / "final" / f"st{n:03d}.md"
    res = {"draft": draft.read_text() if draft.exists() else "", "edited": {},
           "final": fin.read_text() if fin.exists() else ""}
    for p in sorted((d / "edited").glob(f"st{n:03d}.*.md")):
        res["edited"][p.name.split(".", 1)[1].rsplit(".md", 1)[0]] = p.read_text()
    return res

@app.put("/api/books/{slug}/story/{n}/draft")
def save_draft(slug: str, n: int, data: dict = Body(...)):
    d, _ = kdp.load_book(slug)
    (d / "stories" / f"st{n:03d}.md").write_text(data.get("text", ""))
    return {"ok": True}

@app.put("/api/books/{slug}/story/{n}/final")
def save_final(slug: str, n: int, data: dict = Body(...)):
    d, _ = kdp.load_book(slug)
    (d / "final").mkdir(exist_ok=True)
    (d / "final" / f"st{n:03d}.md").write_text(data.get("text", ""))
    return {"ok": True}

@app.post("/api/books/{slug}/write/{n}")
def write_story(slug: str, n: int, model: str = ""):
    d, cfg = kdp.load_book(slug)
    outline = json.loads((d / "02_outline.json").read_text())
    spec = next((s for s in outline["stories"] if s["number"] == n), None)
    if not spec:
        raise HTTPException(404, "Spec no encontrada")
    def gen():
        parts = []
        try:
            for i, seg in kdp.iter_story_segments(cfg, spec):
                parts.append(seg)
                yield seg + "\n\n"
        except Exception as e:
            kdp.event("ERROR", slug, n, "webui_write", cfg["model_local"], detail=str(e))
            yield f"\n[ERROR: {e}]"
            return
        acc = "\n\n".join(parts).strip()
        issues = kdp.qc_story(acc, spec)
        if issues:
            yield f"\n[QC fallo: {', '.join(issues)}] Reintentando (hasta 2)...\n"
            acc, issues = kdp.generate_story(cfg, spec, retries=2)
        if issues:
            kdp.event("ERROR", slug, n, "webui_write", cfg["model_local"], detail=f"rechazado: {issues}")
            yield f"\n[QC persiste: {', '.join(issues)}. NO SE GUARDO. Revisa spec o proba otro modelo.]\n"
            return
        (d / "stories" / f"st{n:03d}.md").write_text(acc)
        kdp.event("INFO", slug, n, "webui_write", cfg["model_local"], words_out=len(acc.split()), detail="guardado ok")
        yield "\n[Borrador guardado y validado]\n"
    return StreamingResponse(gen(), media_type="text/plain")

@app.post("/api/books/{slug}/edit/{n}")
def edit_story(slug: str, n: int, model: str = ""):
    d, cfg = kdp.load_book(slug)
    src = d / "stories" / f"st{n:03d}.md"
    if not src.exists():
        raise HTTPException(404, "Primero escribi el borrador")
    m = model or cfg["model_cloud"]
    outline = json.loads((d / "02_outline.json").read_text())
    spec = next((s for s in outline["stories"] if s["number"] == n), {})
    text = src.read_text()
    issues = kdp.run_critic(cfg, spec, text, m)
    sys_p = (PROMPTS / "editor.md").read_text()
    payload = json.dumps({"language": cfg["language"], "story_spec": spec,
                          "story_text": text, "known_issues": issues})
    tag = m.replace(":", "_").replace("/", "_")
    t0 = time.time()

    def gen():
        acc = ""
        try:
            for tok in ollama_stream(m, sys_p, payload, 0.4):
                acc += tok
                yield tok
        except Exception as e:
            kdp.event("ERROR", slug, n, "webui_edit", m, time.time() - t0, detail=str(e))
            yield f"\n[ERROR del modelo: {e}]"
            return
        if acc.strip():
            (d / "edited" / f"st{n:03d}.{tag}.md").write_text(acc)
            kdp.event("INFO", slug, n, "webui_edit", m, time.time() - t0, words_out=len(acc.split()), detail="ok")
    return StreamingResponse(gen(), media_type="text/plain")

@app.post("/api/books/{slug}/polish/{n}")
def polish_story(slug: str, n: int, model: str = ""):
    d, cfg = kdp.load_book(slug)
    m = model or cfg["model_cloud"]
    src = kdp._pick_edited(d, n)
    if src is None:
        raise HTTPException(404, "Primero edita el cuento (Paso 4)")
    outline = json.loads((d / "02_outline.json").read_text())
    spec = next((s for s in outline["stories"] if s["number"] == n), {})
    text = src.read_text()

    def gen():
        nonlocal_text = text
        try:
            for r in range(2):
                report = kdp.run_reader(cfg, spec, nonlocal_text, m)
                issues = report.get("unanswered", []) + report.get("weird", [])
                if report.get("pass") and not issues:
                    yield f"[Ronda {r+1}: lector infantil conforme]\n\n"
                    break
                yield f"[Ronda {r+1}: {len(issues)} problemas detectados]\n"
                for i in issues[:8]:
                    yield f"  - {i}\n"
                yield "\n[Puliendo...]\n\n"
                sys_p = (kdp.PROMPTS / "polisher.md").read_text()
                payload = json.dumps({"language": cfg["language"], "story_spec": spec,
                                      "story_text": nonlocal_text, "reader_report": report})
                acc = ""
                for tok in ollama_stream(m, sys_p, payload, 0.4):
                    acc += tok
                    yield tok
                nonlocal_text = acc
                yield "\n\n"
        except Exception as e:
            yield f"\n[ERROR: {e}]"
            return
        (d / "final").mkdir(exist_ok=True)
        (d / "final" / f"st{n:03d}.md").write_text(nonlocal_text)
        yield "\n[Guardado en FINAL]"
    return StreamingResponse(gen(), media_type="text/plain")

@app.post("/api/books/{slug}/process/{n}")
def process_story(slug: str, n: int, model: str = ""):
    d, cfg = kdp.load_book(slug)
    m = model or cfg["model_cloud"]
    src = d / "stories" / f"st{n:03d}.md"
    if not src.exists():
        raise HTTPException(404, "Primero escribi/aproba el borrador")
    outline = json.loads((d / "02_outline.json").read_text())
    spec = next((s for s in outline["stories"] if s["number"] == n), {})

    def gen():
        try:
            yield "[1/3 Edicion cloud...]\n"
            text, issues, tok = kdp._edit_flow(d, cfg, n, m)
            if issues:
                yield f"  critic: {len(issues)} defectos corregidos\n"
            tag = m.replace(":", "_").replace("/", "_")
            (d / "edited" / f"st{n:03d}.{tag}.md").write_text(text)
            yield "[2/3 Consistencia de mundo...]\n"
            conflicts = kdp.run_world(cfg, spec, text, m)
            if conflicts:
                for c in conflicts[:8]:
                    yield f"  - {c.get('issue','')}\n"
                issues2 = [f"{c.get('issue','')} -> fix: {c.get('fix','')}" for c in conflicts]
                text, _ = kdp.fix_with_editor(cfg, spec, text, issues2, m)
                yield "  corregidos\n"
            else:
                yield "  sin conflictos\n"
            yield "[3/3 Lector infantil + pulido...]\n"
            for r in range(2):
                report = kdp.run_reader(cfg, spec, text, m)
                problems = report.get("unanswered", []) + report.get("weird", [])
                if report.get("pass") and not problems:
                    yield f"  ronda {r+1}: conforme\n"
                    break
                yield f"  ronda {r+1}: {len(problems)} problemas\n"
                sys_p = (kdp.PROMPTS / "polisher.md").read_text()
                payload = json.dumps({"language": cfg["language"], "story_spec": spec,
                                      "story_text": text, "reader_report": report})
                text, _ = kdp.call_ollama(m, sys_p, payload, temperature=0.4)
            (d / "final").mkdir(exist_ok=True)
            (d / "final" / f"st{n:03d}.md").write_text(text)
            yield "\n[FINAL guardado]\n\n" + text
        except Exception as e:
            yield f"\n[ERROR: {e}]"
    return StreamingResponse(gen(), media_type="text/plain")

@app.get("/api/version")
def version():
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                         cwd=Path(__file__).parent).decode().strip()
    except Exception:
        commit = "sin-git"
    return {"commit": commit,
            "webui_mtime": Path(__file__).stat().st_mtime,
            "kdp_mtime": (Path(__file__).parent / "kdp.py").stat().st_mtime}

@app.post("/api/books/{slug}/cancel_generation")
def cancel_generation(slug: str):
    CANCEL_SLUGS.add(slug)
    return {"ok": True}

@app.post("/api/books/{slug}/generate_all")
def generate_all(slug: str, model: str = ""):
    d, cfg = kdp.load_book(slug)
    outline = json.loads((d / "02_outline.json").read_text())
    m = model or cfg["model_cloud"]
    total = len(outline["stories"])
    CANCEL_SLUGS.discard(slug)
    STORY_TIMEOUT = 900  # process() encadena hasta ~9 llamadas cloud; 600s cortaba de mas
    HEARTBEAT = 15

    def _run_phase(label, n, fn, *args):
        # OJO: sin "with" -- el executor NO se cierra aca. Si cerramos (shutdown(wait=True)),
        # Python bloquea la salida de esta funcion hasta que el hilo real termine,
        # anulando el timeout (es lo que colgaba antes). Se deja el hilo huerfano
        # corriendo en 2do plano; termina solo cuando la llamada http real responda o
        # el timeout de call_ollama (450s) la corte.
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(fn, *args)
        waited = 0
        try:
            while True:
                if slug in CANCEL_SLUGS:
                    yield f"[{n}] cancelado por el usuario durante {label} (el trabajo en curso puede seguir un rato en 2do plano)\n"
                    yield ("__CANCELLED__", True)
                    return
                try:
                    result = fut.result(timeout=HEARTBEAT)
                    yield ("__RESULT__", result)
                    return
                except concurrent.futures.TimeoutError:
                    waited += HEARTBEAT
                    yield f"[{n}] ... {label} ({waited}s)\n"
                    if waited >= STORY_TIMEOUT:
                        yield f"[{n}] TIMEOUT en {label} tras {waited}s (cuento saltado; puede terminar solo en 2do plano sin guardarse)\n"
                        kdp.event("ERROR", slug, n, "generate_all", m, detail=f"timeout {label} tras {waited}s")
                        return
                except Exception as e:
                    yield f"[{n}] ERROR en {label}: {e}\n"
                    kdp.event("ERROR", slug, n, "generate_all", m, detail=str(e))
                    return
        finally:
            ex.shutdown(wait=False)

    def gen():
        for st in outline["stories"]:
            n = st["number"]
            if slug in CANCEL_SLUGS:
                yield f"\n=== Cancelado por el usuario antes del cuento {n} ===\n"
                CANCEL_SLUGS.discard(slug)
                return
            yield f"\n=== Cuento {n}/{total}: {st['protagonist']} {st['name']} ===\n"
            draft = d / "stories" / f"st{n:03d}.md"
            needs = (not draft.exists()) or bool(kdp.qc_story(draft.read_text(), st))
            cancelled = False
            if needs:
                yield f"[{n}] escribiendo borrador...\n"
                holder = {}
                for item in _run_phase("escribiendo borrador", n, kdp.generate_story, cfg, st, 2):
                    if isinstance(item, tuple):
                        if item[0] == "__RESULT__": holder["v"] = item[1]
                        elif item[0] == "__CANCELLED__": cancelled = True
                    else:
                        yield item
                if cancelled:
                    CANCEL_SLUGS.discard(slug); return
                if "v" not in holder:
                    continue
                out, issues = holder["v"]
                if issues:
                    yield f"[{n}] SALTEADO, defectos persistentes: {issues}\n"
                    kdp.event("ERROR", slug, n, "generate_all", cfg["model_local"], detail=issues)
                    continue
                draft.write_text(out)

            yield f"[{n}] procesando (edicion+mundo+lector/pulido)...\n"
            holder = {}
            for item in _run_phase("procesando", n, kdp.process, slug, n, m):
                if isinstance(item, tuple):
                    if item[0] == "__RESULT__": holder["v"] = item[1]
                    elif item[0] == "__CANCELLED__": cancelled = True
                else:
                    yield item
            if cancelled:
                CANCEL_SLUGS.discard(slug); return
            if "v" in holder:
                yield f"[{n}] OK -> final/st{n:03d}.md\n"

        finals = sorted(int(p.stem[2:]) for p in (d / "final").glob("st*.md"))
        faltan = [s["number"] for s in outline["stories"] if s["number"] not in finals]
        CANCEL_SLUGS.discard(slug)
        if faltan:
            yield f"\n=== Terminado con FALTANTES: {faltan}. No armes el EPUB todavia. ===\n"
        else:
            yield f"\n=== Libro COMPLETO: {total}/{total} en final/. Listo para EPUB. ===\n"
    return StreamingResponse(gen(), media_type="text/plain")

@app.post("/api/books/{slug}/build")
def build(slug: str, editor_tag: str = ""):
    kdp.build(slug, editor_tag)
    return {"ok": True}

@app.post("/api/books/{slug}/report")
def gen_report(slug: str):
    out = kdp.report(slug)
    return {"ok": True, "file": out.name}

@app.get("/api/books/{slug}/report")
def get_report(slug: str):
    d, _ = kdp.load_book(slug)
    f = d / "pipeline_report.md"
    if not f.exists():
        raise HTTPException(404, "Sin reporte. Genéralo primero.")
    return FileResponse(f, filename=f"{slug}_pipeline_report.md", media_type="text/markdown")

@app.get("/api/books/{slug}/epub")
def epub(slug: str):
    d, _ = kdp.load_book(slug)
    f = d / f"{slug}.epub"
    if not f.exists():
        raise HTTPException(404, "Sin EPUB. Usa el paso 5.")
    return FileResponse(f, filename=f.name)

@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "webui.html").read_text()
