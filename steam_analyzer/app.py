import streamlit as st

from steam_analyzer.config import load_settings

settings = load_settings()

st.title("Steam Review Sentiment Analysis")
st.caption(f"App ID {settings.steam_app_id} · model {settings.ollama_model}")
st.info("v0 scaffold: update catalog, selection and Run analysis are not built yet.")
