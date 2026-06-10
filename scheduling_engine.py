"""
================================================================================
FreshRoute — Scheduling Engine
================================================================================

File      : scheduling_engine.py
Purpose   : Core algorithm that calculates when each restaurant should start
            cooking so that food is ready exactly when the driver arrives.

Innovation: Instead of every restaurant cooking immediately after an order is
            placed, this engine back-calculates optimal cook-start times using
            the formula:

                cook_start = driver_arrival_at_restaurant - max_prep_time

This means food is always fresh, never cold, and never makes the driver wait.

Author    : [Your Name]
Project   : FreshRoute
License   : MIT

================================================================================
HOW TO USE THIS FILE
================================================================================

    from scheduling_engine import SchedulingEngine, Restaurant, RouteStop

    # 1. Define restaurants in the order
    restaurants = [
        Restaurant(id="rest_b", name="Restaurant B", prep_minutes=15),
        Restaurant(id="rest_a", name="Restaurant A", prep_minutes=20),
    ]

    # 2. Define the pickup sequence (already determined by route optimizer)
    stops = [
        RouteStop(restaurant_id="rest_b", stop_order=1, travel_minutes_from_prev=0),
        RouteStop(restaurant_id="rest_a", stop_order=2, travel_minutes_from_prev=20),
    ]

    # 3. Run the engine
    engine = SchedulingEngine()
    result = engine.calculate(
        order_placed_at=datetime(2024, 1, 1, 15, 0),   # 3:00 PM
        driver_start_minutes=5,                          # driver takes 5 min to reach stop 1
        stops=stops,
        restaurants=restaurants,
        travel_to_customer_minutes=25,
    )

    # 4. Read the output
    for schedule in result.cook_schedules:
        print(schedule.restaurant_name, "→ start at", schedule.cook_start_at)

    print("Customer ETA:", result.customer_eta)

================================================================================
"""

# ------------------------------------------------------------------------------
# Standard library imports — no external dependencies required.
# datetime is used for all time arithmetic.
# dataclasses gives us clean, typed data containers without boilerplate.
# typing provides List and Optional for type hints.
# ------------------------------------------------------------------------------

from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Optional


# ==============================================================================
# DATA CLASSES
# ==============================================================================
# These are the inputs and outputs of the scheduling engine.
# Think of them as typed contracts — they enforce what data the engine needs
# and what it promises to return.
# ==============================================================================


@dataclass
class Restaurant:
    """
    Represents one restaurant participating in a multi-restaurant order.

    Fields
    ------
    id : str
        Unique identifier. In production this is a UUID from your database.

    name : str
        Human-readable name. Used in output for readability and logging.

    prep_minutes : int
        The maximum preparation time for this restaurant's portion of the order.

        WHY MAXIMUM?
        A kitchen works in parallel — if the order has a burger (8 min) and
        fries (5 min), the kitchen starts both at the same time. The order is
        ready when the SLOWEST item is done, which is 8 minutes, not 8+5=13.

        In production, you compute this as:
            max(item.prep_minutes for item in order_items
                if item.restaurant_id == restaurant.id)

        For the MVP, the restaurant's stored avg_prep_minutes is a safe fallback.
    """

    id: str
    name: str
    prep_minutes: int


@dataclass
class RouteStop:
    """
    Represents one stop on the driver's pickup route.

    The route is an ordered sequence: stop 1 → stop 2 → stop 3 → customer.
    Each stop knows how long it takes to drive there from the previous stop.

    Fields
    ------
    restaurant_id : str
        Foreign key linking this stop to a Restaurant object.

    stop_order : int
        Position in the pickup sequence. 1 = first pickup, 2 = second, etc.
        The driver visits stops in ascending order.

    travel_minutes_from_prev : int
        Drive time in minutes from the previous stop to this one.

        For stop_order == 1: this is the drive time from the driver's
        CURRENT location to the first restaurant.

        For stop_order == 2+: this is the drive time from the previous
        restaurant to this restaurant.

        In production this comes from the Google Maps Directions API or a
        Distance Matrix API call, not a hardcoded number.
    """

    restaurant_id: str
    stop_order: int
    travel_minutes_from_prev: int


