import os
import time
import json
import pandas as pd
import logging
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from supabase import create_client, Client
import requests
from dotenv import load_dotenv
import hashlib
import secrets

load_dotenv()

# ===== LOGGING CONFIGURATION =====
os.makedirs('logs', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/api_output.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase
SUPABASE_URL = "https://eevolwvokcfuepbiqyiy.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVldm9sd3Zva2NmdWVwYmlxeWl5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ1MDU3MDYsImV4cCI6MjA5MDA4MTcwNn0.PBrqlRXrT6ubV2HbO0KFBKUeSBRG8JII2MB_HzVlp-o"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===== ADMIN CREDENTIALS (Store in .env in production) =====
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = hashlib.sha256("admin123".encode()).hexdigest()  # Change in production!

# In-memory session store (use Redis/Database in production)
active_sessions = {}

def calculate_analytics(details):
    """Calculate PDI, risk band, and analytics from hearing dates"""
    raw_dates = []
    
    if details.get("hearings"):
        raw_dates.extend([h.get('date') or h.get('hearingDate') for h in details['hearings']])
    
    if details.get("historyOfCaseHearings"):
        raw_dates.extend([h.get('date') or h.get('hearingDate') for h in details['historyOfCaseHearings']])
    
    for date_key in ["filingDate", "firstHearingDate", "lastHearingDate", 
                     "nextHearingDate", "filing_date", "first_hearing_date", 
                     "last_hearing_date", "next_hearing_date"]:
        if details.get(date_key):
            raw_dates.append(details.get(date_key))
    
    valid_dates = list(set([d for d in raw_dates if d and str(d).strip() != ""]))
    
    if len(valid_dates) < 2:
        return {
            "total_hearings": len(valid_dates),
            "avg_gap_days": 0,
            "risk_band": "Low",
            "pdi_percent": 0,
            "risk_display": "Low (0%)"
        }
    
    try:
        dates = pd.Series(pd.to_datetime(valid_dates, errors='coerce')).dropna().sort_values()
        
        if len(dates) < 2:
            return {
                "total_hearings": len(valid_dates),
                "avg_gap_days": 0,
                "risk_band": "Low",
                "pdi_percent": 0,
                "risk_display": "Low (0%)"
            }
        
        gaps = dates.diff().dt.days.dropna()
        avg_gap = gaps.mean() if len(gaps) > 0 else 0
        
        if avg_gap < 30:
            risk = "High"
            pdi_percent = min(100, max(0, round(100 - (avg_gap * 3), 1)))
        elif avg_gap < 90:
            risk = "Medium"
            pdi_percent = min(100, max(0, round(100 - avg_gap, 1)))
        else:
            risk = "Low"
            pdi_percent = min(100, max(0, round(90 - (avg_gap / 10), 1)))
        
        try:
            filing_date = details.get("filingDate") or details.get("filing_date")
            if filing_date:
                filing = pd.to_datetime(filing_date, errors='coerce')
                today = pd.Timestamp.now()
                case_age_days = (today - filing).days
                
                if case_age_days > 0:
                    expected_hearings = max(1, case_age_days / 60)
                    actual_hearings = len(valid_dates)
                    pdi_percent = min(100, round((actual_hearings / expected_hearings) * 50, 1))
                    
                    if pdi_percent >= 70:
                        risk = "High"
                    elif pdi_percent >= 40:
                        risk = "Medium"
                    else:
                        risk = "Low"
        except Exception as e:
            logger.warning(f"PDI calculation fallback: {e}")
            pass
        
        return {
            "total_hearings": len(valid_dates),
            "avg_gap_days": round(avg_gap, 2),
            "risk_band": risk,
            "pdi_percent": pdi_percent,
            "risk_display": f"{risk} ({pdi_percent}%)"
        }
        
    except Exception as e:
        logger.error(f"Analytics calculation error: {e}")
        return {
            "total_hearings": len(valid_dates),
            "avg_gap_days": 0,
            "risk_band": "Low",
            "pdi_percent": 0,
            "risk_display": "Low (0%)"
        }

def get_case_age_bucket(filing_date):
    """Calculate case age bucket"""
    if not filing_date:
        return "Unknown"
    
    try:
        filing = pd.to_datetime(filing_date)
        today = pd.Timestamp.now()
        age_days = (today - filing).days
        age_years = age_days / 365.25
        
        if age_years < 1:
            return "0-1 year"
        elif age_years < 3:
            return "1-3 years"
        elif age_years < 5:
            return "3-5 years"
        else:
            return "5+ years"
    except:
        return "Unknown"

def get_top_delay_reason(hearings):
    """Extract top delay reason from hearing history"""
    if not hearings or len(hearings) == 0:
        return "No hearing data available"
    
    purpose_count = {}
    
    for hearing in hearings:
        purpose = (
            hearing.get('purposeOfListing') or 
            hearing.get('purpose') or 
            hearing.get('purposeOfHearing') or 
            hearing.get('businessOnDate') or 
            hearing.get('order') or 
            hearing.get('stage') or 
            'Unknown'
        )
        
        purpose = str(purpose).strip()
        if not purpose or purpose.lower() in ['na', 'n/a', 'none', 'unknown', '']:
            purpose = 'Unknown Purpose'
        
        categorized_purpose = categorize_delay_reason(purpose)
        purpose_count[categorized_purpose] = purpose_count.get(categorized_purpose, 0) + 1
    
    if not purpose_count:
        return "No delay data available"
    
    top_reason = max(purpose_count, key=purpose_count.get)
    count = purpose_count[top_reason]
    total = sum(purpose_count.values())
    percentage = round((count / total) * 100, 1)
    
    return f"{top_reason} ({percentage}%)"

def categorize_delay_reason(purpose):
    """Categorize raw purpose strings into meaningful delay reasons"""
    purpose_lower = purpose.lower()
    
    if any(term in purpose_lower for term in ['unready', 'not ready', 'absent', 'missing', 'unavailable', 'counsel', 'advocate', 'lawyer']):
        return 'Party/Advocate Unavailable'
    
    if any(term in purpose_lower for term in ['notice', 'summons', 'service', 'process']):
        return 'Notice/Summons Pending'
    
    if any(term in purpose_lower for term in ['stay', 'injunction', 'restraint', 'higher court']):
        return 'Stayed by Higher Court'
    
    if any(term in purpose_lower for term in ['evidence', 'exhibit', 'exh', 'document', 'affidavit', 'verification']):
        return 'Evidence/Document Pending'
    
    if any(term in purpose_lower for term in ['amount', 'deposit', 'payment', 'fee', 'cost', 'fine']):
        return 'Payment/Deposit Pending'
    
    if any(term in purpose_lower for term in ['order', 'judgment', 'decision', 'ruling', 'pronouncement']):
        return 'Order/Judgment Pending'
    
    if any(term in purpose_lower for term in ['filing', 'say', 'reply', 'response', 'written', 'statement']):
        return 'Filing Pending'
    
    if any(term in purpose_lower for term in ['appearance', 'present', 'presented', 'appear']):
        return 'Appearance Required'
    
    if any(term in purpose_lower for term in ['adjourn', 'adjourned', 'postpone', 'deferred']):
        return 'Adjournment Requested'
    
    if any(term in purpose_lower for term in ['steps', 'progress', 'proceeding', 'next step']):
        return 'Case Progression Steps'
    
    if any(term in purpose_lower for term in ['hearing', 'arguments', 'final hearing']):
        return 'Hearing in Progress'
    
    return purpose if len(purpose) <= 50 else purpose[:50] + '...'

def save_search_history(cnr, case_data, admin_id):
    """Save search to admin history in Supabase"""
    try:
        search_record = {
            "cnr_number": cnr,
            "case_number": case_data.get("caseNumber", "N/A"),
            "court_name": case_data.get("courtName", "Unknown"),
            "case_type": case_data.get("caseType", "N/A"),
            "filing_date": case_data.get("filingDate"),
            "status": case_data.get("caseStatus", "UNKNOWN"),
            "searched_by": admin_id,
            "searched_at": datetime.now().isoformat()
        }
        
        supabase.table("admin_search_history").insert(search_record).execute()
        logger.info(f"💾 Search history saved for CNR: {cnr}")
    except Exception as e:
        logger.error(f"❌ Failed to save search history: {e}")

@app.post("/api/auth/login")
async def admin_login(request: Request):
    """Admin login endpoint"""
    try:
        data = await request.json()
        username = data.get("username", "")
        password = data.get("password", "")
        
        if not username or not password:
            raise HTTPException(status_code=400, detail="Username and password required")
        
        # Verify credentials
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        if username != ADMIN_USERNAME or password_hash != ADMIN_PASSWORD_HASH:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Create session token
        session_token = secrets.token_urlsafe(32)
        active_sessions[session_token] = {
            "username": username,
            "created_at": datetime.now(),
            "expires_at": datetime.now().replace(hour=23, minute=59, second=59)
        }
        
        logger.info(f"✅ Admin login successful: {username}")
        
        return {
            "success": True,
            "message": "Login successful",
            "session_token": session_token,
            "username": username
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Login failed")

@app.post("/api/auth/logout")
async def admin_logout(request: Request):
    """Admin logout endpoint"""
    try:
        data = await request.json()
        session_token = data.get("session_token", "")
        
        if session_token in active_sessions:
            del active_sessions[session_token]
            logger.info("✅ Admin logout successful")
        
        return {"success": True, "message": "Logout successful"}
        
    except Exception as e:
        logger.error(f"Logout error: {e}")
        raise HTTPException(status_code=500, detail="Logout failed")

@app.get("/api/auth/verify")
async def verify_session(session_token: str):
    """Verify admin session"""
    if session_token in active_sessions:
        session = active_sessions[session_token]
        if datetime.now() < session["expires_at"]:
            return {
                "valid": True,
                "username": session["username"]
            }
        else:
            del active_sessions[session_token]
    
    return {"valid": False}
@app.get("/")
def home():
    return {"message": "Server is running 🚀"}

@app.get("/api/admin/search-history")
async def get_search_history(session_token: str, limit: int = 20):
    """Get admin's search history"""
    try:
        if session_token not in active_sessions:
            raise HTTPException(status_code=401, detail="Invalid session")
        
        response = supabase.table("admin_search_history")\
            .select("*")\
            .order("searched_at", desc=True)\
            .limit(limit)\
            .execute()
        
        return {
            "success": True,
            "history": response.data if response.data else []
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search history error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch search history")

@app.get("/api/case/{cnr}")
async def get_case_data(cnr: str, session_token: str = ""):
    """Fetch case data from eCourts API"""
    
    logger.info(f"\n{'='*60}")
    logger.info(f"🚨 API REQUEST FOR CNR: {cnr}")
    logger.info(f"{'='*60}")
    
    api_url = f"https://webapi.ecourtsindia.com/api/partner/case/{cnr}"
    headers = {
        "Authorization": "Bearer eci_live_uykayp4uhxaatljmj0tuzfp9cdl27nyj",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Connection": "keep-alive"
    }
    
    raw_data = None
    for attempt in range(2):
        try:
            response = requests.get(api_url, headers=headers, timeout=15)
            response.raise_for_status()
            raw_data = response.json()
            logger.info(f"✅ API Fetch Successful (Attempt {attempt + 1})")
            break
        except Exception as e:
            logger.error(f"❌ API Fetch Attempt {attempt + 1} failed: {e}")
            time.sleep(2)
            continue
    
    if not raw_data:
        logger.error("❌ Failed to connect to eCourts API after 2 attempts")
        raise HTTPException(status_code=504, detail="Failed to connect to eCourts API.")
    
    details = raw_data.get("data", {}).get("courtCaseData", {})
    
    if not details:
        logger.error(f"❌ CNR not found: {cnr}")
        raise HTTPException(status_code=404, detail="CNR not found in eCourts database.")
    
    logger.info(f"\n📋 RAW API PAYLOAD FOR {cnr}")
    logger.info(json.dumps(details, indent=2, ensure_ascii=False))
    
    stats = calculate_analytics(details)
    case_age_bucket = get_case_age_bucket(details.get("filingDate"))
    hearings = details.get("historyOfCaseHearings") or details.get("hearings") or []
    top_delay_reason = get_top_delay_reason(hearings)
    
    cleaned_data = {
        "cnr_number": cnr,
        "case_number": details.get("caseNumber"),
        "court_name": details.get("courtName", "Unknown"),
        "case_type": details.get("caseType", "N/A"),
        "filing_date": details.get("filingDate"),
        "hearing_date": details.get("lastHearingDate"),
        "next_hearing_date": details.get("nextHearingDate"),
        "purpose_of_hearing": details.get("purposeOfHearing"),
        "is_adjourned": details.get("caseStatus") == "PENDING" and bool(details.get("nextHearingDate")),
        "total_hearings": stats["total_hearings"],
        "avg_gap_days": stats["avg_gap_days"],
        "risk_band": stats["risk_band"],
        "pdi_percent": stats["pdi_percent"]
    }
    
    try:
        supabase.table("case_records").delete().eq("cnr_number", cnr).execute()
        supabase.table("case_records").insert(cleaned_data).execute()
        logger.info(f"💾 Successfully saved to Supabase: {cnr}")
    except Exception as e:
        logger.error(f"❌ Storage Error: {e}")
    
    # Save to admin search history if logged in
    if session_token and session_token in active_sessions:
        admin_id = active_sessions[session_token]["username"]
        save_search_history(cnr, details, admin_id)
    
    response_data = {
        "source": "eCourtsIndia Live",
        "data": cleaned_data,
        "raw_data": details,
        "case_age_bucket": case_age_bucket,
        "top_delay_reason": top_delay_reason,
        **details
    }
    
    logger.info(f"✅ Response sent successfully for CNR: {cnr}")
    logger.info(f"{'='*60}\n")
    
    return response_data

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    logger.info("Health check requested")
    return {"status": "ok", "message": "Backend is running"}

if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting JuriSight Backend Server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
