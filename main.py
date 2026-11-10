"""
================================================================================
FreshRoute — FastAPI Application Entry Point
================================================================================

File    : app/main.py
Purpose : Creates and configures the FastAPI application instance.
          Registers routers, middleware, and startup/shutdown events.

Run locally:
    uvicorn app.main:app --reload --port 8000

Then visit:
    http://localhost:8000/docs      ← Interactive Swagger UI
    http://localhost:8000/redoc     ← ReDoc documentation
    http://localhost:8000/health    ← Health check
================================================================================
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import schedule
from app.schemas.schedule import HealthResponse

# ==============================================================================
# APPLICATION FACTORY
# ==============================================================================

app = FastAPI(
    title="FreshRoute API",
    description="""
## Route-aware, just-in-time food preparation scheduling.

FreshRoute synchronises restaurant cooking times with driver arrival times
so that food is **always fresh** when the driver arrives for pickup.

### Core Innovation

Instead of every restaurant starting to cook immediately when an order is placed,
the engine back-calculates the optimal cook-start time for each restaurant:

```
cook_start = driver_arrival_at_restaurant − max_prep_minutes
```

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/schedule/` | Calculate schedule for an order |
| `GET`  | `/api/v1/schedule/example` | Worked example from the spec |
| `GET`  | `/health` | Health check for load balancers |

### Maximum Restaurants Per Order
The current implementation supports **up to 3 restaurants** per order.
Beyond 3, the TSP search space grows factorially and requires heuristics —
that is a planned v2 feature.
    """,
    version="1.0.0",
    contact={
        "name": "FreshRoute",
        "url": "https://github.com/YOUR_USERNAME/freshroute",
    },
    license_info={
        "name": "MIT",
    },
    # Swagger UI available at /docs only in non-production environments.
    # Set docs_url=None and redoc_url=None to disable in production.
    docs_url="/docs",
    redoc_url="/redoc",
)

# ==============================================================================
# MIDDLEWARE
# ==============================================================================

# CORS: allows browsers to call this API from any origin.
# In production, replace allow_origins=["*"] with your specific frontend domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================================================================
# ROUTERS
# ==============================================================================

# Register the scheduling router.
# All its endpoints are prefixed with /api/v1/schedule (defined in the router).
app.include_router(schedule.router)

# Future routers slot in here cleanly:
#   app.include_router(auth.router)
#   app.include_router(restaurants.router)
#   app.include_router(orders.router)
#   app.include_router(drivers.router)


# ==============================================================================
# HEALTH CHECK
# ==============================================================================

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Health check",
    description="Used by Docker, Railway, and load balancers to verify the service is alive.",
)
def health_check() -> HealthResponse:
    """Returns 200 OK with service status. No authentication required."""
    return HealthResponse(
        status="ok",
        version="1.0.0",
        engine="SchedulingEngine ready",
    )


# ==============================================================================
# ROOT REDIRECT
# ==============================================================================

@app.get("/", include_in_schema=False)
def root():
    """Redirects the root path to the interactive API docs."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")
