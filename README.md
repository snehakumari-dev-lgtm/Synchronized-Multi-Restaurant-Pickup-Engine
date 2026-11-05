# FreshRoute

**Route-aware, just-in-time food preparation and delivery scheduling.**

[![CI](https://github.com/YOUR_USERNAME/freshroute/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/freshroute/actions)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The Problem

Traditional food delivery platforms instruct every restaurant in a multi-restaurant order to start cooking immediately after the order is placed. This causes two problems:

- **Restaurant A** finishes cooking in 15 minutes and the food sits cold for 25 minutes while the driver is still at Restaurant B.
- **The driver** arrives at Restaurant B and waits 10 minutes because it started cooking too late.

## The Innovation

FreshRoute back-calculates the optimal cooking start time for each restaurant:

```
cook_start = driver_arrival_at_restaurant − max_prep_minutes
```

Each restaurant begins cooking at exactly the right moment so food is ready precisely when the driver arrives — never early (cold), never late (driver waits).

---

## Worked Example

```
Order placed: 3:00 PM
```

| Restaurant | Prep Time | Stop | Travel from Prev | Driver Arrives | Cook Start | Food Ready |
|---|---|---|---|---|---|---|
| Restaurant B | 15 min | 1 | 0 min | 3:00 PM | **3:00 PM** | 3:15 PM |
| Restaurant A | 20 min | 2 | 20 min | 3:35 PM | **3:15 PM** | 3:35 PM ✓ |

**Customer ETA: 4:00 PM** (3:35 PM + 25 min to customer)

Restaurant A starts cooking 15 minutes after the order — not immediately — because the driver needs 20 minutes to travel from B to A after picking up B's food.

---

## Architecture

```
HTTP Request
     │
     ▼
┌─────────────────────────────────────┐
│  FastAPI Router  (app/routers/)     │  ← HTTP concerns only
│  POST /api/v1/schedule/             │
└────────────────┬────────────────────┘
                 │  converts Pydantic → dataclasses
                 ▼
┌─────────────────────────────────────┐
│  Scheduling Engine                  │  ← Pure algorithm, no HTTP
│  (app/services/scheduling_engine.py)│
│                                     │
│  1. Sort stops by stop_order        │
│  2. Compute driver arrival times    │
│  3. Back-calculate cook starts      │
│  4. Compute customer ETA            │
└─────────────────────────────────────┘
                 │  returns ScheduleResult
                 ▼
┌─────────────────────────────────────┐
│  Pydantic Response Schema           │  ← Serialisation, docs
│  (app/schemas/schedule.py)          │
└─────────────────────────────────────┘
```

The scheduling engine is intentionally I/O-free. It has no database connections, no HTTP calls, and no side effects. It can be tested, imported, and run in any context.

---

## Project Structure

```
freshroute/
├── app/
│   ├── main.py                          # FastAPI app, middleware, router registration
│   ├── routers/
│   │   └── schedule.py                  # HTTP endpoints
│   ├── schemas/
│   │   └── schedule.py                  # Pydantic request/response models
│   └── services/
│       └── scheduling_engine.py         # Core algorithm (pure Python)
├── tests/
│   └── test_scheduling.py               # 30+ tests across 3 layers
├── .github/
│   └── workflows/
│       └── ci.yml                       # GitHub Actions CI pipeline
├── Dockerfile                           # Production container
├── docker-compose.yml                   # Local development environment
├── requirements.txt                     # Pinned dependencies
└── pytest.ini                           # Test configuration
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/schedule/` | Calculate cook schedule for an order |
| `GET`  | `/api/v1/schedule/example` | Run the spec example (no body needed) |
| `GET`  | `/health` | Health check for load balancers |
| `GET`  | `/docs` | Interactive Swagger UI |
| `GET`  | `/redoc` | ReDoc documentation |

### Request Example

```bash
curl -X POST http://localhost:8000/api/v1/schedule/ \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "ORDER_001",
    "order_placed_at": "2099-06-01T15:00:00",
    "driver_start_minutes": 0,
    "restaurants": [
      {"id": "rest_b", "name": "Restaurant B", "prep_minutes": 15},
      {"id": "rest_a", "name": "Restaurant A", "prep_minutes": 20}
    ],
    "stops": [
      {"restaurant_id": "rest_b", "stop_order": 1, "travel_minutes_from_prev": 0},
      {"restaurant_id": "rest_a", "stop_order": 2, "travel_minutes_from_prev": 20}
    ],
    "travel_to_customer_minutes": 25
  }'
```

### Response Example

```json
{
  "order_id": "ORDER_001",
  "cook_schedules": [
    {
      "restaurant_id": "rest_b",
      "restaurant_name": "Restaurant B",
      "driver_arrives_at": "2099-06-01T15:00:00",
      "cook_start_at": "2099-06-01T15:00:00",
      "estimated_ready_at": "2099-06-01T15:15:00",
      "minutes_until_cook_start": 0.0,
      "is_start_immediate": false
    },
    {
      "restaurant_id": "rest_a",
      "restaurant_name": "Restaurant A",
      "driver_arrives_at": "2099-06-01T15:35:00",
      "cook_start_at": "2099-06-01T15:15:00",
      "estimated_ready_at": "2099-06-01T15:35:00",
      "minutes_until_cook_start": 15.0,
      "is_start_immediate": false
    }
  ],
  "customer_eta": "2099-06-01T16:00:00",
  "total_route_minutes": 60,
  "is_feasible": true,
  "warnings": []
}
```

---

## Getting Started

### Option 1: Docker (recommended)

```bash
git clone https://github.com/YOUR_USERNAME/freshroute.git
cd freshroute
docker compose up --build
```

Visit **http://localhost:8000/docs** for the interactive API.

### Option 2: Local Python

```bash
git clone https://github.com/YOUR_USERNAME/freshroute.git
cd freshroute

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Run the scheduling engine standalone

```bash
python app/services/scheduling_engine.py
```

---

## Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ -v --cov=app --cov-report=term-missing

# Run a specific layer
pytest tests/test_scheduling.py::TestSchedulingEngineSpec -v
pytest tests/test_scheduling.py::TestScheduleEndpoint -v
```

The test suite has three layers:

- **Engine unit tests** — algorithm correctness with no HTTP overhead
- **Schema tests** — Pydantic validation catches bad inputs before the engine
- **API integration tests** — full HTTP request/response cycle via `TestClient`

---

## Algorithm Design

The scheduling algorithm runs in O(n log n) time where n = number of restaurants. For the current constraint of n ≤ 3, this is effectively constant.

**Step 1** — Sort route stops by `stop_order` to establish pickup sequence.

**Step 2** — Compute driver arrival at each stop by accumulating travel times from order placement.

**Step 3** — Back-calculate each restaurant's cook start:
```
cook_start = driver_arrives_at − max_prep_minutes
```

**Step 4** — Advance the driver clock correctly:
```
driver_departs = max(driver_arrives_at, food_ready_at)
```
This handles both cases: driver waits for food, or food waits for driver.

**Step 5** — Customer ETA = departure from last stop + travel to customer.

**Edge case — clamping**: If the ideal cook start falls in the past (very short notice orders), the engine clamps it to `now`, marks `is_start_immediate=True`, and adds a warning. The order still proceeds — food arrives slightly less optimal, but not cold.

---

## Tech Stack

| Layer | Technology | Reason |
|---|---|---|
| Framework | FastAPI | Auto Swagger docs, Pydantic v2, async-ready |
| Validation | Pydantic v2 | Type safety, cross-field validators |
| Server | Uvicorn | ASGI, production-grade |
| Tests | pytest + httpx | TestClient for full HTTP integration tests |
| Container | Docker | Reproducible builds, Railway/AWS deploy |
| CI | GitHub Actions | Runs tests on every push |

---

## Roadmap

- [x] **Phase 1** — Scheduling engine (pure Python algorithm)
- [x] **Phase 2** — FastAPI wrapper, schemas, tests, Docker
- [ ] **Phase 3** — PostgreSQL + SQLAlchemy (persistent orders)
- [ ] **Phase 4** — Auth (JWT, roles: customer / restaurant / driver)
- [ ] **Phase 5** — Google Maps API integration (real travel times)
- [ ] **Phase 6** — WebSocket real-time tracking
- [ ] **Phase 7** — React frontend

---

## License

MIT — see [LICENSE](LICENSE).

---

*Built as a portfolio project demonstrating system design, scheduling algorithms, and production FastAPI patterns.*