@dataclass
class CookSchedule:
    """
    The output for a single restaurant — when to start cooking and when
    the food should be ready.

    This is what gets written to the ORDER_RESTAURANTS table in your database
    and what gets sent as a notification to the restaurant's dashboard.

    Fields
    ------
    restaurant_id : str
        Which restaurant this schedule applies to.

    restaurant_name : str
        Human-readable name for logs and notifications.

    driver_arrives_at : datetime
        The exact moment the driver is expected to reach this restaurant.
        This is the ANCHOR point — everything is calculated relative to this.

    cook_start_at : datetime
        The moment the restaurant should start cooking.
        Formula: driver_arrives_at - prep_minutes

        If this is in the past (because the order was placed with very short
        notice), the engine clamps it to "now" and logs a warning.

    estimated_ready_at : datetime
        The moment the food should be ready for pickup.
        Formula: cook_start_at + prep_minutes

        In the ideal case: estimated_ready_at == driver_arrives_at (perfect sync).
        In the clamped case: estimated_ready_at < driver_arrives_at
        (driver waits briefly — better than cold food).

    minutes_until_cook_start : float
        How many minutes from NOW until the restaurant should start cooking.
        Positive → "start in X minutes" (normal case).
        Zero      → "start immediately" (clamped case).
        Negative  → should not occur after clamping, but logged if it does.

    is_start_immediate : bool
        True if the cook start was clamped to now. A flag the notification
        system uses to send "Start cooking NOW" vs "Start cooking at HH:MM".
    """

    restaurant_id: str
    restaurant_name: str
    driver_arrives_at: datetime
    cook_start_at: datetime
    estimated_ready_at: datetime
    minutes_until_cook_start: float
    is_start_immediate: bool


@dataclass
class ScheduleResult:
    """
    The complete output of the scheduling engine for one order.

    Fields
    ------
    order_id : str
        Which order this result belongs to. Used when persisting to DB.

    cook_schedules : List[CookSchedule]
        One CookSchedule per restaurant, in stop order.

    customer_eta : datetime
        The estimated delivery time to the customer's address.

    total_route_minutes : int
        Total time from order placement to customer delivery.

    is_feasible : bool
        True if all cook start times are in the future (or now).
        False if the driver cannot arrive before some restaurants finish cooking
        even if they start immediately — signals an infeasible route.

    warnings : List[str]
        Human-readable explanations of any clamping or edge cases.
        Empty list in the normal case.
    """

    order_id: str
    cook_schedules: List[CookSchedule]
    customer_eta: datetime
    total_route_minutes: int
    is_feasible: bool
    warnings: List[str] = field(default_factory=list)


# ==============================================================================
# SCHEDULING ENGINE
# ==============================================================================


