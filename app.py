import streamlit as st

st.set_page_config(page_title="Paid Media Pacer", page_icon="📊", layout="wide")

pg = st.navigation([
    st.Page("pages/1_Paid_Media_Pacer.py", title="Paid Media Pacer"),
    st.Page("pages/2_Paid_to_Pipeline.py", title="Paid to Pipeline"),
    st.Page("pages/3_Event_Attribution.py", title="Event & Webinar Attribution"),
    st.Page("pages/4_Web_Traffic.py", title="Web Traffic"),
])
pg.run()
