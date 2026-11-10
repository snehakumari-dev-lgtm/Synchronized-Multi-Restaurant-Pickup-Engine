"""
================================================================================
FreshRoute — Test Suite
================================================================================

File    : tests/test_scheduling.py
Purpose : Tests for the scheduling engine, Pydantic schemas, and API endpoints.

Test categories
---------------
1. Engine unit tests   — pure algorithm correctness, no HTTP
2. Schema unit tests   — Pydantic validation rules
3. API integration tests — full HTTP request/response cycle via TestClient

Run all tests:
    pytest tests/ -v

Run with coverage:
    pytest tests/ -v --cov=app --cov-report=term-missing

WHY THREE LAYERS OF TESTS?
--------------------------
Engine tests verify the algorithm in isolation. If these fail, the bug is
in the scheduling logic — no HTTP, no Pydantic, no noise.

Schema tests verify that bad inputs are rejected before reaching the engine.
If these fail, invalid data could silently reach the algorithm.

API tests verify the full request/response contract. If these fail, the
router layer has a bug even though the engine and schemas are correct.

This separation means when a test fails, you immediately know WHERE the bug is.
================================================================================
"""

import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

from app.main import app
from app.services.scheduling_engine import (
    SchedulingEngine,
    Restaurant,
    RouteStop,
)
from app.schemas.schedule import ScheduleRequest, RestaurantIn, RouteStopIn


# ==============================================================================
# FIXTURES
# ==============================================================================

@pytest.fixture
def engine():
    """A fresh SchedulingEngine instance for each test."""
    return SchedulingEngine()


@pytest.fixture
def client():
    """FastAPI TestClient — makes real HTTP requests without a running server."""
    return TestClient(app)


@pytest.fixture
def future_order_time():
    """
    An order time far enough in the future that no cook starts get clamped.
    Using 'now + 2 hours' ensures the engine sees all cook starts as future.
    """
    return datetime.now() + timedelta(hours=2)


@pytest.fixture
def spec_restaurants():
    """The exact restaurants from the project specification."""
    return [
        Restaurant(id="rest_b", name="Restaurant B", prep_minutes=15),
        Restaurant(id="rest_a", name="Restaurant A", prep_minutes=20),
    ]


@pytest.fixture
def spec_stops():
    """The exact route stops from the project specification."""
    return [
        RouteStop(restaurant_id="rest_b", stop_order=1, travel_minutes_from_prev=0),
        RouteStop(restaurant_id="rest_a", stop_order=2, travel_minutes_from_prev=20),
    ]


# ==============================================================================
# LAYER 1 — ENGINE UNIT TESTS
# ==============================================================================

