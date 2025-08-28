# jira_prompt_ui.py
import os, io
from contextlib import redirect_stdout
import streamlit as st

# Importa tu pipeline existente
from jira_auto_create import run_pipeline

st.set_page_config(page_title="Jira Prompt → Tasks", page_icon="✅", layout="centered")
st.title("Jira Prompt → Tasks")
st.caption("Pegá un brief y convertilo en tareas/subtareas en tu proyecto Jira.")

with st.sidebar:
    st.header("Configuración")
    st.write("Las credenciales se toman de variables de entorno.")
    # Mostrar (solo lectura) para confirmar
    st.text_input("JIRA_BASE_URL", os.getenv("JIRA_BASE_URL",""), disabled=True)
    st.text_input("JIRA_PROJECT_KEY", os.getenv("JIRA_PROJECT_KEY",""), disabled=True)
    st.text_input("OPENAI_MODEL", os.getenv("OPENAI_MODEL","gpt-4o-mini"), disabled=True)

    # Épica como variable (opcional)
    epic = st.text_input("Épica (JIRA_EPIC_NAME)", value=os.getenv("JIRA_EPIC_NAME",""), placeholder="Ej: El Gran Bazar Chino")
    if epic:
        os.environ["JIRA_EPIC_NAME"] = epic

    dry_default = True
    dry_run = st.checkbox("DRY-RUN (no crear issues)", value=dry_default)
    os.environ["JIRA_DRY_RUN"] = "1" if dry_run else "0"

st.subheader("Brief / Prompt")
example = """Historias a crear bajo la épica variable:
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
