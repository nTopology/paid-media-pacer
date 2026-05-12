import streamlit as st
from datetime import date

st.set_page_config(
    page_title="Paid Media Pacer",
    layout="wide",
)

st.title("Paid Media Pacer")
st.caption(f"Today is {date.today().strftime('%B %d, %Y')}.")

st.markdown(
    """
    This is the budget pacing dashboard for nTop's paid media spend.

    It's currently empty. Over the next iterations we'll add:

    1. Month-to-date pacing tile (actual vs. expected, variance)
    2. Daily spend chart with the expected-pace line overlaid
    3. Channel breakdowns for LinkedIn, Google, Microsoft, Reddit
    4. Strategic vs. High Velocity split
    5. Recommended daily spend to land on budget
    """
)

if st.button("Confirm Streamlit is working"):
    st.success("It's working. You're ready for step 2.")