class TestSchedulingEngineSpec:
    """
    Tests that reproduce the canonical example from the project specification.

    Order placed at T (2 hours in future so no clamping occurs).
    Restaurant B: 15 min prep, Stop 1, driver starts here (0 travel min).
    Restaurant A: 20 min prep, Stop 2, 20 min drive from B.
    Customer: 25 min drive from A.

    With a future order time, no cook start is clamped, so:
      B cook start = T - 15 min  (driver arrives T, prep 15, start T-15 — not in the past)
      B food ready = T           (perfect sync with driver arrival)
      A cook start = T + 0 min   (driver departs B at T, travels 20, arrives T+20, prep 20 → start T)
      A food ready = T + 20 min  (perfect sync with driver arrival at T+20)
      Customer ETA = T + 45 min  (depart A at T+20, travel 25)
      Total route  = 45 minutes

    The original spec shows 60 minutes because it assumed the driver waits
    15 minutes at Restaurant B (clamped cook start). With a future order and
    adequate lead time, there is no wait — the engine achieves perfect sync.
    """

    def test_two_restaurant_total_time(self, engine, future_order_time, spec_restaurants, spec_stops):
        """Total route should be 45 minutes when no clamping occurs."""
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=spec_stops,
            restaurants=spec_restaurants,
            travel_to_customer_minutes=25,
            order_id="TEST_SPEC",
        )
        assert result.total_route_minutes == 45

    def test_two_restaurant_returns_two_schedules(self, engine, future_order_time, spec_restaurants, spec_stops):
        """Engine must return exactly one CookSchedule per restaurant."""
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=spec_stops,
            restaurants=spec_restaurants,
            travel_to_customer_minutes=25,
        )
        assert len(result.cook_schedules) == 2

    def test_schedules_in_stop_order(self, engine, future_order_time, spec_restaurants, spec_stops):
        """Cook schedules must be returned in stop_order sequence (B first, A second)."""
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=spec_stops,
            restaurants=spec_restaurants,
            travel_to_customer_minutes=25,
        )
        assert result.cook_schedules[0].restaurant_id == "rest_b"
        assert result.cook_schedules[1].restaurant_id == "rest_a"

    def test_restaurant_a_cook_start_is_at_order_time(self, engine, future_order_time, spec_restaurants, spec_stops):
        """
        Restaurant A should start cooking at order placement time (T+0).

        Derivation (no clamping, future order):
          Driver at B from T, food ready at T (B cook_start = T-15, ready = T).
          Driver departs B at T, travels 20 min → arrives A at T+20.
          A prep = 20 min → cook_start = T+20-20 = T.
        """
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=spec_stops,
            restaurants=spec_restaurants,
            travel_to_customer_minutes=25,
        )
        sched_a = result.cook_schedules[1]
        expected_start = future_order_time  # T+0
        diff = abs((sched_a.cook_start_at - expected_start).total_seconds())
        assert diff < 2, f"Expected cook start at T+0, got {sched_a.cook_start_at}"

    def test_restaurant_a_food_ready_matches_driver_arrival(self, engine, future_order_time, spec_restaurants, spec_stops):
        """
        Perfect sync: Restaurant A's food should be ready exactly when driver arrives.
        This is the core invariant of the just-in-time scheduling innovation.
        """
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=spec_stops,
            restaurants=spec_restaurants,
            travel_to_customer_minutes=25,
        )
        sched_a = result.cook_schedules[1]
        diff = abs((sched_a.estimated_ready_at - sched_a.driver_arrives_at).total_seconds())
        assert diff < 2, (
            f"Food should be ready when driver arrives. "
            f"Ready: {sched_a.estimated_ready_at}, Arrives: {sched_a.driver_arrives_at}"
        )

    def test_customer_eta_is_correct(self, engine, future_order_time, spec_restaurants, spec_stops):
        """Customer ETA should be exactly 45 minutes after order placement (no driver wait at B)."""
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=spec_stops,
            restaurants=spec_restaurants,
            travel_to_customer_minutes=25,
        )
        expected_eta = future_order_time + timedelta(minutes=45)
        diff = abs((result.customer_eta - expected_eta).total_seconds())
        assert diff < 2

    def test_is_feasible_true_for_future_order(self, engine, future_order_time, spec_restaurants, spec_stops):
        """A future order with adequate lead time should be flagged as feasible."""
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=spec_stops,
            restaurants=spec_restaurants,
            travel_to_customer_minutes=25,
        )
        assert result.is_feasible is True
        assert result.warnings == []

    def test_order_id_echoed_in_result(self, engine, future_order_time, spec_restaurants, spec_stops):
        """The order_id passed in must be echoed back in the result."""
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=spec_stops,
            restaurants=spec_restaurants,
            travel_to_customer_minutes=25,
            order_id="ECHO_TEST",
        )
        assert result.order_id == "ECHO_TEST"


class TestSchedulingEngineThreeRestaurants:
    """Tests for the full 3-restaurant constraint."""

    def test_three_restaurant_schedule_length(self, engine, future_order_time):
        restaurants = [
            Restaurant(id="pizza", name="Pizza Palace", prep_minutes=18),
            Restaurant(id="sushi", name="Sushi Garden", prep_minutes=12),
            Restaurant(id="tacos", name="Taco Town",    prep_minutes=8),
        ]
        stops = [
            RouteStop(restaurant_id="pizza", stop_order=1, travel_minutes_from_prev=5),
            RouteStop(restaurant_id="sushi", stop_order=2, travel_minutes_from_prev=10),
            RouteStop(restaurant_id="tacos", stop_order=3, travel_minutes_from_prev=7),
        ]
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=stops,
            restaurants=restaurants,
            travel_to_customer_minutes=12,
        )
        assert len(result.cook_schedules) == 3

    def test_three_restaurant_stop_order_preserved(self, engine, future_order_time):
        """Schedules must be returned in stop_order, regardless of input order."""
        restaurants = [
            Restaurant(id="c", name="C", prep_minutes=10),
            Restaurant(id="a", name="A", prep_minutes=10),
            Restaurant(id="b", name="B", prep_minutes=10),
        ]
        # Stops passed in reverse order — engine must sort them
        stops = [
            RouteStop(restaurant_id="c", stop_order=3, travel_minutes_from_prev=5),
            RouteStop(restaurant_id="a", stop_order=1, travel_minutes_from_prev=5),
            RouteStop(restaurant_id="b", stop_order=2, travel_minutes_from_prev=5),
        ]
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=stops,
            restaurants=restaurants,
            travel_to_customer_minutes=5,
        )
        ids = [s.restaurant_id for s in result.cook_schedules]
        assert ids == ["a", "b", "c"]


