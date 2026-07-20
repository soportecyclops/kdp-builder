#!/usr/bin/env python3
"""KDP pipeline infantil: new / outline / write / edit / build / status"""
import json, os, sqlite3, re, time, random
from pathlib import Path
from datetime import datetime
import httpx, yaml, typer
from rich import print
from rich.progress import Progress

BASE = Path.home() / "kdp"
BOOKS = BASE / "books"
PROMPTS = BASE / "prompts"
DB = BASE / "db.sqlite"
OLLAMA = "http://192.168.0.10:11434"

app = typer.Typer(no_args_is_help=True)

OPTIONAL = ["twist", "weather", "celebration", "companion", "time_of_day", "season", "antagonist", "reward"]

def bags_path(language="en"):
    if str(language).startswith("es"):
        p = BASE / "config_bags_es.yaml"
        if p.exists():
            return p
    return BASE / "config_bags.yaml"

BAGS = bags_path()

def db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS books(slug TEXT PRIMARY KEY, created TEXT, stage TEXT, config TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, slug TEXT, agent TEXT, model TEXT, started TEXT, elapsed REAL, tokens INTEGER, ok INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, level TEXT, slug TEXT, story INTEGER, agent TEXT, model TEXT, elapsed REAL, words_in INTEGER, words_out INTEGER, detail TEXT)")
    return c

def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:60]

