from . import formulas as cf
from ... import standard_keys as c
from .get_total_volume import calculate_total_pcu
from ...utils.dump_to_file import dump_to_file
from ..carriage_width_info.carriagewayStandards import CarriagewayStandards


def validate_new(a, Traffic_Input, debug=False):
    """
    Validates input data including the new free-flow toggle.
    """
    errors = []
    # ... (Keep existing validation for 'a', lane_type, and hourly_dist) ...

    additional_inputs = Traffic_Input.get("additional_inputs", {})

    # Validate the new boolean toggle
    free_flow_toggle = additional_inputs.get(c.FORCE_FREE_FLOW_OFF_PEAK, False)
    if not isinstance(free_flow_toggle, bool):
        errors.append(f"{c.FORCE_FREE_FLOW_OFF_PEAK} must be a boolean.")

    if errors:
        raise ValueError("\n".join(errors))
    return True


def calculate_total_adjusted_costs(a, Traffic_Input, debug=False):
    """
    Calculates costs with an optional override for off-peak congestion factors.
    """
    validate_new(a, Traffic_Input, debug)

    add_in = Traffic_Input["additional_inputs"]
    vehicle_data = Traffic_Input["vehicle_data"]

    # 1. Extract Parameters
    hourly_dist = add_in.get(c.PEAK_HOURLY_DISTRIBUTION, [])
    h_capacity = add_in.get(c.HOURLY_CAPACITY)
    lane = add_in.get("alternate_road_carriageway")
    lane_type = CarriagewayStandards.get_velocity_class(lane)
    # New Toggle
    force_free_flow = add_in.get(c.FORCE_FREE_FLOW_OFF_PEAK, False)

    # 2. PCU Volume Calculation
    # total_daily_pcu = sum(
    #     v.get("vehicles_per_day", 0) * v_const.pcu.get(vt)
    #     for vt, v in vehicle_data.items()
    # )
    total_daily_pcu = calculate_total_pcu(vehicle_data, debug=debug)["total_daily_pcu"]

    # 3. State Definitions
    peak_hours_count = len(hourly_dist)
    off_peak_duration = 24 - peak_hours_count
    peak_share_total = sum(hourly_dist)
    off_peak_share_total = 1.0 - peak_share_total

    states = []
    # Peak Hours: Always use calculated V/C
    for i, hourly_percent in enumerate(hourly_dist):
        hourly_pcu_vol = total_daily_pcu * hourly_percent

        vc_cal = hourly_pcu_vol / h_capacity
        vc_considered = min(vc_cal, 1.0)

        states.append(
            {
                "id": f"Peak_Hour_{i+1}",
                "vc_cal": vc_cal,
                "vc_considered": vc_considered,
                "share": hourly_percent,
                "is_peak": True,
            }
        )

    # Off-Peak Period
    if off_peak_duration > 0:
        off_peak_hourly_pcu = (
            total_daily_pcu * off_peak_share_total
        ) / off_peak_duration

        vc_cal = off_peak_hourly_pcu / h_capacity
        vc_considered = min(vc_cal, 1.0)

        states.append(
            {
                "id": "Off_Peak_Period",
                "vc_cal": vc_cal,
                "vc_considered": vc_considered,
                "share": off_peak_share_total,
                "is_peak": False,
            }
        )

    # 4. Aggregation Loop
    total_dist_cost = {c.IT: 0.0, c.ET: 0.0}
    total_time_cost = {c.IT: 0.0, c.ET: 0.0}
    temporal_breakdown = []
    input_vehicles = list(vehicle_data.keys())

    for state in states:
        # Check if we should override factors for off-peak
        if not state["is_peak"] and force_free_flow:
            # Factor = 1.0 means no additional congestion cost
            d_factors = {v: 1.0 for v in input_vehicles + ["buses"]}
            t_factors = {v: 1.0 for v in input_vehicles + ["buses"]}
        else:
            d_factors = cf.distance_congestion_factors(
                lane_type, vc=state["vc_considered"]
            )
            t_factors = cf.time_congestion_factors(lane_type, vc=state["vc_considered"])

        state_result = {
            "state": state["id"],
            "v_c_calculated": round(state["vc_cal"], 4),
            "v_c_considered": round(state["vc_considered"], 4),
            "traffic_share": state["share"],
            "free_flow_applied": (not state["is_peak"] and force_free_flow),
            "vehicle_impacts": {},
        }

        for v_key in input_vehicles:
            formula_key = "buses" if v_key in ["o_buses", "d_buses"] else v_key
            base_cost_key = (
                v_key
                if v_key in a["distanceCost"]
                else (formula_key if formula_key in a["distanceCost"] else None)
            )

            if (
                base_cost_key is None
                or vehicle_data[v_key].get("vehicles_per_day", 0) == 0
            ):
                continue

            cd = d_factors.get(formula_key, 1.0)
            ct = t_factors.get(formula_key, 1.0)
            share_of_vehicles = vehicle_data[v_key]["vehicles_per_day"] * state["share"]

            # Cost Calculations
            cost_dist_it = (
                a["distanceCost"][base_cost_key][c.IT] * cd
            ) * share_of_vehicles
            cost_dist_et = (
                a["distanceCost"][base_cost_key][c.ET] * cd
            ) * share_of_vehicles
            cost_time_it = (a["timeCost"][base_cost_key][c.IT] * ct) * share_of_vehicles
            cost_time_et = (a["timeCost"][base_cost_key][c.ET] * ct) * share_of_vehicles

            total_dist_cost[c.IT] += cost_dist_it
            total_dist_cost[c.ET] += cost_dist_et
            total_time_cost[c.IT] += cost_time_it
            total_time_cost[c.ET] += cost_time_et

            if debug:
                state_result["vehicle_impacts"][v_key] = {
                    "factors": {"dist": round(cd, 4), "time": round(ct, 4)},
                    "weighted_costs": {
                        "dist_it": round(cost_dist_it, 2),
                        "dist_et": round(cost_dist_et, 2),
                        "time_it": round(cost_time_it, 2),
                        "time_et": round(cost_time_et, 2),
                    },
                }

        if debug:
            temporal_breakdown.append(state_result)

    result = {
        "distance_total": total_dist_cost,
        "time_total": total_time_cost,
        "total": {
            c.IT: total_dist_cost[c.IT] + total_time_cost[c.IT],
            c.ET: total_dist_cost[c.ET] + total_time_cost[c.ET],
        },
        "unit": "Rs/km",
    }

    if debug:
        dump_to_file(
            "ruc-congestion-voc-4-Post_congestion_VOC.json",
            {"summary": result, "temporal_steps": temporal_breakdown},
        )

    return result