class TestSchedulingEngineDriverDelay:
    """Tests that driver_start_minutes correctly delays the entire timeline."""

    def test_driver_delay_shifts_all_arrivals(self, engine, future_order_time):
        """
        Adding 10 minutes of driver start delay should shift all driver arrival
        times by exactly 10 minutes compared to 0 delay.
        """
        restaurants = [
            Restaurant(id="r1", name="R1", prep_minutes=15),
            Restaurant(id="r2", name="R2", prep_minutes=15),
        ]
        stops = [
            RouteStop(restaurant_id="r1", stop_order=1, travel_minutes_from_prev=5),
            RouteStop(restaurant_id="r2", stop_order=2, travel_minutes_from_prev=10),
        ]

        result_no_delay = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=stops,
            restaurants=restaurants,
            travel_to_customer_minutes=10,
        )
        result_with_delay = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=10,
            stops=stops,
            restaurants=restaurants,
            travel_to_customer_minutes=10,
        )

        for no_delay, with_delay in zip(
            result_no_delay.cook_schedules,
            result_with_delay.cook_schedules
        ):
            diff = (with_delay.driver_arrives_at - no_delay.driver_arrives_at).total_seconds()
            assert abs(diff - 600) < 2  # 600 seconds = 10 minutes


class TestSchedulingEngineEdgeCases:
    """Tests for boundary conditions and edge cases."""

    def test_single_restaurant(self, engine, future_order_time):
        """Engine should handle a single-restaurant order correctly.

        travel=10 → arrives T+10, cook_start=T-10 (not clamped, future order),
        ready=T+10 (perfect sync), travel 15 to customer → ETA = T+25, total = 25 min.
        """
        result = engine.calculate(
            order_placed_at=future_order_time,
            driver_start_minutes=0,
            stops=[RouteStop(restaurant_id="r1", stop_order=1, travel_minutes_from_prev=10)],
            restaurants=[Restaurant(id="r1", name="Solo", prep_minutes=20)],
            travel_to_customer_minutes=15,
        )
        assert len(result.cook_schedules) == 1
        assert result.total_route_minutes == 25

    def test_clamping_produces_warning(self, engine):
        """
        When cook_start is in the past, engine must clamp and add a warning.
        Using order_placed_at = now means stop 1 with 20 min prep will
        get an ideal start of (now - 20 min) which is in the past.
        """
        result = engine.calculate(
            order_placed_at=datetime.now(),
            driver_start_minutes=0,
            stops=[RouteStop(restaurant_id="r1", stop_order=1, travel_minutes_from_prev=0)],
            restaurants=[Restaurant(id="r1", name="Instant", prep_minutes=20)],
            travel_to_customer_minutes=5,
        )
        assert result.is_feasible is False
        assert len(result.warnings) >= 1
        assert "r1" not in result.warnings[0] or "Instant" in result.warnings[0]

    def test_clamped_start_is_immediate(self, engine):
        """is_start_immediate must be True for a clamped stop."""
        result = engine.calculate(
            order_placed_at=datetime.now(),
            driver_start_minutes=0,
            stops=[RouteStop(restaurant_id="r1", stop_order=1, travel_minutes_from_prev=0)],
            restaurants=[Restaurant(id="r1", name="Fast", prep_minutes=20)],
            travel_to_customer_minutes=5,
        )
        assert result.cook_schedules[0].is_start_immediate is True

    def test_four_restaurants_raises(self, engine, future_order_time):
        """Engine must reject orders with more than 3 restaurants."""
        restaurants = [Restaurant(id=f"r{i}", name=f"R{i}", prep_minutes=10) for i in range(4)]
        stops = [RouteStop(restaurant_id=f"r{i}", stop_order=i+1, travel_minutes_from_prev=5) for i in range(4)]
        with pytest.raises(ValueError, match="maximum of 3"):
            engine.calculate(
                order_placed_at=future_order_time,
                driver_start_minutes=0,
                stops=stops,
                restaurants=restaurants,
                travel_to_customer_minutes=10,
            )

    def test_empty_restaurants_raises(self, engine, future_order_time):
        """Engine must reject an empty restaurants list."""
        with pytest.raises(ValueError):
            engine.calculate(
                order_placed_at=future_order_time,
                driver_start_minutes=0,
                stops=[],
                restaurants=[],
                travel_to_customer_minutes=10,
            )

    def test_stop_restaurant_mismatch_raises(self, engine, future_order_time):
        """Engine must raise when a stop references an unknown restaurant_id."""
        with pytest.raises(ValueError):
            engine.calculate(
                order_placed_at=future_order_time,
                driver_start_minutes=0,
                stops=[RouteStop(restaurant_id="unknown_id", stop_order=1, travel_minutes_from_prev=5)],
                restaurants=[Restaurant(id="r1", name="R1", prep_minutes=10)],
                travel_to_customer_minutes=10,
            )