def call_ollama(model, system, user, temperature=0.7, timeout=1800, seed=None):
    opts = {"temperature": temperature}
    if seed is not None:
        opts["seed"] = seed
    r = httpx.post(f"{OLLAMA}/api/chat", timeout=timeout, json={
        "model": model, "stream": False, "options": opts,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    return j["message"]["content"], j.get("eval_count", 0)

LOGFILE = BASE / "logs" / "pipeline.log"

def event(level, slug, story, agent, model, elapsed=0.0, words_in=0, words_out=0, detail=""):
    """Log estructurado: DB + archivo. level: INFO|WARN|ERROR"""
    try:
        c = db()
        c.execute("INSERT INTO events(ts,level,slug,story,agent,model,elapsed,words_in,words_out,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (datetime.now().isoformat(timespec='seconds'), level, slug, story, agent, model,
                   round(elapsed, 1), words_in, words_out, str(detail)[:2000]))
        c.commit(); c.close()
    except Exception:
        pass
    try:
        LOGFILE.parent.mkdir(exist_ok=True)
        with open(LOGFILE, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} [{level}] {slug} st{story:03d} {agent}/{model} {elapsed:.1f}s in={words_in}w out={words_out}w {detail}\n")
    except Exception:
        pass

def log(slug, agent, model, elapsed, tokens, ok):
    c = db()
    c.execute("INSERT INTO runs(slug,agent,model,started,elapsed,tokens,ok) VALUES(?,?,?,?,?,?,?)",
              (slug, agent, model, datetime.now().isoformat(), elapsed, tokens, int(ok)))
    c.commit(); c.close()

def load_book(slug):
    d = BOOKS / slug
    if not d.exists():
        raise typer.Exit(f"No existe libro: {slug}")
    return d, yaml.safe_load((d / "book.yaml").read_text())

def set_stage(slug, stage):
    c = db(); c.execute("UPDATE books SET stage=? WHERE slug=?", (stage, slug)); c.commit(); c.close()

def _profile_to_fields(p):
    if isinstance(p, str):
        parts = [x.strip() for x in p.split("|")]
        animal = parts[0] if parts else "Bear"
        role = parts[1] if len(parts) > 1 else "hero"
        traits = [t.strip() for t in parts[2].split(",")] if len(parts) > 2 else []
        return animal, role, traits
    return p.get("animal", "Bear"), p.get("role", "hero"), p.get("traits", [])

def extract_json(text):
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise ValueError("Sin JSON")
    return json.loads(m.group())

# ---------- beats determinísticos ----------
def build_beats(spec):
    p = f"{spec['name']} the {spec['protagonist']}"
    beats = []
    intro = f"Beat 1 (opening): Introduce {p}, who is {spec['traits']}, living in the {spec['setting']}."
    extras = []
    if spec.get("season"): extras.append(f"It is {spec['season']}.")
    if spec.get("time_of_day"): extras.append(f"It is {spec['time_of_day']}.")
    if spec.get("weather"): extras.append(f"The weather is {spec['weather']}.")
    if spec.get("companion"): extras.append(f"{p} is with their companion: {spec['companion']} (this companion stays with {p} for the whole story).")
    beats.append(intro + " " + " ".join(extras))
    beats.append(f"Beat 2 (goal): {p} wants to: {spec['goal']}. Show why it matters to them. The goal never changes for the rest of the story.")
    prob = f"Beat 3 (problem): This exact problem interrupts the goal: {spec['problem']}."
    if spec.get("antagonist"):
        prob += f" The antagonist involved is: {spec['antagonist']}. The antagonist causes or worsens the problem but is never violent or scary."
    prob += f" {p} feels {spec['emotion']}."
    beats.append(prob)
    beats.append(f"Beat 4 (help): The helper, {spec['helper']}, arrives walking/flying into the scene in a possible way, and gives {p} the {spec['object']} plus one piece of advice. The helper does NOT solve the problem.")
    beats.append(f"Beat 5 (attempt and solution): {p} tries, struggles a little, and then solves the problem THEMSELVES by applying: {spec['solution']}. The {spec['object']} may assist with its one simple power, but the key action and idea come from {p}.")
    outcome = f"Beat 6 (outcome): The goal from Beat 2 is achieved."
    if spec.get("reward"): outcome += f" {p} receives: {spec['reward']}."
    if spec.get("celebration"): outcome += f" There is a brief, small celebration: {spec['celebration']} (directly tied to achieving the goal, nothing new is built or started)."
    if spec.get("twist"): outcome += f" Include this gentle twist naturally: {spec['twist']}."
    beats.append(outcome)
    beats.append(f"Beat 7 (closing): Calm, sleepy ending matching: {spec['ending']}. All characters present are accounted for or say goodbye. Then the final line: Lesson: {spec['moral']}.")
    return beats
# -------------------------------------------

def qc_story(text, spec):
    issues = []
    headings = len(re.findall(r'^## ', text, re.M))
    if headings == 0:
        issues.append("sin heading")
    if headings > 1:
        issues.append("historia duplicada (mas de un heading)")
    if not re.search(r'^(Lesson|Moraleja):', text, re.M):
        issues.append("sin linea de moraleja")
    lessons = len(re.findall(r'^(Lesson|Moraleja):', text, re.M))
    if lessons > 1:
        issues.append(f"{lessons} lineas de moraleja")
    # parrafos duplicados
    paras = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 60]
    if len(paras) != len(set(paras)):
        issues.append("parrafos duplicados")
    words = len(text.split())
    tgt = spec.get("word_target", 450)
    if words < tgt * 0.7:
        issues.append(f"muy corto ({words}w, target {tgt})")
    if words > tgt * 1.3:
        issues.append(f"muy largo ({words}w, target {tgt})")
    return issues

def _sanitize_segment(seg, i, is_last, per_beat, prev_tail):
    """Limpia un segmento: sin heading duplicado, sin Lesson prematura,
    sin repetir el texto previo, recortado al presupuesto."""
    seg = seg.strip()
    if i > 0:
        seg = re.sub(r'^## .*\n?', '', seg).strip()
    if not is_last:
        seg = re.sub(r'^(Lesson|Moraleja):.*$', '', seg, flags=re.M).strip()
    else:
        # una sola linea de Lesson, al final
        lessons = re.findall(r'^(?:Lesson|Moraleja):.*$', seg, flags=re.M)
        if len(lessons) > 1:
            seg = re.sub(r'^(?:Lesson|Moraleja):.*$\n?', '', seg, flags=re.M).strip()
            seg += "\n\n" + lessons[-1]
    # dedupe: si arranca repitiendo la cola previa, cortarla
    if prev_tail:
        tail_words = prev_tail.split()[-12:]
        probe = " ".join(tail_words)
        idx = seg.find(probe)
        if idx >= 0:
            seg = seg[idx + len(probe):].strip()
    # recorte por presupuesto (2x max), en limite de oracion
    words = seg.split()
    if len(words) > int(per_beat * 1.5):
        cut = " ".join(words[:int(per_beat * 1.5)])
        m = re.search(r'^(.*[.!?])', cut, re.S)
        seg = (m.group(1) if m else cut).strip()
    return seg