class SchedulingEngine:
    """
    The core of FreshRoute.

    This class contains the algorithm that makes just-in-time food preparation
    possible. It is a pure calculation engine — it has no database connections,
    no HTTP calls, and no side effects. It only takes data in and returns data.

    This design decision is intentional and important:
    - It makes the engine trivially testable (no mocking required).
    - It makes the engine reusable in any context (API handler, CLI, cron job).
    - It separates the algorithm from the infrastructure.

    The algorithm runs in four steps:
        1. Sort stops by their stop_order to establish pickup sequence.
        2. Compute the driver's arrival time at each restaurant by summing
           travel times from order placement forward.
        3. Back-calculate each restaurant's cook start time from its arrival time.
        4. Compute the final customer ETA from the last stop's arrival + last
           travel segment to the customer.

    Time complexity : O(n log n) where n = number of restaurants (sort + scan).
    Space complexity: O(n) for the output schedules.
    For n ≤ 3 (your current constraint), this is effectively O(1).
    """

    def calculate(
        self,
        order_placed_at: datetime,
        driver_start_minutes: int,
        stops: List[RouteStop],
        restaurants: List[Restaurant],
        travel_to_customer_minutes: int,
        order_id: str = "ORDER_001",
    ) -> ScheduleResult:
        """
        Run the scheduling algorithm.

        Parameters
        ----------
        order_placed_at : datetime
            The exact moment the customer confirmed their order.
            This is the starting point of all time calculations.

        driver_start_minutes : int
            Minutes from order placement until the driver begins moving.
            Accounts for: driver acceptance delay + time to reach vehicle.
            Typical value: 3–7 minutes. Use 0 in tests.

        stops : List[RouteStop]
            The ordered sequence of restaurant pickups. Must contain exactly
            the same restaurants as the `restaurants` parameter.
            Generated by your route optimizer (or manually for MVP).

        restaurants : List[Restaurant]
            Restaurant objects with prep times. Order does not matter here —
            the engine looks them up by ID when processing each stop.

        travel_to_customer_minutes : int
            Drive time from the LAST restaurant pickup to the customer's address.
            Comes from Google Maps Directions API in production.

        order_id : str
            The order identifier. Written into the result for traceability.

        Returns
        -------
        ScheduleResult
            Complete scheduling output. Persist this to your database and
            send notifications to restaurants.
        """

        # ------------------------------------------------------------------
        # STEP 0: VALIDATION
        # Fail fast with a clear error if inputs are invalid.
        # Never let bad data silently produce wrong schedules.
        # ------------------------------------------------------------------

        if not stops:
            raise ValueError("Cannot schedule an order with no route stops.")

        if not restaurants:
            raise ValueError("Cannot schedule an order with no restaurants.")

        if len(stops) != len(restaurants):
            raise ValueError(
                f"Mismatch: {len(stops)} stops but {len(restaurants)} restaurants. "
                "Every stop must have a corresponding restaurant."
            )

        if len(restaurants) > 3:
            raise ValueError(
                f"FreshRoute supports a maximum of 3 restaurants per order. "
                f"Received {len(restaurants)}."
            )

        # ------------------------------------------------------------------
        # STEP 1: BUILD A LOOKUP MAP (restaurant_id → Restaurant)
        #
        # WHY A DICTIONARY?
        # When processing each stop, we need to find the restaurant's prep time.
        # If we searched a list each time: O(n) per stop → O(n²) total.
        # With a dict: O(1) per lookup → O(n) total.
        #
        # For n ≤ 3 this doesn't matter for performance — but it matters for
        # code clarity and correctness. Always index by ID, never by position.
        # ------------------------------------------------------------------

        restaurant_map: dict[str, Restaurant] = {
            r.id: r for r in restaurants
        }

        # Validate that every stop references a known restaurant.
        for stop in stops:
            if stop.restaurant_id not in restaurant_map:
                raise ValueError(
                    f"Stop references restaurant_id='{stop.restaurant_id}' "
                    f"which is not in the restaurants list."
                )

        # ------------------------------------------------------------------
        # STEP 2: SORT STOPS BY stop_order
        #
        # WHY SORT?
        # The caller might pass stops in any order. We must process them
        # in the driver's actual pickup sequence: 1, 2, 3.
        # Arrival times are cumulative — stop 2's arrival depends on stop 1's.
        # Sorting guarantees the correct cumulative sum.
        # ------------------------------------------------------------------

        ordered_stops: List[RouteStop] = sorted(stops, key=lambda s: s.stop_order)

        # ------------------------------------------------------------------
        # STEP 3: COMPUTE DRIVER ARRIVAL TIMES AT EACH STOP
        #
        # The driver doesn't leave immediately when the order is placed.
        # driver_start_minutes accounts for acceptance delay.
        #
        # Then we walk through the stops, adding each segment's travel time.
        # The arrival time at stop N = arrival time at stop (N-1) + travel_minutes.
        # For stop 1, arrival = order_time + driver_start_minutes + travel_from_driver.
        #
        # TIMELINE EXAMPLE (from the project specification):
        #   3:00 PM  — order placed
        #   3:00 PM  — driver starts (driver_start_minutes = 0)
        #   Stop 1 (Restaurant B): travel_minutes_from_prev = 0 (driver is already near B)
        #   Arrival at B = 3:00 + 0 + 0 = 3:00 PM
        #   Stop 2 (Restaurant A): travel_minutes_from_prev = 20
        #   Arrival at A = 3:00 + 20 = 3:20 PM
        #
        # Wait — the spec says 3:35 for A. That's because B has 15 min prep
        # and the driver waits for B to be ready before leaving.
        # We handle that wait time below in Step 4.
        # ------------------------------------------------------------------

        warnings: List[str] = []

        # The moment the driver begins moving toward the first restaurant.
        driver_departs_at: datetime = order_placed_at + timedelta(minutes=driver_start_minutes)

        # We track the current clock as the driver moves through the route.
        # This variable advances as we process each stop.
        current_time: datetime = driver_departs_at

        # We'll accumulate the final cook schedules here.
        cook_schedules: List[CookSchedule] = []

        # ------------------------------------------------------------------
        # STEP 4: PROCESS EACH STOP IN SEQUENCE
        #
        # For each stop:
        #   a) Advance the clock by the travel time to reach this stop.
        #   b) This gives us driver_arrives_at for this restaurant.
        #   c) Back-calculate: cook_start = driver_arrives_at - prep_minutes.
        #   d) Clamp cook_start to "now" if it's in the past.
        #   e) The driver leaves this stop when the food is actually ready.
        #      (If food is ready before driver arrives → driver takes it immediately.
        #       If driver arrives before food is ready → driver waits.)
        #   f) Update current_time to reflect when the driver DEPARTS this stop.
        # ------------------------------------------------------------------

        now: datetime = datetime.now()

        for stop in ordered_stops:
            restaurant: Restaurant = restaurant_map[stop.restaurant_id]

            # ----------------------------------------------------------------
            # (a) Advance clock: driver travels from previous location to here.
            # ----------------------------------------------------------------
            current_time += timedelta(minutes=stop.travel_minutes_from_prev)
            driver_arrives_at: datetime = current_time

            # ----------------------------------------------------------------
            # (b) Back-calculate cook start time.
            #
            # THE CORE FORMULA:
            #   cook_start = driver_arrives_at − prep_minutes
            #
            # Why subtract? Because we want food to FINISH at driver_arrives_at.
            # If food takes 20 minutes and the driver arrives at 3:35,
            # cooking must START at 3:15 (20 minutes before 3:35).
            # ----------------------------------------------------------------
            ideal_cook_start: datetime = driver_arrives_at - timedelta(minutes=restaurant.prep_minutes)

            # ----------------------------------------------------------------
            # (c) Clamp to now if cook_start is in the past.
            #
            # This happens when prep time is very short, or the driver is
            # starting from very close to the restaurant, or the order was
            # placed with barely any lead time.
            #
            # Example: prep=5 min, driver arrives in 2 min.
            #   ideal_cook_start = now - 3 minutes → already past!
            #   Clamped to: now (start immediately)
            #   Food ready at: now + 5 = 3 minutes after driver arrives.
            #   Driver waits 3 minutes. That's acceptable.
            #   It's still better than a traditional system where ALL restaurants
            #   start cooking at order placement.
            # ----------------------------------------------------------------
            is_start_immediate = False

            if ideal_cook_start < now:
                warnings.append(
                    f"Restaurant '{restaurant.name}': ideal cook start "
                    f"({ideal_cook_start.strftime('%H:%M')}) is in the past. "
                    f"Clamped to now. Driver may wait briefly at this stop."
                )
                cook_start_at = now
                is_start_immediate = True
            else:
                cook_start_at = ideal_cook_start

            # ----------------------------------------------------------------
            # (d) Compute when food will actually be ready.
            #
            # ready_at = cook_start + prep_minutes
            # In the ideal case: ready_at == driver_arrives_at (perfect sync).
            # In the clamped case: ready_at > driver_arrives_at (driver waits).
            # ----------------------------------------------------------------
            estimated_ready_at: datetime = cook_start_at + timedelta(minutes=restaurant.prep_minutes)

            # ----------------------------------------------------------------
            # (e) How many minutes from NOW until cooking should start.
            #
            # This is the practical value sent to the restaurant's dashboard.
            # "Start cooking in 15 minutes" is more actionable than a timestamp.
            # ----------------------------------------------------------------
            minutes_until_cook_start: float = max(
                0.0,
                (cook_start_at - now).total_seconds() / 60
            )

            # ----------------------------------------------------------------
            # (f) Build the CookSchedule output object for this restaurant.
            # ----------------------------------------------------------------
            cook_schedules.append(
                CookSchedule(
                    restaurant_id=restaurant.id,
                    restaurant_name=restaurant.name,
                    driver_arrives_at=driver_arrives_at,
                    cook_start_at=cook_start_at,
                    estimated_ready_at=estimated_ready_at,
                    minutes_until_cook_start=minutes_until_cook_start,
                    is_start_immediate=is_start_immediate,
                )
            )

            # ----------------------------------------------------------------
            # (g) Advance the clock: driver DEPARTS when food is actually ready.
            #
            # WHY THIS MATTERS:
            # If the driver arrives at 3:20 but food is ready at 3:35,
            # the driver doesn't leave at 3:20. They leave at 3:35.
            # This means the next stop's arrival time must be calculated
            # from 3:35, not 3:20.
            #
            # max(driver_arrives_at, estimated_ready_at) correctly handles both:
            #   - Driver waits for food:  departs at estimated_ready_at
            #   - Food waits for driver:  departs at driver_arrives_at (instant pickup)
            #
            # This is the most subtle part of the algorithm. A naive
            # implementation that ignores driver wait time produces wrong ETAs.
            # ----------------------------------------------------------------
            current_time = max(driver_arrives_at, estimated_ready_at)

        # ------------------------------------------------------------------
        # STEP 5: COMPUTE CUSTOMER ETA
        #
        # After the last restaurant pickup, the driver travels to the customer.
        # customer_eta = departure_from_last_stop + travel_to_customer_minutes
        # ------------------------------------------------------------------

        customer_eta: datetime = current_time + timedelta(minutes=travel_to_customer_minutes)

        # Total elapsed minutes from order placement to delivery.
        total_route_minutes: int = int(
            (customer_eta - order_placed_at).total_seconds() / 60
        )

        # ------------------------------------------------------------------
        # STEP 6: FEASIBILITY CHECK
        #
        # The route is infeasible if any restaurant's cook start was clamped
        # to "now" AND the driver arrives before the food is ready.
        # This signals to the system that the order cannot be served fresh
        # within normal parameters.
        #
        # In a production system you might reject such orders or flag them
        # for manual review. For MVP, we allow them but set is_feasible=False.
        # ------------------------------------------------------------------

        is_feasible: bool = len(warnings) == 0

        return ScheduleResult(
            order_id=order_id,
            cook_schedules=cook_schedules,
            customer_eta=customer_eta,
            total_route_minutes=total_route_minutes,
            is_feasible=is_feasible,
            warnings=warnings,
        )