# ==============================================================================
# LAYER 2 — SCHEMA VALIDATION TESTS
# ==============================================================================

class TestScheduleRequestSchema:
    """Tests for Pydantic validation in ScheduleRequest."""

    def _valid_payload(self, order_placed_at: str = "2099-01-01T15:00:00"):
        """Returns a minimal valid request payload dict."""
        return {
            "order_id": "TEST",
            "order_placed_at": order_placed_at,
            "driver_start_minutes": 0,
            "restaurants": [
                {"id": "r1", "name": "R1", "prep_minutes": 15},
                {"id": "r2", "name": "R2", "prep_minutes": 20},
            ],
            "stops": [
                {"restaurant_id": "r1", "stop_order": 1, "travel_minutes_from_prev": 0},
                {"restaurant_id": "r2", "stop_order": 2, "travel_minutes_from_prev": 10},
            ],
            "travel_to_customer_minutes": 15,
        }

    def test_valid_request_passes(self):
        payload = self._valid_payload()
        req = ScheduleRequest(**payload)
        assert req.order_id == "TEST"

    def test_duplicate_restaurant_ids_rejected(self):
        payload = self._valid_payload()
        payload["restaurants"][1]["id"] = "r1"   # duplicate
        payload["stops"][1]["restaurant_id"] = "r1"
        with pytest.raises(Exception, match="Duplicate restaurant IDs"):
            ScheduleRequest(**payload)

    def test_duplicate_stop_orders_rejected(self):
        payload = self._valid_payload()
        payload["stops"][1]["stop_order"] = 1   # duplicate order
        with pytest.raises(Exception, match="Duplicate stop_order"):
            ScheduleRequest(**payload)

    def test_stop_referencing_unknown_restaurant_rejected(self):
        payload = self._valid_payload()
        payload["stops"][1]["restaurant_id"] = "unknown"
        with pytest.raises(Exception):
            ScheduleRequest(**payload)

    def test_four_restaurants_rejected_by_schema(self):
        payload = self._valid_payload()
        payload["restaurants"].append({"id": "r3", "name": "R3", "prep_minutes": 10})
        payload["restaurants"].append({"id": "r4", "name": "R4", "prep_minutes": 10})
        with pytest.raises(Exception):
            ScheduleRequest(**payload)

    def test_negative_prep_minutes_rejected(self):
        payload = self._valid_payload()
        payload["restaurants"][0]["prep_minutes"] = -5
        with pytest.raises(Exception):
            ScheduleRequest(**payload)

    def test_zero_prep_minutes_rejected(self):
        payload = self._valid_payload()
        payload["restaurants"][0]["prep_minutes"] = 0
        with pytest.raises(Exception):
            ScheduleRequest(**payload)


# ==============================================================================
# LAYER 3 — API INTEGRATION TESTS
# ==============================================================================

