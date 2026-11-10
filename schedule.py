"""
================================================================================
FreshRoute — Scheduling Router
================================================================================

File    : app/routers/schedule.py
Purpose : Defines all HTTP endpoints related to schedule calculation.

RESPONSIBILITY BOUNDARY
-----------------------
This file knows about:
  • HTTP methods (POST, GET)
  • Request / response shapes (Pydantic schemas)
  • HTTP status codes and error responses
  • Calling the service layer

This file does NOT know about:
  • Scheduling arithmetic (that lives in scheduling_engine.py)
  • Database queries (future concern)
  • Authentication (future middleware concern)

ENDPOINTS
---------
  POST /api/v1/schedule        — calculate a new schedule
  GET  /api/v1/schedule/example — return a worked example (great for demos)
================================================================================
"""

from datetime import datetime
from fastapi import APIRouter, HTTPException, status

from app.schemas.schedule import ScheduleRequest, ScheduleResponse, CookScheduleOut
from app.services.scheduling_engine import (
    SchedulingEngine,
    Restaurant,
    RouteStop,
)

# ------------------------------------------------------------------------------
# Router setup
#
# prefix="/api/v1/schedule" means every route defined here automatically gets
# that prefix. This gives us clean API versioning from day one — when v2 ships,
# we add a new router at /api/v2/schedule without touching this file.
# ------------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/v1/schedule",
    tags=["Scheduling"],  # groups endpoints under "Scheduling" in Swagger UI
)

# One shared engine instance.
# SchedulingEngine is stateless (no instance variables), so sharing it is safe.
# In a database-backed version this would be injected as a dependency.
_engine = SchedulingEngine()


# ==============================================================================
# POST /api/v1/schedule
# ==============================================================================

@router.post(
    "/",
    response_model=ScheduleResponse,
    status_code=status.HTTP_200_OK,
    summary="Calculate cook-start schedule for a multi-restaurant order",
    description="""
Runs the just-in-time scheduling algorithm.

**Core formula:**
```
cook_start = driver_arrival_at_restaurant − max_prep_minutes
```

The engine back-calculates the optimal moment for each restaurant to start
cooking so that food is ready exactly when the driver arrives — minimising
cooling time and driver wait time.

**Business rules enforced:**
- Maximum 3 restaurants per order
- Every stop must reference a restaurant in the list
- Restaurant IDs must be unique
- stop_order values must be unique
""",
    responses={
        200: {"description": "Schedule calculated successfully."},
        422: {"description": "Validation error — check request body against the schema."},
        500: {"description": "Unexpected engine error."},
    },
)
def calculate_schedule(request: ScheduleRequest) -> ScheduleResponse:
    """
    Accepts a scheduling request, converts it to engine types, runs the engine,
    and converts the result back to API response types.

    This function is intentionally thin — all logic is in the engine.
    """

    # ------------------------------------------------------------------
    # Convert Pydantic input models → engine dataclasses
    #
    # WHY CONVERT?
    # The engine is framework-agnostic. It must not import Pydantic or FastAPI.
    # Keeping the conversion here means the engine can be used in a CLI,
    # a Celery task, a test, or any other context without modification.
    # ------------------------------------------------------------------

    engine_restaurants = [
        Restaurant(
            id=r.id,
            name=r.name,
            prep_minutes=r.prep_minutes,
        )
        for r in request.restaurants
    ]

    engine_stops = [
        RouteStop(
            restaurant_id=s.restaurant_id,
            stop_order=s.stop_order,
            travel_minutes_from_prev=s.travel_minutes_from_prev,
        )
        for s in request.stops
    ]

    # ------------------------------------------------------------------
    # Run the engine
    #
    # We wrap in try/except to translate engine ValueErrors into HTTP 422
    # responses with clear messages. HTTP 422 means "request was well-formed
    # JSON but semantically invalid" — more accurate than 400 here.
    # ------------------------------------------------------------------

    try:
        result = _engine.calculate(
            order_placed_at=request.order_placed_at,
            driver_start_minutes=request.driver_start_minutes,
            stops=engine_stops,
            restaurants=engine_restaurants,
            travel_to_customer_minutes=request.travel_to_customer_minutes,
            order_id=request.order_id,
        )
    except ValueError as exc:
        # The engine raises ValueError for invalid inputs that pass Pydantic
        # validation but fail business logic (e.g. mismatched stop/restaurant).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except Exception as exc:
        # Catch-all for unexpected errors — log in production, surface message.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Scheduling engine encountered an unexpected error: {str(exc)}",
        )

    # ------------------------------------------------------------------
    # Convert engine output → Pydantic response models
    # ------------------------------------------------------------------

    cook_schedules_out = [
        CookScheduleOut(
            restaurant_id=s.restaurant_id,
            restaurant_name=s.restaurant_name,
            driver_arrives_at=s.driver_arrives_at,
            cook_start_at=s.cook_start_at,
            estimated_ready_at=s.estimated_ready_at,
            minutes_until_cook_start=s.minutes_until_cook_start,
            is_start_immediate=s.is_start_immediate,
        )
        for s in result.cook_schedules
    ]

    return ScheduleResponse(
        order_id=result.order_id,
        cook_schedules=cook_schedules_out,
        customer_eta=result.customer_eta,
        total_route_minutes=result.total_route_minutes,
        is_feasible=result.is_feasible,
        warnings=result.warnings,
    )


# ==============================================================================
# GET /api/v1/schedule/example
# ==============================================================================

@router.get(
    "/example",
    response_model=ScheduleResponse,
    status_code=status.HTTP_200_OK,
    summary="Returns the worked example from the project specification",
    description="""
Runs the canonical spec example without requiring a request body.

**Order placed now. Route:**
- Restaurant B (15 min prep) → Stop 1, driver starts here
- Restaurant A (20 min prep) → Stop 2, 20 min drive from B
- Customer → 25 min drive from A

Use this endpoint to verify the API is working and to demonstrate the
just-in-time scheduling behaviour in demos.
""",
)
def get_example_schedule() -> ScheduleResponse:
    """
    Hard-wired spec example. Order placed at current time so cook-start
    countdowns are always meaningful when you hit this endpoint.
    """

    now = datetime.now()

    result = _engine.calculate(
        order_placed_at=now,
        driver_start_minutes=0,
        restaurants=[
            Restaurant(id="rest_b", name="Restaurant B", prep_minutes=15),
            Restaurant(id="rest_a", name="Restaurant A", prep_minutes=20),
        ],
        stops=[
            RouteStop(restaurant_id="rest_b", stop_order=1, travel_minutes_from_prev=0),
            RouteStop(restaurant_id="rest_a", stop_order=2, travel_minutes_from_prev=20),
        ],
        travel_to_customer_minutes=25,
        order_id="EXAMPLE_ORDER",
    )

    cook_schedules_out = [
        CookScheduleOut(
            restaurant_id=s.restaurant_id,
            restaurant_name=s.restaurant_name,
            driver_arrives_at=s.driver_arrives_at,
            cook_start_at=s.cook_start_at,
            estimated_ready_at=s.estimated_ready_at,
            minutes_until_cook_start=s.minutes_until_cook_start,
            is_start_immediate=s.is_start_immediate,
        )
        for s in result.cook_schedules
    ]

    return ScheduleResponse(
        order_id=result.order_id,
        cook_schedules=cook_schedules_out,
        customer_eta=result.customer_eta,
        total_route_minutes=result.total_route_minutes,
        is_feasible=result.is_feasible,
        warnings=result.warnings,
    )