def iter_story_segments(cfg, spec, seg_retries=2):
    """Escritura segmentada con autoconsulta: una llamada por beat."""
    sys_p = (PROMPTS / "writer_segment.md").read_text()
    beats = build_beats(spec)
    target = spec.get("word_target", 450)
    BEAT_WEIGHTS = [0.10, 0.12, 0.15, 0.20, 0.23, 0.10, 0.10]
    weights = BEAT_WEIGHTS if len(beats) == len(BEAT_WEIGHTS) else [1.0/len(beats)] * len(beats)
    budgets = [max(40, int(target * w)) for w in weights]
    slug = cfg.get("slug", "?"); story = spec.get("number", 0)
    full = ""
    covered = []
    for i, beat in enumerate(beats):
        is_last = i == len(beats) - 1
        per_beat = budgets[i]
        payload = json.dumps({
            "story_spec": spec, "covered": covered,
            "previous_text": " ".join(full.split()[-120:]),
            "current_beat": beat, "words_budget": per_beat,
            "is_first": i == 0, "is_last": is_last,
        })
        seg = ""
        for attempt in range(seg_retries + 1):
            t0 = time.time()
            seed = (cfg.get("seed", 0) * 1000) + (story * 100) + (i * 10) + attempt
            try:
                raw, _ = call_ollama(cfg["model_local"], sys_p, payload, temperature=0.6, seed=seed)
            except Exception as e:
                event("ERROR", slug, story, f"writer_seg{i+1}", cfg["model_local"],
                      time.time() - t0, detail=f"attempt {attempt+1}: {e}")
                continue
            seg = _sanitize_segment(raw, i, is_last, per_beat, full)
            if i == 0:
                seg = re.sub(r'^## Story N\b', f"## Story {spec.get('number',1)}", seg)
            ok = len(seg.split()) >= per_beat * 0.3
            if i == 0 and not seg.startswith("##"):
                ok = False
            event("INFO" if ok else "WARN", slug, story, f"writer_seg{i+1}", cfg["model_local"],
                  time.time() - t0, len(payload.split()), len(seg.split()),
                  "" if ok else f"attempt {attempt+1} invalido ({len(seg.split())}w, budget {per_beat})")
            if ok:
                break
            seg = ""
        if not seg:
            seg = f"({beat})"
            event("ERROR", slug, story, f"writer_seg{i+1}", cfg["model_local"], detail="segmento vacio tras reintentos, marcador insertado")
        full += ("\n\n" if full else "") + seg
        covered.append(beat.split(":")[0] + ": done")
        yield i, seg
    if not re.search(r'^(Lesson|Moraleja):', full, re.M):
        tail = f"\n\nLesson: {spec.get('moral','')}"
        yield len(beats), tail

def generate_story(cfg, spec, retries=1):
    """Genera completo via segmentos. Devuelve (texto, defectos_QC)."""
    last, last_issues = "", ["no generado"]
    for attempt in range(retries + 1):
        parts = []
        for _, seg in iter_story_segments(cfg, spec):
            parts.append(seg)
        out = "\n\n".join(parts).strip()
        issues = qc_story(out, spec)
        event("INFO" if not issues else "WARN", cfg.get("slug","?"), spec.get("number",0),
              "qc", cfg["model_local"], words_out=len(out.split()), detail=issues or "ok")
        if not issues:
            return out, []
        last, last_issues = out, issues
    return last, last_issues