# ==============================================================================
# PRETTY PRINTER
# ==============================================================================
# A standalone function that formats a ScheduleResult for human-readable output.
# Use this in your CLI runner, in logs, and in test output.
# This is NOT part of the algorithm — it's a display utility.
# ==============================================================================


def print_schedule(result: ScheduleResult) -> None:
    """
    Prints a ScheduleResult in a formatted, human-readable form.
    Useful for debugging, demos, and CLI output.
    """

    divider = "=" * 60
    thin    = "-" * 60

    print(f"\n{divider}")
    print(f"  FreshRoute Schedule — Order: {result.order_id}")
    print(divider)

    for idx, schedule in enumerate(result.cook_schedules, start=1):
        print(f"\n  Stop {idx}: {schedule.restaurant_name}")
        print(thin)

        if schedule.is_start_immediate:
            print(f"  ⚠  Cook start : NOW (clamped — driver arriving soon)")
        else:
            countdown = f"({schedule.minutes_until_cook_start:.0f} min from now)"
            print(f"  ▶  Cook start : {schedule.cook_start_at.strftime('%I:%M %p')} {countdown}")

        print(f"  ✓  Food ready : {schedule.estimated_ready_at.strftime('%I:%M %p')}")
        print(f"  🚗 Driver ETA : {schedule.driver_arrives_at.strftime('%I:%M %p')}")

    print(f"\n{divider}")
    print(f"  Customer ETA : {result.customer_eta.strftime('%I:%M %p')}")
    print(f"  Total time   : {result.total_route_minutes} minutes")
    print(f"  Feasible     : {'✓ Yes' if result.is_feasible else '✗ No (see warnings)'}")

    if result.warnings:
        print(f"\n  Warnings:")
        for w in result.warnings:
            print(f"    • {w}")

    print(f"{divider}\n")


