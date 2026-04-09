"""
MIBA — Carbon Value & Severity Scoring Engine
Service: carbon-engine
Inputs: waste category, weight, verification status
Outputs: CO2e avoided, severity score, carbon credit value in INR
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../classification-api'))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="MIBA Carbon Engine",
    description="CO2e avoided calculation + severity scoring per IndicWaste category",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*", "https://miba-ui-340635219170.europe-west1.run.app"], allow_methods=["*"], allow_headers=["*"])

# ─── Carbon constants (IPCC AR6 WG3 Ch7 + Verra VM0046) ───────────────────
# Baseline: India mixed municipal waste landfill = 0.52 tCO2e/tonne
LANDFILL_BASELINE_TCO2E_PER_TONNE = 0.52

# Carbon credit market prices (INR) — updated quarterly
CARBON_CREDIT_PRICE_INR_PER_TONNE = float(os.getenv("CARBON_PRICE_INR", "2000"))  # ~$24 @ 83 INR/USD

# Registry: GOLD_STANDARD | VERRA_VCS (set at deployment)
CARBON_REGISTRY = os.getenv("CARBON_REGISTRY", "GOLD_STANDARD")

# IndicWaste CO2e avoided per tonne (kg) + severity (hardcoded from taxonomy, replicated here for independence)
CARBON_TABLE = {
    "W01": {"co2e_per_tonne": 550,  "severity": 5,  "name": "Organic / Food Waste"},
    "W02": {"co2e_per_tonne": 1850, "severity": 8,  "name": "Rigid Plastic"},
    "W03": {"co2e_per_tonne": 1200, "severity": 9,  "name": "Flexible Plastic / Film"},
    "W04": {"co2e_per_tonne": 2100, "severity": 6,  "name": "Metal (Ferrous)"},
    "W05": {"co2e_per_tonne": 9200, "severity": 7,  "name": "Metal (Non-ferrous)"},
    "W06": {"co2e_per_tonne": 900,  "severity": 4,  "name": "Paper / Cardboard"},
    "W07": {"co2e_per_tonne": 300,  "severity": 3,  "name": "Glass"},
    "W08": {"co2e_per_tonne": 4800, "severity": 10, "name": "E-waste"},
    "W09": {"co2e_per_tonne": 6200, "severity": 10, "name": "Hazardous / Chemical"},
    "W10": {"co2e_per_tonne": 1100, "severity": 6,  "name": "Textile"},
    "W11": {"co2e_per_tonne": 1400, "severity": 7,  "name": "Rubber / Tyres"},
    "W12": {"co2e_per_tonne": 180,  "severity": 3,  "name": "Construction & Demolition"},
    "W13": {"co2e_per_tonne": 3200, "severity": 10, "name": "Medical / Biomedical"},
    "W14": {"co2e_per_tonne": 680,  "severity": 8,  "name": "Sanitary / Hygiene"},
    "W15": {"co2e_per_tonne": 90,   "severity": 2,  "name": "Inert / Ash"},
    "W16": {"co2e_per_tonne": 420,  "severity": 5,  "name": "Mixed / Unclassified"},
}

# ─── Schemas ───────────────────────────────────────────────────────────────

class CarbonRequest(BaseModel):
    work_order_id: str
    category_code: str = Field(..., pattern=r"^W(0[1-9]|1[0-6])$")
    weight_kg: float = Field(..., gt=0, le=50000)
    verified: bool = True                      # Supervisor-verified = full credit; unverified = 50%
    location_city: Optional[str] = None

class CategoryCarbonRequest(BaseModel):
    """Multi-category request (for a full work order with mixed waste)"""
    work_order_id: str
    categories: list[dict]  # [{category_code, weight_kg}, ...]
    verified: bool = True

class CarbonResult(BaseModel):
    work_order_id: str
    category_code: str
    category_name: str
    weight_kg: float
    severity_score: int
    co2e_avoided_kg: float
    co2e_avoided_tonne: float
    carbon_credit_inr: float
    verification_multiplier: float
    registry: str
    methodology: str
    timestamp: str


# ─── Core calculation logic ────────────────────────────────────────────────

def calculate_carbon_value(
    category_code: str,
    weight_kg: float,
    verified: bool = True,
) -> dict:
    cat = CARBON_TABLE.get(category_code)
    if not cat:
        raise ValueError(f"Unknown category: {category_code}")

    # Verification multiplier: unverified gets 50% credit (conservative)
    verification_multiplier = 1.0 if verified else 0.5

    # CO2e avoided = (category_co2e_factor / 1000) × weight_kg × verification_multiplier
    co2e_avoided_kg = (cat["co2e_per_tonne"] / 1000) * weight_kg * verification_multiplier

    # Carbon credit value = (co2e_avoided_kg / 1000) × price_per_tonne_INR
    carbon_credit_inr = (co2e_avoided_kg / 1000) * CARBON_CREDIT_PRICE_INR_PER_TONNE

    # Severity multiplier for token weighting (passed to token engine)
    severity_multiplier = 1 + (cat["severity"] - 1) * 0.15  # 1.0 to 2.35

    return {
        "category_name": cat["name"],
        "severity_score": cat["severity"],
        "severity_multiplier": round(severity_multiplier, 3),
        "co2e_avoided_kg": round(co2e_avoided_kg, 4),
        "co2e_avoided_tonne": round(co2e_avoided_kg / 1000, 6),
        "carbon_credit_inr": round(carbon_credit_inr, 2),
        "verification_multiplier": verification_multiplier,
        "methodology": f"{CARBON_REGISTRY} VM0046 / IPCC AR6 WG3 Ch7",
        "registry": CARBON_REGISTRY,
    }


# ─── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "carbon-engine", "registry": CARBON_REGISTRY}


@app.post("/carbon/calculate", response_model=CarbonResult)
def calculate(req: CarbonRequest):
    logger.info("[carbon-engine] POST /carbon/calculate — wo=%s cat=%s weight=%.2fkg verified=%s",
                req.work_order_id, req.category_code, req.weight_kg, req.verified)
    try:
        result = calculate_carbon_value(req.category_code, req.weight_kg, req.verified)
    except ValueError as e:
        raise HTTPException(400, str(e))

    logger.info("[carbon-engine] Result — co2e=%.4fkg credit=₹%.2f",
                result['co2e_avoided_kg'], result['carbon_credit_inr'])
    return CarbonResult(
        work_order_id=req.work_order_id,
        category_code=req.category_code,
        timestamp=datetime.now(timezone.utc).isoformat(),
        weight_kg=req.weight_kg,
        **result,
    )


@app.post("/carbon/calculate/batch")
def calculate_batch(req: CategoryCarbonRequest):
    """Calculate carbon value for a full work order with mixed waste categories."""
    logger.info("[carbon-engine] POST /carbon/calculate/batch — wo=%s items=%d",
                req.work_order_id, len(req.categories))
    results = []
    total_co2e_kg = 0.0
    total_carbon_inr = 0.0

    for item in req.categories:
        try:
            r = calculate_carbon_value(item["category_code"], item["weight_kg"], req.verified)
            results.append({"category_code": item["category_code"], **r})
            total_co2e_kg += r["co2e_avoided_kg"]
            total_carbon_inr += r["carbon_credit_inr"]
        except ValueError as e:
            results.append({"category_code": item.get("category_code"), "error": str(e)})

    logger.info("[carbon-engine] Batch result — total_co2e=%.4fkg total=₹%.2f",
                total_co2e_kg, total_carbon_inr)
    return {
        "work_order_id": req.work_order_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "items": results,
        "totals": {
            "total_co2e_avoided_kg": round(total_co2e_kg, 4),
            "total_co2e_avoided_tonne": round(total_co2e_kg / 1000, 6),
            "total_carbon_credit_inr": round(total_carbon_inr, 2),
        },
        "registry": CARBON_REGISTRY,
    }


@app.get("/carbon/table")
def get_carbon_table():
    """Return the full IndicWaste carbon value reference table."""
    return {
        "registry": CARBON_REGISTRY,
        "baseline_landfill_tco2e_per_tonne": LANDFILL_BASELINE_TCO2E_PER_TONNE,
        "carbon_credit_price_inr_per_tonne": CARBON_CREDIT_PRICE_INR_PER_TONNE,
        "categories": [
            {
                "code": code,
                "name": v["name"],
                "co2e_avoided_per_tonne_kg": v["co2e_per_tonne"],
                "severity_score": v["severity"],
            }
            for code, v in CARBON_TABLE.items()
        ],
    }


@app.get("/carbon/severity/{category_code}")
def get_severity(category_code: str):
    cat = CARBON_TABLE.get(category_code.upper())
    if not cat:
        raise HTTPException(404, f"Category {category_code} not found")
    severity = cat["severity"]
    multiplier = 1 + (severity - 1) * 0.15
    return {
        "category_code": category_code.upper(),
        "category_name": cat["name"],
        "severity_score": severity,
        "severity_label": ["", "Minimal", "Very Low", "Low", "Low-Moderate", "Moderate",
                            "Moderate-High", "High", "Very High", "Critical", "Extreme"][severity],
        "token_multiplier": round(multiplier, 3),
    }