def run_critic(cfg, spec, text, model=None):
    """Critico semantico con LLM cloud. Devuelve lista de errores (vacia = ok)."""
    m = model or cfg["model_cloud"]
    sys_p = (PROMPTS / "critic.md").read_text()
    payload = json.dumps({"story_spec": spec, "story_text": text})
    try:
        out, _ = call_ollama(m, sys_p, payload, temperature=0.1)
        j = extract_json(out)
        return j.get("errors", []) if not j.get("pass", False) else []
    except Exception as e:
        return [f"(critic no disponible: {e})"]

@app.command()
def new(title: str, language: str = "en", niche: str = "children-bedtime",
        stories: int = 30, words_per_story: int = 450,
        model_local: str = "qwen2.5:3b", model_cloud: str = "minimax-m2.5:cloud",
        seed: int = 0):
    slug = slugify(title)
    d = BOOKS / slug
    if d.exists():
        raise typer.Exit(f"Ya existe: {slug}")
    d.mkdir(parents=True); (d / "stories").mkdir(); (d / "edited").mkdir(); (d / "final").mkdir()
    cfg = dict(slug=slug, title=title, language=language, niche=niche,
               stories=stories, words_per_story=words_per_story,
               model_local=model_local, model_cloud=model_cloud,
               seed=seed or random.randint(1, 999999),
               created=datetime.now().isoformat())
    (d / "book.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    c = db()
    c.execute("INSERT OR REPLACE INTO books VALUES(?,?,?,?)", (slug, cfg["created"], "created", json.dumps(cfg)))
    c.commit(); c.close()
    print(f"[green]Creado[/] {d} (seed={cfg['seed']})")

@app.command()
def outline(slug: str):
    d, cfg = load_book(slug)
    bags = yaml.safe_load(bags_path(cfg.get("language", "en")).read_text())
    compat = bags.pop("compat_setting", {})
    moral_map = bags.pop("moral_by_solution", {})
    hero_profiles = bags.pop("protagonist_profiles", None)
    villain_profiles = bags.pop("antagonist_profiles", None)
    rng = random.Random(cfg["seed"])
    used_pairs = set()
    specs = []
    for n in range(1, cfg["stories"] + 1):
        if hero_profiles:
            for _ in range(50):
                prof = rng.choice(hero_profiles)
                animal, role, traits = _profile_to_fields(prof)
                goal = rng.choice(bags["goal"])
                if (animal, goal) not in used_pairs:
                    used_pairs.add((animal, goal)); break
            prot, trait_str = animal, ", ".join(traits) if traits else rng.choice(bags["trait"])
        else:
            for _ in range(50):
                prot = rng.choice(bags["protagonist"])
                goal = rng.choice(bags["goal"])
                if (prot, goal) not in used_pairs:
                    used_pairs.add((prot, goal)); break
            trait_str = rng.choice(bags["trait"])
        setting = rng.choice(compat.get(prot, bags["setting"]))
        solution = rng.choice(bags["solution"])
        moral = moral_map.get(solution) or rng.choice(bags["moral"])
        spec = {
            "number": n, "protagonist": prot, "name": rng.choice(bags["name"]),
            "traits": trait_str, "setting": setting, "goal": goal,
            "problem": rng.choice(bags["problem"]), "helper": rng.choice(bags["helper"]),
            "object": rng.choice(bags["object"]), "emotion": rng.choice(bags["emotion"]),
            "solution": solution, "ending": rng.choice(bags["ending"]),
            "moral": moral, "word_target": cfg["words_per_story"],
        }
        for opt in rng.sample(OPTIONAL, 2):
            if opt == "antagonist" and villain_profiles:
                a, r_, t = _profile_to_fields(rng.choice(villain_profiles))
                spec["antagonist"] = f"{a} ({r_}: {', '.join(t)})" if t else a
            else:
                spec[opt] = rng.choice(bags[opt])
        specs.append(spec)
    (d / "02_outline.json").write_text(json.dumps({"stories": specs}, indent=2, ensure_ascii=False))
    print(f"[green]{len(specs)} specs[/] -> {d}/02_outline.json")
    set_stage(slug, "outline_done")
    print("[yellow]HITL: revisa/edita 02_outline.json antes de write[/]")

@app.command()
def write(slug: str, from_st: int = 1, to_st: int = 0):
    d, cfg = load_book(slug)
    outline = json.loads((d / "02_outline.json").read_text())
    stories = outline["stories"]
    if to_st == 0:
        to_st = len(stories)
    with Progress() as pr:
        task = pr.add_task("Writing", total=to_st - from_st + 1)
        for st in stories[from_st - 1:to_st]:
            t0 = time.time()
            try:
                out, issues = generate_story(cfg, st)
                (d / "stories" / f"st{st['number']:03d}.md").write_text(out)
                log(slug, "writer", cfg["model_local"], time.time() - t0, 0, not issues)
                if issues:
                    print(f"[yellow]st{st['number']:03d} defectos: {issues}[/]")
            except Exception as e:
                print(f"[red]fallo st{st['number']:03d}[/]: {e}")
                log(slug, "writer", cfg["model_local"], time.time() - t0, 0, False)
            pr.update(task, advance=1, description=f"st{st['number']:03d} {time.time()-t0:.0f}s")
    set_stage(slug, "draft_done")

def _edit_flow(d, cfg, story, model):
    """critic -> editor con known_issues. Devuelve texto final."""
    outline = json.loads((d / "02_outline.json").read_text())
    spec = next((s for s in outline["stories"] if s["number"] == story), {})
    text = (d / "stories" / f"st{story:03d}.md").read_text()
    issues = run_critic(cfg, spec, text, model)
    sys_p = (PROMPTS / "editor.md").read_text()
    payload = json.dumps({"language": cfg["language"], "story_spec": spec,
                          "story_text": text, "known_issues": issues})
    out, tok = call_ollama(model, sys_p, payload, temperature=0.4)
    return out, issues, tok

def run_reader(cfg, spec, text, model=None):
    m = model or cfg["model_cloud"]
    sys_p = (PROMPTS / "reader.md").read_text()
    payload = json.dumps({"story_spec": spec, "story_text": text})
    out, _ = call_ollama(m, sys_p, payload, temperature=0.2)
    j = extract_json(out)
    return j

def run_world(cfg, spec, text, model=None):
    m = model or cfg["model_cloud"]
    sys_p = (PROMPTS / "world.md").read_text()
    payload = json.dumps({"story_spec": spec, "story_text": text})
    out, _ = call_ollama(m, sys_p, payload, temperature=0.1)
    j = extract_json(out)
    return j.get("conflicts", []) if not j.get("pass", False) else []

def fix_with_editor(cfg, spec, text, issues, model=None):
    """Reusa el editor para corregir una lista de problemas manteniendo idioma."""
    m = model or cfg["model_cloud"]
    sys_p = (PROMPTS / "editor.md").read_text()
    payload = json.dumps({"language": cfg["language"], "story_spec": spec,
                          "story_text": text, "known_issues": issues})
    out, tok = call_ollama(m, sys_p, payload, temperature=0.4)
    return out, tok

def _pick_edited(d, story, tag=""):
    if tag:
        cand = sorted((d / "edited").glob(f"st{story:03d}.*{tag}*.md"))
        if cand:
            return cand[0]
    cand = sorted((d / "edited").glob(f"st{story:03d}.*.md"))
    return cand[0] if cand else None

@app.command()
def polish(slug: str, story: int, model: str = "", rounds: int = 2):
    """PASO 4.5: lector infantil + pulidor sobre la version editada -> final/"""
    d, cfg = load_book(slug)
    if not model:
        model = cfg["model_cloud"]
    src = _pick_edited(d, story)
    if src is None:
        raise typer.Exit(f"No hay version editada de st{story:03d}; corre edit primero")
    outline = json.loads((d / "02_outline.json").read_text())
    spec = next((s for s in outline["stories"] if s["number"] == story), {})
    text = src.read_text()
    (d / "final").mkdir(exist_ok=True)
    for r in range(rounds):
        t0 = time.time()
        report = run_reader(cfg, spec, text, model)
        if report.get("pass") and not report.get("unanswered") and not report.get("weird"):
            print(f"[green]st{story:03d} ronda {r+1}: lector conforme[/]")
            break
        issues = report.get("unanswered", []) + report.get("weird", [])
        print(f"[yellow]st{story:03d} ronda {r+1}: {len(issues)} problemas[/] {issues[:4]}")
        sys_p = (PROMPTS / "polisher.md").read_text()
        payload = json.dumps({"language": cfg["language"], "story_spec": spec,
                              "story_text": text, "reader_report": report})
        text, tok = call_ollama(model, sys_p, payload, temperature=0.4)
        log(slug, "polisher", model, time.time() - t0, tok, True)
    (d / "final" / f"st{story:03d}.md").write_text(text)
    print(f"[green]FINAL[/] {d}/final/st{story:03d}.md")

@app.command()
def polish_all(slug: str, model: str = "", from_st: int = 1, to_st: int = 0, rounds: int = 2):
    d, cfg = load_book(slug)
    outline = json.loads((d / "02_outline.json").read_text())
    n = len(outline["stories"])
    if to_st == 0:
        to_st = n
    for i in range(from_st, to_st + 1):
        try:
            polish(slug, i, model, rounds)
        except Exception as e:
            print(f"[red]st{i:03d}[/]: {e}")

@app.command()
def edit(slug: str, story: int, model: str = ""):
    d, cfg = load_book(slug)
    if not model:
        model = cfg["model_cloud"]
    src = d / "stories" / f"st{story:03d}.md"
    if not src.exists():
        raise typer.Exit(f"No existe {src}")
    t0 = time.time(); print(f"[cyan]Critic+Edit st{story:03d} con {model}...")
    out, issues, tok = _edit_flow(d, cfg, story, model)
    if issues:
        print(f"[yellow]Critic detecto: {issues}[/]")
    tag = model.replace(":", "_").replace("/", "_")
    dst = d / "edited" / f"st{story:03d}.{tag}.md"
    dst.write_text(out)
    log(slug, "editor", model, time.time() - t0, tok, True)
    print(f"[green]OK[/] {dst} ({time.time()-t0:.1f}s)")

@app.command()
def edit_all(slug: str, model: str = "", from_st: int = 1, to_st: int = 0):
    d, cfg = load_book(slug)
    if not model:
        model = cfg["model_cloud"]
    outline = json.loads((d / "02_outline.json").read_text())
    n = len(outline["stories"])
    if to_st == 0:
        to_st = n
    for i in range(from_st, to_st + 1):
        try:
            edit(slug, i, model)
        except Exception as e:
            print(f"[red]st{i:03d}[/]: {e}")

@app.command()
def process(slug: str, story: int, model: str = ""):
    """Pipeline automatico post-borrador: edit -> world-check -> reader+polish -> final/"""
    d, cfg = load_book(slug)
    if not model:
        model = cfg["model_cloud"]
    src = d / "stories" / f"st{story:03d}.md"
    if not src.exists():
        raise typer.Exit(f"No hay borrador st{story:03d}")
    outline = json.loads((d / "02_outline.json").read_text())
    spec = next((s for s in outline["stories"] if s["number"] == story), {})
    t0 = time.time()
    # Etapa 1: critic + edit
    print(f"[cyan]st{story:03d} 1/3 edicion...[/]")
    text, issues, tok = _edit_flow(d, cfg, story, model)
    tag = model.replace(":", "_").replace("/", "_")
    (d / "edited" / f"st{story:03d}.{tag}.md").write_text(text)
    # Etapa 2: consistencia de mundo
    print(f"[cyan]st{story:03d} 2/3 mundo...[/]")
    conflicts = run_world(cfg, spec, text, model)
    if conflicts:
        print(f"[yellow]  {len(conflicts)} conflictos[/]")
        issues2 = [f"{c.get('issue','')} -> fix: {c.get('fix','')}" for c in conflicts]
        text, _ = fix_with_editor(cfg, spec, text, issues2, model)
    # Etapa 3: lector + pulido (2 rondas)
    print(f"[cyan]st{story:03d} 3/3 lector+pulido...[/]")
    for r in range(2):
        report = run_reader(cfg, spec, text, model)
        problems = report.get("unanswered", []) + report.get("weird", [])
        if report.get("pass") and not problems:
            break
        print(f"[yellow]  ronda {r+1}: {len(problems)} problemas[/]")
        sys_p = (PROMPTS / "polisher.md").read_text()
        payload = json.dumps({"language": cfg["language"], "story_spec": spec,
                              "story_text": text, "reader_report": report})
        text, _ = call_ollama(model, sys_p, payload, temperature=0.4)
    final_issues = run_critic(cfg, spec, text, model)
    if final_issues:
        event("WARN", slug, story, "process_postcheck", model, detail=final_issues)
        text, _ = fix_with_editor(cfg, spec, text, final_issues, model)
    (d / "final").mkdir(exist_ok=True)
    (d / "final" / f"st{story:03d}.md").write_text(text)
    log(slug, "process", model, time.time() - t0, 0, True)
    print(f"[green]FINAL st{story:03d}[/] ({time.time()-t0:.0f}s)")

@app.command()
def process_all(slug: str, model: str = "", from_st: int = 1, to_st: int = 0):
    d, cfg = load_book(slug)
    outline = json.loads((d / "02_outline.json").read_text())
    n = len(outline["stories"])
    if to_st == 0:
        to_st = n
    for i in range(from_st, to_st + 1):
        try:
            process(slug, i, model)
        except Exception as e:
            print(f"[red]st{i:03d}[/]: {e}")

def _wcount(t):
    return len(t.split()) if t else 0

@app.command()
def report(slug: str):
    """Genera pipeline_report.md: documentacion punta a punta del libro."""
    d, cfg = load_book(slug)
    outline = json.loads((d / "02_outline.json").read_text())
    c = db()
    L = []
    L.append(f"# Reporte de pipeline — {cfg['title']}")
    L.append("")
    L.append(f"- Slug: `{slug}`")
    L.append(f"- Idioma: {cfg['language']} · Cuentos: {cfg['stories']} · Palabras objetivo c/u: {cfg['words_per_story']}")
    L.append(f"- Modelo borrador: `{cfg['model_local']}` · Modelo cloud: `{cfg['model_cloud']}` · Seed: {cfg.get('seed')}")
    L.append(f"- Generado: {datetime.now().isoformat(timespec='seconds')}")
    L.append("")

    # resumen de eventos por nivel
    ev = c.execute("SELECT level, COUNT(*) FROM events WHERE slug=? GROUP BY level", (slug,)).fetchall()
    L.append("## Resumen de eventos")
    L.append("")
    for lvl, cnt in ev:
        L.append(f"- {lvl}: {cnt}")
    warns = c.execute("SELECT ts,story,agent,detail FROM events WHERE slug=? AND level IN ('WARN','ERROR') ORDER BY id", (slug,)).fetchall()
    if warns:
        L.append("")
        L.append("### WARN/ERROR")
        for ts, story, agent, detail in warns:
            L.append(f"- `{ts}` st{story:03d} {agent}: {detail}")
    L.append("")

    # metricas por cuento (palabras en cada etapa)
    L.append("## Métricas por cuento")
    L.append("")
    L.append("| # | Protagonista | Borrador (w) | Editado (w) | FINAL (w) | Objetivo |")
    L.append("|---|---|---|---|---|---|")
    for st in outline["stories"]:
        n = st["number"]
        draft = (d/"stories"/f"st{n:03d}.md")
        drw = _wcount(draft.read_text()) if draft.exists() else 0
        eds = sorted((d/"edited").glob(f"st{n:03d}.*.md"))
        edw = _wcount(eds[0].read_text()) if eds else 0
        fin = (d/"final"/f"st{n:03d}.md")
        fiw = _wcount(fin.read_text()) if fin.exists() else 0
        L.append(f"| {n} | {st['protagonist']} {st['name']} | {drw} | {edw} | {fiw} | {cfg['words_per_story']} |")
    L.append("")

    # detalle por cuento
    for st in outline["stories"]:
        n = st["number"]
        L.append("---")
        L.append("")
        L.append(f"## Cuento {n}: {st['protagonist']} {st['name']}")
        L.append("")
        L.append("### Spec (ingredientes)")
        L.append("")
        L.append("```json")
        L.append(json.dumps(st, indent=2, ensure_ascii=False))
        L.append("```")
        L.append("")
        L.append("### Beats generados (estructura)")
        L.append("")
        for b in build_beats(st):
            L.append(f"- {b}")
        L.append("")
        # eventos del cuento
        rows = c.execute("SELECT ts,level,agent,model,elapsed,words_out,detail FROM events WHERE slug=? AND story=? ORDER BY id", (slug, n)).fetchall()
        if rows:
            L.append("### Eventos del pipeline")
            L.append("")
            L.append("| ts | nivel | agente | modelo | seg | out(w) | detalle |")
            L.append("|---|---|---|---|---|---|---|")
            for ts, lvl, agent, model, el, wo, det in rows:
                L.append(f"| {ts} | {lvl} | {agent} | {model} | {el} | {wo} | {str(det)[:80]} |")
            L.append("")
        # textos por etapa
        draft = (d/"stories"/f"st{n:03d}.md")
        if draft.exists():
            L.append("### Borrador (modelo local)")
            L.append("")
            L.append("```")
            L.append(draft.read_text().strip())
            L.append("```")
            L.append("")
        for ed in sorted((d/"edited").glob(f"st{n:03d}.*.md")):
            tagname = ed.name.split(".",1)[1].rsplit(".md",1)[0]
            L.append(f"### Editado — {tagname}")
            L.append("")
            L.append("```")
            L.append(ed.read_text().strip())
            L.append("```")
            L.append("")
        fin = (d/"final"/f"st{n:03d}.md")
        if fin.exists():
            L.append("### FINAL (va al EPUB)")
            L.append("")
            L.append("```")
            L.append(fin.read_text().strip())
            L.append("```")
            L.append("")
    c.close()
    out = d / "pipeline_report.md"
    out.write_text("\n".join(L))
    print(f"[green]Reporte[/] {out} ({len(L)} lineas)")
    return out

@app.command()
def build(slug: str, editor_tag: str = ""):
    d, cfg = load_book(slug)
    outline = json.loads((d / "02_outline.json").read_text())
    md = f"---\ntitle: {cfg['title']}\nlanguage: {cfg['language']}\n---\n\n"
    missing = []
    for st in outline["stories"]:
        n = st["number"]
        src = None
        fp = d / "final" / f"st{n:03d}.md"
        if fp.exists():
            src = fp
        if src is None and editor_tag:
            cand = sorted((d / "edited").glob(f"st{n:03d}.*{editor_tag}*.md"))
            if cand:
                src = cand[0]
        if src is None:
            pp = d / "stories" / f"st{n:03d}.md"
            if pp.exists():
                src = pp
                print(f"[yellow]st{n:03d}: BORRADOR SIN EDITAR al EPUB (sin final/ ni edited/ con tag)[/]")
        if src is None:
            missing.append(n); continue
        md += src.read_text().strip() + "\n\n"
    if missing:
        print(f"[yellow]Faltan: {missing}[/]")
    (d / "03_manuscript.md").write_text(md)
    out_epub = d / f"{slug}.epub"
    os.system(f"pandoc '{d}/03_manuscript.md' -o '{out_epub}' --toc --metadata title='{cfg['title']}' --metadata lang={cfg['language']}")
    print(f"[green]EPUB[/] {out_epub}")

@app.command()
def status(slug: str = ""):
    c = db()
    if slug:
        d, cfg = load_book(slug)
        print(cfg)
        done = len(list((d / "stories").glob("st*.md")))
        edited = len(list((d / "edited").glob("st*.md")))
        print(f"Escritas: {done} | Editadas: {edited}")
        for r in c.execute("SELECT agent,model,elapsed,tokens,ok FROM runs WHERE slug=? ORDER BY id DESC LIMIT 10", (slug,)).fetchall():
            print(r)
    else:
        for r in c.execute("SELECT slug,stage,created FROM books").fetchall():
            print(r)

if __name__ == "__main__":
    app()
