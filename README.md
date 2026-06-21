# GeoIntel MCP – Location-Based Decision Support Platform

## Overview

GeoIntel is a production-grade, intelligent decision support system that analyzes location-based data and generates optimized routes using MCP (Model Context Protocol) architecture. The system integrates real-time weather data, geocoding services, and intelligent routing algorithms to provide proactive insights for location-dependent scenarios.

## Architecture

The backend is built with a **modular service-oriented architecture**:

- **Orchestrator**: Central coordination engine managing trip candidates and routing decisions
- **MCP Services**: Weather, intel, and city-specific MCP servers providing specialized tools
- **Database Layer**: Profile management, trip history, and analytics storage
- **External Integrations**: HERE Maps API (routing/geocoding), Weather APIs, and data enrichment services

## Key Features

- **Intelligent Route Planning**: Multi-criteria optimization considering weather, cost, and time
- **Real-time Weather Integration**: Dynamic cost adjustment based on weather conditions
- **Geocoding & Reverse Geocoding**: HERE Maps + Nominatim fallback for location resolution
- **MCP Framework**: Extensible tool-based system for adding new capabilities
- **Profile Management**: User preferences, history tracking, and personalized insights
- **Data Aggregation**: Multi-source data integration with conflict resolution

## Tech Stack

- **Backend**: Python 3.10+
- **Framework**: FastAPI / Custom async handlers
- **Database**: PostgreSQL (PostGIS for spatial queries) / MySQL
- **APIs**: HERE Maps, Weather APIs
- **Architecture**: MCP (Model Context Protocol)
- **Async**: asyncpg, httpx
- **Tools**: Docker, Git

## Local Setup

### Requirements

- Python 3.10+
- PostgreSQL (or MySQL)
- HERE Maps API key
- OpenWeather API key (optional)

### Installation

```bash
# Clone repository
git clone https://github.com/beyuphan/geointel-backend.git
cd geointel-backend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys and database connection

# Initialize database
python scripts/init_db.py

# Run development server
python -m services.orchestrator.api.main
```

### Configuration

```env
# .env
DATABASE_URL=postgresql://user:password@localhost/geointel
HERE_API_KEY=your_here_api_key
WEATHER_API_KEY=your_weather_api_key
DEBUG=true
```

## Project Structure

```
services/
├── orchestrator/          # Main orchestration engine
│   ├── api/              # REST API endpoints
│   ├── core/             # Trip planning, strategy
│   └── profile_manager.py
├── mcp_city/             # City-specific MCP server
└── mcp_intel/            # Intel/data MCP server

scripts/
├── init_db.py            # Database initialization
└── test_api.py

requirements.txt
.env.example
```

## API Endpoints

### Trip Planning

```bash
POST /api/trips/candidates
GET /api/trips/{trip_id}
GET /api/history
```

### Geocoding

```bash
GET /api/geocode?location=Rize
GET /api/reverse-geocode?lat=40.5&lon=40.5
```

## Development

### Running Tests

```bash
pytest tests/ -v
```

### Code Quality

```bash
black . --check
flake8 .
```

## Deployment

Deployable on Railway, Heroku, or custom VPS with proper environment configuration.

## Future Enhancements

- Connection pooling for database optimization
- Caching layer (Redis) for frequently accessed routes
- Machine learning for predictive analysis
- Real-time WebSocket updates for trip tracking

## License

MIT

## Contact

📧 eyuphan546@gmail.com | 🔗 [GitHub](https://github.com/beyuphan)
