import streamlit as st

st.set_page_config(page_title="Paid Media Pacer", page_icon="📊", layout="wide")

pg = st.navigation([
    st.Page("pages/1_Paid_Media_Pacer.py", title="Paid Media Pacer"),
    st.Page("pages/2_Paid_to_Pipeline.py", title="Paid to Pipeline"),
])
pg.run()
