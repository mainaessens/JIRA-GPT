# jira_prompt_ui.py
import os, io
from contextlib import redirect_stdout
import streamlit as st

# --- Opción A: hidratar variables ANTES del import ---
def hydrate_env_from_secrets():
    """
    Carga variables desde el entorno si existen; si no, intenta st.secrets.
    No pisa valores ya presentes en os.environ.
    """
    KEYS = [
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT_KEY",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "JIRA_DRY_RUN",
        "JIRA_EPIC_NAME",  # opcional; si no usás épica por variable, dejalo vacío
    ]
    for k in KEYS:
        if not os.getenv(k):
            v = st.secrets.get(k, None)
            if v is not None:
                os.environ[k] = str(v)

hydrate_env_from_secrets()

# Importa tu pipeline existente (ya con envs disponibles)
try:
    from jira_auto_create import run_pipeline
except Exception as e:
    st.error(
        "No se pudo importar `jira_auto_create`. "
        "Verificá que las variables estén cargadas y que las dependencias coincidan "
        "(pydantic>=2, openai>=1). Detalle: {}".format(e)
    )
    st.stop()

# --- UI ---
st.set_page_config(page_title="Jira Prompt → Tasks", page_icon="✅", layout="centered")
st.title("Jira Prompt → Tasks")
st.caption("Pegá un brief y convertilo en tareas/subtareas en tu proyecto Jira.")

with st.sidebar:
    st.header("Configuración")
    st.write("Las credenciales se toman de variables de entorno / secrets.")

    # Mostrar (solo lectura) para confirmar (ya hidratadas)
    st.text_input("JIRA_BASE_URL", os.getenv("JIRA_BASE_URL",""), disabled=True)
    st.text_input("JIRA_PROJECT_KEY", os.getenv("JIRA_PROJECT_KEY",""), disabled=True)
    st.text_input("OPENAI_MODEL", os.getenv("OPENAI_MODEL","gpt-4o-mini"), disabled=True)

    # DRY-RUN por defecto según env (1 = simula)
    dry_default = (os.getenv("JIRA_DRY_RUN", "1") != "0")
    dry_run = st.checkbox("DRY-RUN (no crear issues)", value=dry_default)
    os.environ["JIRA_DRY_RUN"] = "1" if dry_run else "0"

st.subheader("Brief / Prompt")
example = """Ejemplo (con épica en el texto):
Epic: Gran Bazar Chino

Tareas a crear:
- Diseño 1
- Diseño 2
- Diseño 3
- Diseño 4
- Diseño 5
- Diseño 6
- Diseño 7
- Diseño 8
"""
text = st.text_area("Pegá aquí el texto (tareas y subtareas).", value=example, height=260)

c1, c2 = st.columns(2)
if c1.button("Analizar (DRY-RUN)"):
    if not text.strip():
        st.warning("Pegá algún texto primero.")
    else:
        os.environ["JIRA_DRY_RUN"] = "1"
        buf = io.StringIO()
        with redirect_stdout(buf):
            _ = run_pipeline(text, dry_run=True)
        st.subheader("Resultado del análisis")
        st.code(buf.getvalue(), language="text")

if c2.button("Crear en Jira"):
    if not text.strip():
        st.warning("Pegá algún texto primero.")
    else:
        buf = io.StringIO()
        with redirect_stdout(buf):
            keys = run_pipeline(text, dry_run=False)
        st.subheader("Log de ejecución")
        st.code(buf.getvalue(), language="text")

        base = os.getenv("JIRA_BASE_URL","").rstrip("/")
        if keys:
            st.success("Issues creadas:")
            for k in keys:
                st.markdown(f"- [{k}]({base}/browse/{k})")
        else:
            st.info("No se devolvieron claves creadas. Revisá el log por si hubo validaciones o DRY-RUN activo.")
