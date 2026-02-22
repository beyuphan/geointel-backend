import pytest
from unittest.mock import AsyncMock, patch
from services.orchestrator.main import orchestrator, custom_tool_node, AgentState
from langchain_core.messages import AIMessage, ToolMessage

@pytest.mark.asyncio
async def test_custom_tool_node_visual_extraction():
    """Tool yanıtlarının visual_data içine doğru ayıklandığını test eder."""
    
    # Mock Tool Çağrısı (Google'dan dönen bir mekan listesi gibi)
    mock_tool_call = {
        "name": "search_places_google",
        "args": {"query": "restoran", "session_id": "test_sid"},
        "id": "call_123"
    }
    
    # Başlangıç State'i
    state: AgentState = {
        "messages": [AIMessage(content="", tool_calls=[mock_tool_call])],
        "session_id": "test_sid",
        "visual_data": {"markers": [], "polyline": None},
        "intent": {},
        "retry_count": 0
    }

    # Tool sonucunu taklit et (Faz 2'de belirlediğimiz standart yapı)
    mock_result = [
        {"name": "Nalia", "lat": 41.02, "lon": 40.52, "source": "google"}
    ]

    # Orchestrator'ın get_tool_by_name metodunu mockla
    with patch.object(orchestrator, "get_tool_by_name") as mock_get_tool:
        mock_tool = AsyncMock()
        mock_tool.name = "search_places_google"
        mock_tool.ainvoke.return_value = mock_result
        mock_get_tool.return_value = mock_tool

        # Node'u çalıştır
        output = await custom_tool_node(state)

        # KONTROLLER
        assert "visual_data" in output
        assert len(output["visual_data"]["markers"]) == 1
        assert output["visual_data"]["markers"][0]["name"] == "Nalia"
        assert output["visual_data"]["markers"][0]["lat"] == 41.02
        assert isinstance(output["messages"][0], ToolMessage)