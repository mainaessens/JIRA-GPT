import os, json, sys, time, re
from typing import List, Optional, Tuple, Dict
from dateutil.parser import parse as parse_date
import requests
from pydantic import BaseModel, Field, field_validator

# =========================
#  Configuración (por ENV)
# =========================
JIRA_BASE_URL    = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL       = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN   = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY"]
OPENAI_API_KEY   = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Cambiá a False para crear issues reales
DEFAULT_DRY_RUN  = True if os.getenv("JIRA_DRY_RUN", "1") != "0" else False

# =========================
#  Modelos (entrada LLM)
# =========================
class SubtaskIn(BaseModel):
    title: str
    description: Optional[str] = ""
    due_date: Optional[str] = None
    assignee: Optional[str] = None

    @field_validator("due_date", mode="before")
    def norm_date(cls, v):
        if not v:
            return None
        try:
            return parse_date(str(v), dayfirst=True).date().isoformat()
        except Exception:
            return None

class TaskIn(BaseModel):
    title: str
    description: Optional[str] = ""
    labels: List[str] = Field(default_factory=list)
    priority: Optional[str] = "Medium"  # Highest/High/Medium/Low/Lowest
    due_date: Optional[str] = None
    assignee: Optional[str] = None
    subtasks: List[SubtaskIn] = Field(default_factory=list)

    @field_validator("due_date", mode="before")
    def norm_date(cls, v):
        if not v:
            return None
        try:
            return parse_date(str(v), dayfirst=True).date().isoformat()
        except Exception:
            return None

class TaskBundle(BaseModel):
    tasks: List[TaskIn]

# =========================
#  HTTP helpers
# =========================
def jira_headers() -> Dict[str, str]:
    return {"Accept": "application/json", "Content-Type": "application/json"}

def jira_auth() -> Tuple[str, str]:
    return (JIRA_EMAIL, JIRA_API_TOKEN)

def http_get(url: str):
    r = requests.get(url, headers=jira_headers(), auth=jira_auth())
    if r.status_code >= 400:
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text}")
    return r.json()

def http_post(url: str, payload: dict):
    r = requests.post(url, headers=jira_headers(), auth=jira_auth(), data=json.dumps(payload))
    if r.status_code >= 400:
        raise RuntimeError(f"POST {url} -> {r.status_code}: {r.text}")
    return r.json()

# =========================
#  Jira metadata helpers
# =========================
def get_priority_map() -> Dict[str, str]:
    out = {}
    for p in http_get(f"{JIRA_BASE_URL}/rest/api/3/priority"):
        out[p["name"].lower()] = p["id"]
    alias = {}
    for k, v in out.items():
        alias[k.split()[0]] = v
    out.update(alias)
    return out

