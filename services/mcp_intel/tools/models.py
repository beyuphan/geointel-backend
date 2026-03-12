from pydantic import BaseModel
from typing import Optional, List, Union

# --- TEMEL VERİ MODELLERİ ---

class FuelPrice(BaseModel):
    company: str
    gasoline: float
    diesel: float
    lpg: float
    district: str
    city: str
    last_updated: str = "Güncel"

class Pharmacy(BaseModel):
    name: str
    district: str
    address: str
    phone: str
    coordinates: Optional[str] = None

class Event(BaseModel):
    title: str
    venue: str
    date: str
    category: str = "Genel"
    link: Optional[str] = None
    source: str = "unknown"

class Match(BaseModel):
    match: str
    time: str
    stadium: str
    city: str
    warning: str

# --- GENEL DÖNÜŞ KAPSAYICISI (WRAPPER) ---
class IntelResponse(BaseModel):
    status: str = "success" # 'success', 'error'
    message: Optional[str] = None
    data: Union[List[FuelPrice], List[Pharmacy], List[Event], List[Match], dict] = []