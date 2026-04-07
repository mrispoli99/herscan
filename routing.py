import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import google.generativeai as genai
import os
from dotenv import load_dotenv
from data_loader import get_cleveland_zips

# --- 1. SETUP & SECURE KEY LOADING ---
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if api_key:
    genai.configure(api_key=api_key)
else:
    st.error("🚨 GEMINI_API_KEY not found. Please check your .env file.")

st.set_page_config(page_title="HerScan 90-Day Executive Forecast", layout="wide")

# --- 2. DATA LOAD ---
df = get_cleveland_zips()

# --- 3. UI HEADER ---
st.title("🚐 HerScan 90-Day Operational & Financial Forecast")
st.markdown("### Strategic Planning for Cleveland, Lorain, Lake, & Medina Counties")

# --- 4. SIDEBAR: LOGISTICS & FINANCIALS ---
st.sidebar.header("📊 Travel & Fixed Costs")
hotel_room_avg = st.sidebar.number_input("Hotel Room Rental / Day ($)", value=150)
gas_per_day = st.sidebar.number_input("Est. Gas & Mileage / Day ($)", value=45)
tech_daily_wage = st.sidebar.number_input("Tech Daily Wage ($)", value=450)
rad_fee = st.sidebar.number_input("Radiology Fee per Scan ($)", value=50)

st.sidebar.divider()
st.sidebar.header("📣 Marketing & Retention")
cac_per_new = st.sidebar.slider("Marketing Spend per New Lead ($)", 10, 100, 40)
rescreen_loyalty = st.sidebar.slider("Rescreening Loyalty (%)", 10, 80, 20) / 100

# --- 5. THE FORECAST ENGINE ---
if st.button("🚀 Run 90-Day Financial Simulation"):
    with st.spinner("Analyzing market data and generating visualizations..."):
        
        # Select top 50 events for the 90-day window
        forecast_df = df.sort_values(by="Income", ascending=False).head(50).copy()
        
#        --- UPDATED MATH LOGIC WITH 25-SCAN CAP ---
        # 1. New Leads: Based on 0.3% of target demographic
        forecast_df['New_Leads'] = (forecast_df['Women_45plus'] * 0.003).round(0)
        
        # 2. Rescreening scalar
        rescreen_value = round(30 * rescreen_loyalty)
        forecast_df['Rescreening'] = rescreen_value
        
        # 3. Total Attendance with Hard Cap of 25
        # We use np.clip to ensure the solo-tech isn't overloaded
        forecast_df['Total_Attendees'] = (forecast_df['New_Leads'] + forecast_df['Rescreening']).clip(upper=25)

        # 4. Financial Calculations (Revenue now capped at $6,250/day)
        forecast_df['Gross_Rev'] = forecast_df['Total_Attendees'] * 250
        forecast_df['Marketing_Spend'] = forecast_df['New_Leads'] * cac_per_new
        forecast_df['Variable_Costs'] = (forecast_df['Total_Attendees'] * rad_fee) + forecast_df['Marketing_Spend']
        
        # Daily Fixed Costs (Hotel + Tech + Gas)
        daily_overhead = hotel_room_avg + gas_per_day + tech_daily_wage
        forecast_df['Fixed_Costs'] = daily_overhead
        
        forecast_df['Net_Profit'] = forecast_df['Gross_Rev'] - (forecast_df['Variable_Costs'] + forecast_df['Fixed_Costs'])

        # --- BURNOUT ANALYSIS ---
        # 25 scans @ 20 mins + 2 hrs overhead = 10.3 hour day
        forecast_df['Workday_Hours'] = (forecast_df['Total_Attendees'] * 20 / 60) + 2

        # --- ROW 1: KPI METRICS ---
        q1_net = forecast_df['Net_Profit'].sum()
        q1_rev = forecast_df['Gross_Rev'].sum()
        avg_margin = (q1_net / q1_rev) * 100 if q1_rev > 0 else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("90-Day Net Profit", f"${q1_net:,.0f}")
        c2.metric("Gross Revenue", f"${q1_rev:,.0f}")
        c3.metric("Avg. Profit Margin", f"{avg_margin:.1f}%")
        c4.metric("Total Scans Planned", int(forecast_df['Total_Attendees'].sum()))

        st.divider()

        # --- ROW 2: VISUALIZATIONS ---
        col_left, col_right = st.columns([1, 1])

        with col_left:
            st.subheader("📍 Regional Profitability Map")
            fig_map = px.scatter_mapbox(
                forecast_df, 
                lat="Lat", lon="Lon", 
                size="Total_Attendees", 
                color="Net_Profit",
                color_continuous_scale=px.colors.sequential.Viridis,
                hover_name="City",
                hover_data=["Zip", "County", "Net_Profit"],
                zoom=8,
                mapbox_style="carto-positron",
                height=550
            )
            st.plotly_chart(fig_map, use_container_width=True)

        with col_right:
            st.subheader("💰 Top 15 Profit Centers")
            chart_df = forecast_df.sort_values(by="Net_Profit", ascending=False).head(15)
            fig_profit = px.bar(
                chart_df, 
                x="Net_Profit", 
                y="City", 
                orientation='h',
                color="Net_Profit",
                color_continuous_scale='Greens',
                labels={"Net_Profit": "Net Profit ($)", "City": "Market Hub"},
                height=550
            )
            fig_profit.update_layout(yaxis={'categoryorder':'total ascending'})
            st.plotly_chart(fig_profit, use_container_width=True)

        # --- ROW 3: AI ANALYSIS ---
        st.divider()
        if api_key:
            st.subheader("🤖 AI Logistics Optimization (Generating Below)")
            try:
                model = genai.GenerativeModel('gemini-2.5-flash')
                # Passing a summary to avoid token limits
                summary_context = forecast_df[['City', 'Total_Attendees', 'Net_Profit']].head(20).to_json()
                
                prompt = f"""
                Act as a Logistics Director for HerScan Cleveland. 
                Data: {summary_context}
                
                STRICT OPERATIONAL RULES:
                1. ONE LOCATION PER DAY: The tech stays at one hotel/site for the entire shift. No midday travel.
                2. CAP: Maximum 25 scans per day.
                3. CLUSTERING: Group cities by County (e.g., 'Lorain Week') to minimize the solo tech's total weekly driving.
                
                TASK:
                - Pick the top 5 'High-Profit' days for next week.
                - Explain the 'County Cluster' strategy (e.g., staying in Medina for 2 days to save on gas).
                - Identify which city has the highest 'Rescreening' buffer to protect against marketing dips.
                """
                response = model.generate_content(prompt)
                st.markdown(response.text)
            except Exception as e:
                st.warning(f"Could not reach Gemini: {e}")
        
        # --- DATA TABLE ---
        st.divider()
        st.subheader("📋 90-Day Operational Ledger")
        st.dataframe(forecast_df[['City', 'Zip', 'County', 'New_Leads', 'Rescreening', 'Total_Attendees', 'Net_Profit']], use_container_width=True)

else:
    st.info("Click the button above to generate the 90-day forecast based on your current financial settings.")