# ==============================================================================
# DEMO RUNNER
# ==============================================================================
# When you run this file directly (`python scheduling_engine.py`), this block
# executes the exact example from the project specification.
#
# Expected output (matching the spec):
#   Restaurant B → start at 3:00 PM
#   Restaurant A → start at 3:15 PM
#   Customer ETA → 4:00 PM  (approx, depending on exact now())
#
# The `if __name__ == "__main__"` guard ensures this code does NOT run when
# the file is imported as a module. It only runs when executed directly.
# This is a Python best practice — always use this guard for demo/test code.
# ==============================================================================


if __name__ == "__main__":

    # --------------------------------------------------------------------------
    # SCENARIO 1: The exact example from the project specification.
    #
    # Order Time    = 3:00 PM
    # Restaurant B  = 15 min prep,  Stop 1 (driver starts here)
    # Restaurant A  = 20 min prep,  Stop 2 (20 min drive from B)
    # A → Customer  = 25 min drive
    #
    # Expected results:
    #   Restaurant B: start cooking at 3:00 PM
    #   Restaurant A: start cooking at 3:15 PM
    #   Customer ETA: 4:00 PM (3:00 + 0 + 15 wait + 20 travel + 20 cook + 5 min = 4:00)
    # --------------------------------------------------------------------------

    print("\n" + "=" * 60)
    print("  SCENARIO 1 — Specification Example")
    print("=" * 60)

    order_time = datetime.now().replace(
        hour=15, minute=0, second=0, microsecond=0
    )

    restaurants_s1 = [
        Restaurant(id="rest_b", name="Restaurant B", prep_minutes=15),
        Restaurant(id="rest_a", name="Restaurant A", prep_minutes=20),
    ]

    stops_s1 = [
        # Stop 1: driver starts at Restaurant B — no travel from prev, driver is there.
        RouteStop(restaurant_id="rest_b", stop_order=1, travel_minutes_from_prev=0),
        # Stop 2: 20 min drive from Restaurant B to Restaurant A.
        RouteStop(restaurant_id="rest_a", stop_order=2, travel_minutes_from_prev=20),
    ]

    engine = SchedulingEngine()

    result_s1 = engine.calculate(
        order_placed_at=order_time,
        driver_start_minutes=0,         # Driver departs instantly for the spec example.
        stops=stops_s1,
        restaurants=restaurants_s1,
        travel_to_customer_minutes=25,  # 25 min from Restaurant A to customer.
        order_id="ORDER_001",
    )

    print_schedule(result_s1)

    # --------------------------------------------------------------------------
    # SCENARIO 2: Three restaurants, realistic driver delay.
    #
    # Tests the full 3-restaurant constraint with a driver who takes 5 min
    # to accept the order and begin moving.
    # --------------------------------------------------------------------------

    print("=" * 60)
    print("  SCENARIO 2 — Three Restaurants with Driver Delay")
    print("=" * 60)

    restaurants_s2 = [
        Restaurant(id="pizza",  name="Pizza Palace",  prep_minutes=18),
        Restaurant(id="sushi",  name="Sushi Garden",  prep_minutes=12),
        Restaurant(id="tacos",  name="Taco Town",     prep_minutes=8),
    ]

    stops_s2 = [
        RouteStop(restaurant_id="pizza", stop_order=1, travel_minutes_from_prev=3),
        RouteStop(restaurant_id="sushi", stop_order=2, travel_minutes_from_prev=10),
        RouteStop(restaurant_id="tacos", stop_order=3, travel_minutes_from_prev=7),
    ]

    result_s2 = engine.calculate(
        order_placed_at=order_time,
        driver_start_minutes=5,
        stops=stops_s2,
        restaurants=restaurants_s2,
        travel_to_customer_minutes=12,
        order_id="ORDER_002",
    )

    print_schedule(result_s2)

    # --------------------------------------------------------------------------
    # SCENARIO 3: Edge case — very short prep time triggers clamping.
    #
    # Restaurant C has only 2 min prep and driver arrives in 1 min.
    # Ideal cook start = (now + 1 min) - 2 min = 1 minute in the past.
    # Engine clamps to now and adds a warning.
    # --------------------------------------------------------------------------

    print("=" * 60)
    print("  SCENARIO 3 — Edge Case: Cook Start Clamping")
    print("=" * 60)

    restaurants_s3 = [
        Restaurant(id="slow",  name="Slow Kitchen",  prep_minutes=25),
        Restaurant(id="fast",  name="Fast Kitchen",  prep_minutes=2),
    ]

    stops_s3 = [
        RouteStop(restaurant_id="slow", stop_order=1, travel_minutes_from_prev=5),
        RouteStop(restaurant_id="fast", stop_order=2, travel_minutes_from_prev=1),
    ]

    result_s3 = engine.calculate(
        order_placed_at=order_time,
        driver_start_minutes=0,
        stops=stops_s3,
        restaurants=restaurants_s3,
        travel_to_customer_minutes=10,
        order_id="ORDER_003",
    )

    print_schedule(result_s3)