class TestScheduleEndpoint:
    """Tests for POST /api/v1/schedule/ — full HTTP request/response cycle."""

    def _spec_payload(self, order_placed_at: str = "2099-01-01T15:00:00"):
        return {
            "order_id": "API_TEST",
            "order_placed_at": order_placed_at,
            "driver_start_minutes": 0,
            "restaurants": [
                {"id": "rest_b", "name": "Restaurant B", "prep_minutes": 15},
                {"id": "rest_a", "name": "Restaurant A", "prep_minutes": 20},
            ],
            "stops": [
                {"restaurant_id": "rest_b", "stop_order": 1, "travel_minutes_from_prev": 0},
                {"restaurant_id": "rest_a", "stop_order": 2, "travel_minutes_from_prev": 20},
            ],
            "travel_to_customer_minutes": 25,
        }

    def test_valid_request_returns_200(self, client):
        response = client.post("/api/v1/schedule/", json=self._spec_payload())
        assert response.status_code == 200

    def test_response_contains_expected_fields(self, client):
        response = client.post("/api/v1/schedule/", json=self._spec_payload())
        data = response.json()
        assert "order_id" in data
        assert "cook_schedules" in data
        assert "customer_eta" in data
        assert "total_route_minutes" in data
        assert "is_feasible" in data
        assert "warnings" in data

    def test_response_order_id_matches_request(self, client):
        response = client.post("/api/v1/schedule/", json=self._spec_payload())
        assert response.json()["order_id"] == "API_TEST"

    def test_response_has_two_cook_schedules(self, client):
        response = client.post("/api/v1/schedule/", json=self._spec_payload())
        assert len(response.json()["cook_schedules"]) == 2

    def test_cook_schedule_fields_present(self, client):
        response = client.post("/api/v1/schedule/", json=self._spec_payload())
        schedule = response.json()["cook_schedules"][0]
        assert "restaurant_id" in schedule
        assert "restaurant_name" in schedule
        assert "driver_arrives_at" in schedule
        assert "cook_start_at" in schedule
        assert "estimated_ready_at" in schedule
        assert "minutes_until_cook_start" in schedule
        assert "is_start_immediate" in schedule

    def test_total_route_minutes_correct(self, client):
        """Total route for spec example with future order = 45 min (no driver wait at B)."""
        response = client.post("/api/v1/schedule/", json=self._spec_payload())
        assert response.json()["total_route_minutes"] == 45

    def test_is_feasible_true_for_far_future_order(self, client):
        response = client.post("/api/v1/schedule/", json=self._spec_payload())
        assert response.json()["is_feasible"] is True

    def test_empty_restaurants_returns_422(self, client):
        payload = self._spec_payload()
        payload["restaurants"] = []
        payload["stops"] = []
        response = client.post("/api/v1/schedule/", json=payload)
        assert response.status_code == 422

    def test_duplicate_restaurant_id_returns_422(self, client):
        payload = self._spec_payload()
        payload["restaurants"][1]["id"] = "rest_b"
        payload["stops"][1]["restaurant_id"] = "rest_b"
        response = client.post("/api/v1/schedule/", json=payload)
        assert response.status_code == 422

    def test_missing_required_field_returns_422(self, client):
        payload = self._spec_payload()
        del payload["travel_to_customer_minutes"]
        response = client.post("/api/v1/schedule/", json=payload)
        assert response.status_code == 422

    def test_invalid_json_returns_422(self, client):
        response = client.post(
            "/api/v1/schedule/",
            content="not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


class TestExampleEndpoint:
    """Tests for GET /api/v1/schedule/example"""

    def test_example_returns_200(self, client):
        response = client.get("/api/v1/schedule/example")
        assert response.status_code == 200

    def test_example_order_id(self, client):
        response = client.get("/api/v1/schedule/example")
        assert response.json()["order_id"] == "EXAMPLE_ORDER"

    def test_example_has_two_restaurants(self, client):
        response = client.get("/api/v1/schedule/example")
        assert len(response.json()["cook_schedules"]) == 2

    def test_example_total_route_minutes(self, client):
        """Spec example: B(0 travel) → A(20 travel, 20 prep) → customer(25) = 60 min."""
        response = client.get("/api/v1/schedule/example")
        assert response.json()["total_route_minutes"] == 60


class TestHealthEndpoint:
    """Tests for GET /health"""

    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_status_ok(self, client):
        response = client.get("/health")
        assert response.json()["status"] == "ok"

    def test_health_has_version(self, client):
        response = client.get("/health")
        assert "version" in response.json()

    def test_health_has_engine(self, client):
        response = client.get("/health")
        assert "engine" in response.json()