def get_issue_type_ids(project_key: str) -> Dict[str, str]:
    data = http_get(f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta?projectKeys={project_key}&expand=projects.issuetypes.fields")
    its = data["projects"][0]["issuetypes"]
    out = {}
    wanted = {
        "task": ["Task","Tarea"],
        "subtask": ["Sub-task","Subtarea","Sub-task (Jira)"]
    }
    for key, names in wanted.items():
        for it in its:
            if it["name"] in names:
                out[key] = it["id"]
                break
    if "task" not in out and its:
        out["task"] = its[0]["id"]
    if "subtask" not in out:
        for it in its:
            if "sub" in it["name"].lower():
                out["subtask"] = it["id"]; break
    return out


def find_account_id(query: Optional[str]) -> Optional[str]:
    """Busca el accountId por nombre o email."""
    if not query:
        return None
    url = f"{JIRA_BASE_URL}/rest/api/3/user/search?query={requests.utils.quote(query)}"
    users = http_get(url)
    return users[0]["accountId"] if users else None


def get_epic_link_field_key() -> Optional[str]:
    """
    Busca el fieldKey del campo 'Epic Link' (company-managed).
    Si no existe, probablemente sea un proyecto team-managed y usaremos parent.
    """
    fields = http_get(f"{JIRA_BASE_URL}/rest/api/3/field")
    for f in fields:
        name = f.get("name", "").strip().lower()
        if name == "epic link":
            return f.get("id")  # ej: customfield_10014
    return None

def find_epic_issue(project_key: str, epic_name: str) -> Optional[Dict[str, str]]:
    """
    Devuelve {'key': 'CS-1', 'id': '10001', 'summary': 'El Gran Bazar Chino'} si existe.
    """
    jql = f'project="{project_key}" AND issuetype=Epic AND summary~"{epic_name}" ORDER BY created DESC'
    payload = {"jql": jql, "maxResults": 10, "fields": ["summary"]}
    res = http_post(f"{JIRA_BASE_URL}/rest/api/3/search", payload)
    issues = res.get("issues", [])
    if not issues:
        return None
    # Prefer exact match de summary
    for it in issues:
        if it["fields"].get("summary", "").strip().lower() == epic_name.strip().lower():
            return {"key": it["key"], "id": it["id"], "summary": it["fields"]["summary"]}
    # Si no hay exacta, devolvemos la primera
    it = issues[0]
    return {"key": it["key"], "id": it["id"], "summary": it["fields"]["summary"]}

# =========================
#  ADF
# =========================
def to_adf_description(title: str, description: str, labels: List[str], due_date: Optional[str], assignee: Optional[str]) -> dict:
    content = []
    if description:
        content.append({"type":"paragraph","content":[{"type":"text","text":description}]})
    meta_items = []
    if labels:
        meta_items.append(f"Etiquetas: {', '.join(labels)}")
    if due_date:
        meta_items.append(f"Vence: {due_date}")
    if assignee:
        meta_items.append(f"Asignado: {assignee}")
    if meta_items:
        content.append({"type":"paragraph","content":[{"type":"text","text":" · ".join(meta_items)}]})
    if not content:
        content = [{"type":"paragraph","content":[{"type":"text","text":""}]}]
    return {"type":"doc","version":1,"content":content}

# =========================
#  LLM: estructurar texto
# =========================
def llm_structurize_tasks(free_text: str) -> TaskBundle:
    import os, json
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no está seteada en el entorno/secrets de Streamlit.")
    client = OpenAI(api_key=api_key)

    system = (
        "Sos un asistente que convierte un texto desordenado en un plan de tareas para Jira. "
        "Devolvés SOLO JSON válido según el esquema pedido. Prioridades válidas: Highest, High, Medium, Low, Lowest. "
        "Normalizá fechas al formato YYYY-MM-DD."
    )
    user = f"""Convertí el siguiente texto en una lista de tareas con subtareas.
REQUISITOS POR TAREA: ... (tu prompt actual) ...
TEXTO:
\"\"\"{free_text}\"\"\""""

    # 1) Intento Chat Completions
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=0.1,
            response_format={"type":"json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return TaskBundle(**data)

    except Exception as e_chat:
        # 2) Fallback a Responses API
        try:
            resp2 = client.responses.create(
                model=model, input=f"{system}\n\n{user}",
                temperature=0.1, response_format={"type":"json_object"}
            )
            content_text = getattr(resp2, "output_text", None)
            if not content_text:
                parts = []
                for out in getattr(resp2, "output", []):
                    for c in getattr(out, "content", []):
                        if getattr(c, "type", "") == "output_text":
                            parts.append(getattr(c, "text", ""))
                content_text = "".join(parts)
            data = json.loads(content_text)
            return TaskBundle(**data)
        except Exception as e_resp:
            raise RuntimeError(
                f"OpenAI falló (¿key/modelo?). ChatCompletions: {e_chat} | Responses: {e_resp}"
            )

def detect_epic_name(text: str) -> Optional[str]:
    m = re.search(r"(?im)^\s*epic\s*:\s*(.+)$", text)
    if m:
        return m.group(1).strip()
    return None

# =========================
#  Creación en Jira
# =========================
def create_issue(task: TaskIn, ids: Dict[str,str], epic_ctx: Optional[Dict[str,str]]=None, epic_field_key: Optional[str]=None, parent_key: Optional[str]=None) -> str:
    priority_map = get_priority_map()
    prio_id = priority_map.get((task.priority or "").lower()) if task.priority else None
    assignee_id = find_account_id(task.assignee)

    fields = {
        "project": {"key": JIRA_PROJECT_KEY},
        "summary": task.title[:255],
        "description": to_adf_description(task.title, task.description or "", task.labels, task.due_date, task.assignee),
        "labels": task.labels[:10],
    }

    if parent_key:
        # Subtarea
        fields["issuetype"] = {"id": ids["subtask"]}
        fields["parent"] = {"key": parent_key}
    else:
        # Tarea principal
        fields["issuetype"] = {"id": ids["task"]}
        if prio_id:
            fields["priority"] = {"id": prio_id}
        # Epic link (solo para tareas principales)
        if epic_ctx:
            if epic_field_key:
                # Company-managed: Epic Link custom field
                fields[epic_field_key] = epic_ctx["key"]
            else:
                # Team-managed: usar parent = epic.id (si lo permite)
                fields["parent"] = {"id": epic_ctx["id"]}

    if task.due_date:
        fields["duedate"] = task.due_date
    if assignee_id:
        fields["assignee"] = {"id": assignee_id}

    payload = {"fields": fields}
    res = http_post(f"{JIRA_BASE_URL}/rest/api/3/issue", payload)
    return res["key"]

def run_pipeline(raw_text: str, dry_run: bool=DEFAULT_DRY_RUN) -> List[str]:
    if dry_run:
        print("→ Modo DRY-RUN (no crea issues)")
    else:
        print("→ Modo CREACIÓN REAL")

    # 0) Resolver épica (si el texto la incluye)
    epic_name = detect_epic_name(raw_text)
    epic_ctx = None
    epic_field_key = None
    if epic_name:
        print(f"→ Buscando épica: {epic_name!r}")
        epic = find_epic_issue(JIRA_PROJECT_KEY, epic_name)
        if epic:
            epic_ctx = epic  # {'key','id','summary'}
            epic_field_key = get_epic_link_field_key()
            print(f"   ✓ Epic encontrada: {epic_ctx['key']} ({epic_ctx['summary']}) | campo Epic Link: {epic_field_key or 'no disponible (team-managed)'}")
        else:
            print("   ⚠ No se encontró la épica. Se crearán tareas sin Epic Link.")

    # 1) Estructurar con LLM
    print("→ Analizando texto con LLM…")
    bundle = llm_structurize_tasks(raw_text)

    # 2) Descubrir issue types válidos
    ids = get_issue_type_ids(JIRA_PROJECT_KEY)
    if "task" not in ids or "subtask" not in ids:
        print("⚠ No se encontraron IDs claros de Task/Sub-task. "
              "Se usará el primero disponible para Task y se omitirán subtareas si falta 'subtask'.")

    created_keys = []
    # 3) Crear issues
    for t in bundle.tasks:
        if dry_run:
            print(f"[DRY] Task: {t.title} | due={t.due_date} | prio={t.priority} | labels={t.labels} | assignee={t.assignee} | epic={epic_name or '—'}")
            for st in t.subtasks:
                print(f"   [DRY] Subtask: {st.title} | due={st.due_date} | assignee={st.assignee}")
            continue

        parent_key = create_issue(t, ids, epic_ctx=epic_ctx, epic_field_key=epic_field_key, parent_key=None)
        print(f"✓ Creada {parent_key}: {t.title}")
        created_keys.append(parent_key)

        if "subtask" in ids:
            for st in t.subtasks:
                st_task = TaskIn(
                    title=st.title,
                    description=st.description or "",
                    labels=t.labels,
                    priority=None,
                    due_date=st.due_date,
                    assignee=st.assignee or t.assignee,
                    subtasks=[]
                )
                sub_key = create_issue(st_task, ids, epic_ctx=None, epic_field_key=None, parent_key=parent_key)
                print(f"   ↳ Subtask {sub_key}: {st.title}")
                time.sleep(0.2)
        else:
            if t.subtasks:
                print("⚠ Subtareas ignoradas (no se encontró issuetype de Sub-task en tu proyecto).")
    return created_keys

# =========================
#  CLI
# =========================
def main():
    if sys.stdin.isatty():
        print("Pegá el texto de tareas y presioná Enter, luego Ctrl+Z (Windows) o Ctrl+D (Unix).")
    raw = sys.stdin.read().strip()
    if not raw:
        print("No recibí texto.\nEjemplo de uso:\n  type tareas.txt | python jira_auto_create.py")
        sys.exit(1)

    try:
        keys = run_pipeline(raw_text=raw, dry_run=DEFAULT_DRY_RUN)
        if DEFAULT_DRY_RUN:
            print("\n🧪 DRY-RUN completado. Si el resultado está OK, ejecutá con JIRA_DRY_RUN=0 para crear:")
            print("  Windows PowerShell:")
            print('    $env:JIRA_DRY_RUN="0"; type .\\tareas.txt | py .\\jira_auto_create.py')
        else:
            print("\n🎉 Issues creadas:", ", ".join(keys) if keys else "(ninguna)")
    except Exception as e:
        print("\n❌ Error en la ejecución:")
        print(str(e))
        sys.exit(2)

if __name__ == "__main__":
    main()
