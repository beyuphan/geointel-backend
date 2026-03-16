import json
import asyncio
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph import StateGraph, END
from logger import log
from core.graph import intent_node, agent_node, custom_tool_node, should_continue, AgentState
from core.mcp_client import orchestrator

router = APIRouter()

class ChatRequest(BaseModel):
    session_id: str = "default_session"
    message: str
    current_lat: Optional[float] = None  # Anlık konum (frontend'den gelir)
    current_lon: Optional[float] = None  # Anlık konum (frontend'den gelir)
    fcm_token: Optional[str] = None      # Push bildirim token'i (Firebase)

@router.post("/chat")
async def chat_endpoint(request: ChatRequest):
    log.info(f"📩 [Request] Session: {request.session_id} | Msg: {request.message[:30]}...")

    # Anlık konumu Redis'e kaydet (her istek güncelleyebilir)
    if orchestrator.redis_client and request.current_lat and request.current_lon:
        loc_str = f"{request.current_lat},{request.current_lon}"
        orchestrator.redis_client.set(f"loc:{request.session_id}", loc_str, ex=3600)  # 1 saat geçerli
        log.info(f"📍 [CurrentLoc] Kaydedildi → {loc_str}")

    # FCM token'i kaydet (varsa)
    if orchestrator.redis_client and request.fcm_token:
        orchestrator.redis_client.set(f"fcm:{request.session_id}", request.fcm_token, ex=86400 * 30)
    
    # 1. REDIS'TEN GEÇMİŞİ OKU (Botun hafızası)
    chat_key = f"chat:{request.session_id}"
    history_messages = []
    
    if orchestrator.redis_client:
        raw_history = orchestrator.redis_client.lrange(chat_key, 0, -1)
        for item in raw_history:
            try:
                # Redis client bytes dönebilir, string'e çevirdiğimizden emin olalım
                if isinstance(item, bytes):
                    item = item.decode('utf-8')
                    
                msg_data = json.loads(item)
                if msg_data["role"] == "user":
                    history_messages.append(HumanMessage(content=msg_data["content"]))
                elif msg_data["role"] == "assistant":
                    history_messages.append(AIMessage(content=msg_data["content"]))
            except Exception as e:
                log.error(f"Geçmiş okunurken hata: {e}")

    # 2. YENİ MESAJI GEÇMİŞİN SONUNA EKLE
    history_messages.append(HumanMessage(content=request.message))
    
    # Grafiği (Workflow) Kur
    workflow = StateGraph(AgentState)
    workflow.add_node("classifier", intent_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", custom_tool_node)
    
    workflow.set_entry_point("classifier")
    workflow.add_edge("classifier", "agent")
    workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "agent": "agent", END: END})
    workflow.add_edge("tools", "agent")
    
    executor = workflow.compile()
    
    # 3. YALNIZCA YENİ MESAJI DEĞİL, TÜM GEÇMİŞİ GRAFİĞE VER
    final_state = await executor.ainvoke({
        "messages": history_messages,
        "intent": {}, 
        "retry_count": 0, 
        "session_id": request.session_id, 
        "visual_data": {"markers": [], "polyline": None, "geojson_layers": []}
    })
    
    raw_content = final_state["messages"][-1].content
    
    # AI yanıtı liste (block) olarak dönerse, içindeki metinleri birleştirip tek bir string yapıyoruz:
    if isinstance(raw_content, list):
        response_text = "".join([block.get("text", "") for block in raw_content if isinstance(block, dict) and "text" in block])
    else:
        response_text = str(raw_content)
    
    # Yanıtı Redis'e kaydet (Gelecek sohbetler için)
    if orchestrator.redis_client:
        orchestrator.redis_client.rpush(chat_key, json.dumps({"role": "user", "content": request.message}))
        orchestrator.redis_client.rpush(chat_key, json.dumps({"role": "assistant", "content": response_text}))
        orchestrator.redis_client.ltrim(chat_key, -20, -1)  # Context limitini korumak için son 20 mesajı tut
        orchestrator.redis_client.expire(chat_key, 86400)   # 24 saat sonra sohbet sıfırlanır

    # Route datasını güvenli çekme
    route_data = None
    if orchestrator.redis_client:
        route_val = orchestrator.redis_client.get(f"route:{request.session_id}")
        if route_val:
            route_data = route_val.decode('utf-8') if isinstance(route_val, bytes) else route_val

    return {
        "response": response_text, 
        "visual_data": final_state.get("visual_data"), 
        "route_polyline": route_data
    }